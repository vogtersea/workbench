#!/usr/bin/env bash
# ============================================================
# 工作台项目 · 从备份恢复（SPEC.md 第 10 节 / W-12 恢复演练）
#
# 用法：
#   scripts/restore.sh <备份文件>          # 默认 DRY-RUN：只打印将要做什么，不动任何文件
#   scripts/restore.sh <备份文件> --yes    # 真执行：停容器 -> 留存现场 -> 覆盖 -> 起容器 -> 等 healthz
#
# 铁律：执行前一定先跑 scripts/backup.sh verify <备份文件>；
#       校验不通过直接拒绝，一个文件都不改。
#
# 环境变量（默认值贴合 NAS 实际布局）：
#   DATA_DIR     /volume3/docker3/workbench/data
#   COMPOSE_DIR  /volume3/docker3/workbench          # docker-compose.yml 所在目录
#   BACKUP_DIR   ${COMPOSE_DIR}/backup               # 恢复前现场快照放这里（默认跟 COMPOSE_DIR 走）
#   PROJECT      workbench                            # docker compose -p
#   SERVICE      workbench                            # compose 服务名 / 容器名
#   PORT         15468                                # 宿主端口，用于轮询 /healthz
#   HEALTH_TIMEOUT 90                                 # 秒
#   HEALTH_INTERVAL 2                                 # 秒
#   OWNER        10001:10001                          # 仅在以 root 执行时 chown 恢复出来的 app.db
#   COMPOSE_CMD  "docker compose"                     # 可换 podman / 演练桩
#   PYTHON       同 backup.sh，没有 sqlite3 命令时用 python 的 stdlib
#
# 退出码：0 = 成功（或 dry-run 通过）；非 0 = 失败（原因打在 stderr，并给出手工回退指引）。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERIFY_CMD="$SCRIPT_DIR/backup.sh"

DATA_DIR="${DATA_DIR:-/volume3/docker3/workbench/data}"
COMPOSE_DIR="${COMPOSE_DIR:-/volume3/docker3/workbench}"
BACKUP_DIR="${BACKUP_DIR:-$COMPOSE_DIR/backup}"
PROJECT="${COMPOSE_PROJECT:-workbench}"
SERVICE="${SERVICE:-workbench}"
PORT="${PORT:-15468}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"
HEALTH_INTERVAL="${HEALTH_INTERVAL:-2}"
OWNER="${OWNER:-10001:10001}"
COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"

DB="$DATA_DIR/app.db"
HEALTH_URL="http://127.0.0.1:$PORT/healthz"
STAMP="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT="$BACKUP_DIR/app-before-restore-$STAMP.db"
CHANGED=0
FILE=""
CONFIRM=0

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
err_log() { printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }
die() { err_log "$*"; exit 1; }

usage() {
  cat <<'USAGE'
用法：
  scripts/restore.sh <备份文件>          默认 DRY-RUN（只打印，不动文件）
  scripts/restore.sh <备份文件> --yes    真正执行恢复

步骤：先 verify 备份 -> 停容器 -> 留存现场 -> 覆盖 app.db -> 起容器 -> 轮询 /healthz -> 打印行数
任何一步失败都会打印原因与手工回退指引。

环境变量：DATA_DIR COMPOSE_DIR BACKUP_DIR COMPOSE_PROJECT SERVICE PORT
          HEALTH_TIMEOUT HEALTH_INTERVAL OWNER COMPOSE_CMD PYTHON
USAGE
}

# ---------------- SQLite 执行后端 ----------------
# 与 scripts/backup.sh 中的实现保持一致：两个脚本都能独立运行，不互相 source。
SQL_BACKEND=""
SQL_PY=""

detect_sql() {
  if command -v sqlite3 >/dev/null 2>&1; then
    SQL_BACKEND=cli
    return 0
  fi
  local cand
  for cand in ${PYTHON:-} python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sqlite3' >/dev/null 2>&1; then
      SQL_BACKEND=python
      SQL_PY="$cand"
      return 0
    fi
  done
  return 1
}

