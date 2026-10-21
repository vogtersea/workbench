#!/usr/bin/env bash
# ============================================================
# 工作台项目 · SQLite 在线备份 + 备份校验（SPEC.md 第 8 节 / W-12）
#
# 用法：
#   scripts/backup.sh                        # 默认备份 ./data/app.db -> ./backup/
#   DATA_DIR=/volume1/workbench/data scripts/backup.sh
#   BACKUP_DIR=/volume1/workbench/backup KEEP=7 scripts/backup.sh
#   scripts/backup.sh verify <备份文件>       # 校验一份备份能不能用来恢复
#
# 产出：backup/app-YYYYmmdd-HHMMSS.db，默认保留最近 14 份。
# 依赖：sqlite3 命令行（Docker 镜像内已装；NAS 上用包管理器装 sqlite3）。
#       没有 sqlite3 命令时自动回退到 python3 的 stdlib sqlite3（同一个引擎）。
# 退出码：0 = 通过；非 0 = 失败（原因同时打在 stderr）。
# ============================================================
set -euo pipefail

DATA_DIR="${DATA_DIR:-./data}"
BACKUP_DIR="${BACKUP_DIR:-./backup}"
KEEP="${KEEP:-14}"
DB="${DB_PATH:-$DATA_DIR/app.db}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
err_log() { printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }
die() { err_log "$*"; exit 1; }

usage() {
  cat <<'USAGE'
用法：
  scripts/backup.sh                    备份（环境变量覆盖参数）
  scripts/backup.sh verify <备份文件>   校验备份：文件头 / PRAGMA integrity_check / 关键表行数
  scripts/backup.sh --help             显示本帮助

环境变量：
  DATA_DIR    数据库目录（默认 ./data）
  DB_PATH     数据库文件（默认 $DATA_DIR/app.db）
  BACKUP_DIR  备份目录（默认 ./backup）
  KEEP        备份保留份数（默认 14）
  PYTHON      指定 python 解释器（没有 sqlite3 命令行时使用）
USAGE
}

# ---------------- SQLite 执行后端 ----------------
# NAS / 容器里有 sqlite3 命令行；Windows 或精简环境通常只有 python。
# 两条路径调的是同一个 SQLite 引擎，integrity_check 结果一致。
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

# sql_rows <库文件> <单条 SQL>：逐行输出结果，列之间用 TAB 分隔（只读打开）
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

# sql_vacuum_into <源库> <目标文件>：单事务一致性快照，等价于 sqlite3 "VACUUM INTO"
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

# table_count <库文件> <表名>：表不存在输出 -1
table_count() {
  local exists
  exists="$(sql_rows "$1" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='$2';")"
  if [ "$exists" != "1" ]; then
    printf '%s\n' '-1'
    return 0
  fi
  sql_rows "$1" "SELECT COUNT(*) FROM $2;"
}

# ---------------- verify：校验一份备份 ----------------
VERIFY_TABLES="tasks users reports routines"
WORK_DIR=""

# 输出统一成 OK / FAIL 两态，避免中文与英文混排时 printf 的定宽对齐失效
vok() { printf '  OK    %s\n' "$*"; }
vfail() { printf '  FAIL  %s\n' "$*"; }

verify_backup() {
  local file="$1" size magic check detail t n missing=0

  log "校验备份：$file"

  if [ ! -e "$file" ]; then
    vfail "文件存在（路径不存在）"
    err_log "备份文件不存在：$file"
    return 1
  fi
  if [ ! -f "$file" ]; then
    vfail "文件存在（不是普通文件）"
    err_log "不是普通文件：$file"
    return 1
  fi
  size="$(wc -c < "$file" | tr -d ' ')"
  if [ "${size:-0}" -le 0 ]; then
    vfail "文件存在且非空（0 字节）"
    err_log "备份文件是空的：$file"
    return 1
  fi
  vok "文件存在且非空（$size 字节）"

  magic="$(head -c 16 "$file" | tr -d '\000\n')"
  if [ "$magic" != "SQLite format 3" ]; then
    vfail "SQLite 文件头（前 16 字节不是 SQLite format 3，文件可能被截断或不是数据库）"
    err_log "SQLite 文件头校验失败：$file"
    return 1
  fi
  vok "SQLite 文件头"

  # 复制到临时目录再打开：备份原件全程只读，校验过程产生的 -wal/-shm 不落在备份目录。
  WORK_DIR="$(mktemp -d)" || die "无法创建临时目录"
  trap 'rm -rf "${WORK_DIR:-}"' EXIT
  cp -- "$file" "$WORK_DIR/verify.db" || die "无法复制备份到临时目录：$WORK_DIR"

  check="$(sql_rows "$WORK_DIR/verify.db" 'PRAGMA integrity_check;' 2>&1 || true)"
  if [ "$check" != "ok" ]; then
    # 出错时 sqlite 可能返回一行错误说明，python 后端则是 traceback：优先取含 Error 的那行
    detail="$(printf '%s' "$check" | grep -i -m1 'error' || printf '%s' "$check" | head -1)"
    [ -n "$detail" ] || detail="integrity_check 未返回 ok"
    vfail "PRAGMA integrity_check（$detail）"
    err_log "integrity_check 未通过：$file"
    return 1
  fi
  vok "PRAGMA integrity_check"

  printf '  关键表行数：\n'
  for t in $VERIFY_TABLES; do
    n="$(table_count "$WORK_DIR/verify.db" "$t" 2>/dev/null || printf '%s' '-1')"
    if [ "$n" = "-1" ]; then
      printf '    %-9s %s\n' "$t" "缺失（FAIL）"
      missing=1
    else
      printf '    %-9s %s 行\n' "$t" "$n"
    fi
  done

  if [ "$missing" -ne 0 ]; then
    err_log "关键表缺失（期望 $VERIFY_TABLES）：$file 不能用来恢复"
    return 1
  fi

  log "校验通过：$file"
  return 0
}