# sql_rows <库文件> <单条 SQL>：只读打开，逐行输出，列间 TAB 分隔
sql_rows() {
  if [ "$SQL_BACKEND" = cli ]; then
    sqlite3 "$1" "$2"
  else
    "$SQL_PY" - "$1" "$2" <<'PY'
import sqlite3
import sys
from urllib.parse import quote

path, sql = sys.argv[1], sys.argv[2]
con = sqlite3.connect("file:" + quote(path, safe="/:") + "?mode=ro", uri=True)
try:
    for row in con.execute(sql):
        print("\t".join("" if v is None else str(v) for v in row))
finally:
    con.close()
PY
  fi
}

# sql_vacuum_into <源库> <目标文件>：一致性快照，等价于 sqlite3 "VACUUM INTO"
sql_vacuum_into() {
  if [ "$SQL_BACKEND" = cli ]; then
    sqlite3 "$1" "VACUUM INTO '$2'"
  else
    "$SQL_PY" - "$1" "$2" <<'PY'
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(src)
try:
    con.execute("VACUUM INTO ?", (dst,))
finally:
    con.close()
PY
  fi
}

# ---------------- 行数 / 健康检查 ----------------
VERIFY_TABLES="tasks users reports routines"

# row_counts <库文件> <标题>：打印 tasks/users/reports/routines 行数；表缺失显示 -
row_counts() {
  local db="$1" title="$2" t n out
  printf '  %s：\n' "$title"
  if [ ! -f "$db" ]; then
    printf '    (文件不存在：%s)\n' "$db"
    return 0
  fi
  for t in $VERIFY_TABLES; do
    n="$(sql_rows "$db" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$t';" 2>/dev/null || echo 'x')"
    if [ "$n" = "1" ]; then
      out="$(sql_rows "$db" "SELECT COUNT(*) FROM $t;" 2>/dev/null || echo '?')"
      printf '    %-9s %s 行\n' "$t" "$out"
    else
      printf '    %-9s %s\n' "$t" "-（表缺失或读不到）"
    fi
  done
}

CURL=""
WGET=""
detect_http() {
  CURL="$(command -v curl 2>/dev/null || true)"
  WGET="$(command -v wget 2>/dev/null || true)"
  [ -n "$CURL" ] || [ -n "$WGET" ]
}

http_ok() {
  if [ -n "$CURL" ]; then
    "$CURL" -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1
  else
    "$WGET" -q -T 3 -O /dev/null "$HEALTH_URL" >/dev/null 2>&1
  fi
}

wait_health() {
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    if http_ok; then
      log "healthz 200（等待 ${waited}s）"
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
    waited=$((waited + HEALTH_INTERVAL))
  done
  return 1
}

# ---------------- 手工回退指引 ----------------
rollback_guide() {
  cat >&2 <<EOF
---------------- 手工回退指引（照抄即可） ----------------
 本次恢复前自动留存的现场：${SNAPSHOT}（空 = 恢复前本来没有库）
 1) 停容器：cd $COMPOSE_DIR && $COMPOSE_CMD -p $PROJECT stop
 2) 清 WAL：rm -f "$DB-wal" "$DB-shm"
 3) 放回现场：cp "$SNAPSHOT" "$DB"
 4) 属主：chown $OWNER "$DB"（非 root 执行可跳过）
 5) 起容器：cd $COMPOSE_DIR && $COMPOSE_CMD -p $PROJECT up -d
 6) 复核：curl -fsS $HEALTH_URL   # 期望 200
 7) 复核快照本身：$SCRIPT_DIR/backup.sh verify "$SNAPSHOT"
--------------------------------------------------------
EOF
}

on_err() {
  local code=$?
  err_log "上一步失败（退出码 $code，脚本第 $LINENO 行）"
  if [ "$CHANGED" -eq 1 ]; then
    rollback_guide
  fi
  exit "$code"
}
trap on_err ERR

# ---------------- 参数 ----------------
for arg in "$@"; do
  case "$arg" in
    --yes|-y) CONFIRM=1 ;;
    -h|--help|help) usage; exit 0 ;;
    -*) usage >&2; die "未知参数：$arg" ;;
    *)
      [ -z "$FILE" ] || die "只能指定一个备份文件（多余参数：$arg）"
      FILE="$arg"
      ;;
  esac
done
[ -n "$FILE" ] || { usage >&2; die "缺少备份文件参数"; }

# ---------------- 前置检查（都不修改任何东西） ----------------
[ -f "$VERIFY_CMD" ] || die "找不到校验脚本：$VERIFY_CMD"
detect_sql || die "需要 sqlite3 命令行，或可用的 python3（含 sqlite3 模块）"
detect_http || die "需要 curl 或 wget 轮询 $HEALTH_URL"