# ---------------- 主流程 ----------------
COMMAND="backup"
if [ "$#" -gt 0 ]; then
  case "$1" in
    verify) COMMAND="verify"; shift ;;
    -h|--help|help) usage; exit 0 ;;
    *) usage >&2; die "未知参数：$1" ;;
  esac
fi

if [ "$COMMAND" = "verify" ]; then
  [ "$#" -eq 1 ] || { usage >&2; die "用法：scripts/backup.sh verify <备份文件>"; }
  detect_sql || die "需要 sqlite3 命令行，或可用的 python3（含 sqlite3 模块）"
  if verify_backup "$1"; then
    exit 0
  else
    exit 1
  fi
fi

case "$KEEP" in
  ''|*[!0-9]*) die "KEEP 必须是正整数，当前值：$KEEP" ;;
esac
[ "$KEEP" -ge 1 ] || die "KEEP 必须 >= 1，当前值：$KEEP"

detect_sql \
  || die "未找到 sqlite3 命令。Debian/Ubuntu: apt-get install -y sqlite3；群晖: 套件中心装 SQLite3；或直接用容器内的 sqlite3（也可装 python3 后自动回退）。"
[ -f "$DB" ] || die "数据库不存在：$DB（先用 uvicorn 启动一次，或检查 DATA_DIR 是否正确）"
mkdir -p "$BACKUP_DIR" || die "无法创建备份目录：$BACKUP_DIR"

# ---- 关于 sleep ----
#   VACUUM INTO 在单个读事务里完成，产出的是事务一致快照；应用正在写入时
#   也不需要停服或停写，所以默认不 sleep。
#   若 NAS 磁盘很慢，或希望「刚刚发生的写入」一定包含在快照里，可设
#   BACKUP_PRE_SLEEP=2，脚本会先等待 1~2 秒再抓快照。定时备份建议错开
#   每日提醒扫描时刻（默认 09:00），避免与写事务高峰重叠。
PRE_SLEEP="${BACKUP_PRE_SLEEP:-0}"
if [ "$PRE_SLEEP" != "0" ]; then
  log "BACKUP_PRE_SLEEP=$PRE_SLEEP，等待写入静默…"
  sleep "$PRE_SLEEP"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/app-$STAMP.db"

log "备份 $DB -> $OUT"
if ! sql_vacuum_into "$DB" "$OUT"; then
  rm -f -- "$OUT"
  die "VACUUM INTO 失败（数据库可能被独占锁定，稍后重试）"
fi
[ -s "$OUT" ] || { rm -f -- "$OUT"; die "备份文件为空：$OUT"; }

CHECK="$(sql_rows "$OUT" 'PRAGMA integrity_check;' 2>/dev/null || echo 'check-failed')"
[ "$CHECK" = "ok" ] || log "WARN: 备份完整性检查返回 $CHECK（请人工确认；可用 verify 子命令复查）"

# ---- 轮转：只保留最近 KEEP 份（按修改时间倒序） ----
# 只轮转 app-<数字时间戳>.db：restore.sh 产出的 app-before-restore-*.db 是救命快照，
# 以及人工留的 app-truncated.db 之类都必须永不自动删除。
removed=0
if ls -1t "$BACKUP_DIR"/app-[0-9]*.db >/dev/null 2>&1; then
  index=0
  while IFS= read -r old; do
    index=$((index + 1))
    if [ "$index" -gt "$KEEP" ]; then
      rm -f -- "$old" && log "清理过期备份：$old"
      removed=$((removed + 1))
    fi
  done < <(ls -1t "$BACKUP_DIR"/app-[0-9]*.db)
fi

SIZE="$(wc -c < "$OUT" | tr -d ' ')"
log "完成：$OUT（$SIZE 字节，integrity=$CHECK，保留最近 $KEEP 份，本次清理 $removed 份）"
log "建议紧接着验证一次：scripts/backup.sh verify $OUT"