log "备份文件：$FILE"
log "数据库：$DB"
log "现场快照：$SNAPSHOT"

# ---- 铁律：先 verify，不过就拒绝 ----
if ! bash "$VERIFY_CMD" verify "$FILE"; then
  err_log "备份校验未通过，拒绝恢复（未修改任何文件）"
  exit 1
fi

# ---------------- 计划 ----------------
row_counts "$DB" "当前数据库行数（恢复前）"
echo
log "将要执行的步骤（共 5 步）："
printf '  1) 停容器        cd %s && %s -p %s stop %s\n' "$COMPOSE_DIR" "$COMPOSE_CMD" "$PROJECT" "$SERVICE"
printf '  2) 留存现场      %s（VACUUM INTO 单文件快照）\n' "$SNAPSHOT"
printf '  3) 覆盖数据库    rm -f %s-wal %s-shm；备份 -> %s（先写 .restoring 再 mv）\n' "$DB" "$DB" "$DB"
printf '  4) 起容器+等就绪 cd %s && %s -p %s up -d %s；轮询 %s（超时 %ss）\n' \
  "$COMPOSE_DIR" "$COMPOSE_CMD" "$PROJECT" "$SERVICE" "$HEALTH_URL" "$HEALTH_TIMEOUT"
printf '  5) 复核行数      tasks / users / reports / routines\n'
echo

if [ "$CONFIRM" -ne 1 ]; then
  log "DRY-RUN：以上为将要执行的步骤，未修改任何文件。"
  log "确认执行请加 --yes：scripts/restore.sh '$FILE' --yes"
  exit 0
fi

# ---------------- 真执行 ----------------
compose_run() {
  ( cd "$COMPOSE_DIR" && $COMPOSE_CMD -p "$PROJECT" "$@" )
}

mkdir -p "$BACKUP_DIR" || die "无法创建备份目录：$BACKUP_DIR"
mkdir -p "$DATA_DIR" || die "无法创建数据目录：$DATA_DIR"

log "步骤 1/5：停止容器（$SERVICE）"
compose_run stop "$SERVICE" || die "停止容器失败（COMPOSE_DIR=$COMPOSE_DIR / PROJECT=$PROJECT 是否正确？）"

log "步骤 2/5：留存现场 -> $SNAPSHOT"
if [ -f "$DB" ]; then
  sql_vacuum_into "$DB" "$SNAPSHOT" \
    || die "无法为当前库建立回退点（VACUUM INTO 失败），已中止，未修改任何文件"
  [ -s "$SNAPSHOT" ] || die "回退点文件为空：$SNAPSHOT，已中止，未修改任何文件"
  log "  现场快照完成"
else
  SNAPSHOT=""
  log "  当前没有 $DB，跳过现场快照（恢复后是一份全新的库）"
fi
CHANGED=1

log "步骤 3/5：覆盖数据库"
rm -f -- "$DB-wal" "$DB-shm" || die "无法删除 WAL/SHM 残留"
cp -- "$FILE" "$DB.restoring" || die "复制备份文件失败"
mv -f -- "$DB.restoring" "$DB" || die "替换 $DB 失败"
if command -v id >/dev/null 2>&1 && command -v chown >/dev/null 2>&1 \
   && [ "$(id -u)" = "0" ]; then
  if chown "$OWNER" "$DB" 2>/dev/null; then
    log "  已 chown $OWNER $DB"
  else
    log "  WARN: chown $OWNER 失败，请手工执行"
  fi
fi
log "  已覆盖：$DB"

log "步骤 4/5：启动容器并等待 healthz"
compose_run up -d "$SERVICE" || { err_log "启动容器失败"; rollback_guide; exit 1; }
if ! wait_health; then
  err_log "${HEALTH_TIMEOUT}s 内 $HEALTH_URL 未返回 200"
  err_log "现场已保留：容器仍在运行、恢复出来的库未删除、回退点可回滚"
  rollback_guide
  exit 1
fi

log "步骤 5/5：复核行数"
row_counts "$DB" "恢复后行数"
echo
log "恢复完成：$FILE -> $DB"
if [ -n "$SNAPSHOT" ]; then
  log "回退点：$SNAPSHOT（反悔就照上面的手工回退指引做）"
fi
