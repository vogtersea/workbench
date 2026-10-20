#!/usr/bin/env bash
# ============================================================
# 工作台项目 · SQLite 在线备份（SPEC.md 第 8 节）
#
# 用法：
#   scripts/backup.sh                        # 默认备份 ./data/app.db -> ./backup/
#   DATA_DIR=/volume1/workbench/data scripts/backup.sh
#   BACKUP_DIR=/volume1/workbench/backup KEEP=7 scripts/backup.sh
#
# 产出：backup/app-YYYYmmdd-HHMMSS.db，默认保留最近 14 份。
# 依赖：sqlite3 命令行（Docker 镜像内已装；NAS 上用包管理器装 sqlite3）。
# ============================================================
set -euo pipefail

DATA_DIR="${DATA_DIR:-./data}"
BACKUP_DIR="${BACKUP_DIR:-./backup}"
KEEP="${KEEP:-14}"
DB="${DB_PATH:-$DATA_DIR/app.db}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BACKUP_DIR/app-$STAMP.db"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; exit 1; }

case "$KEEP" in
  ''|*[!0-9]*) die "KEEP 必须是正整数，当前值：$KEEP" ;;
esac
[ "$KEEP" -ge 1 ] || die "KEEP 必须 >= 1，当前值：$KEEP"

command -v sqlite3 >/dev/null 2>&1 \
  || die "未找到 sqlite3 命令。Debian/Ubuntu: apt-get install -y sqlite3；群晖: 套件中心装 SQLite3；或直接用容器内的 sqlite3。"
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

log "备份 $DB -> $OUT"
if ! sqlite3 "$DB" "VACUUM INTO '$OUT'"; then
  rm -f -- "$OUT"
  die "VACUUM INTO 失败（数据库可能被独占锁定，稍后重试）"
fi
[ -s "$OUT" ] || { rm -f -- "$OUT"; die "备份文件为空：$OUT"; }

CHECK="$(sqlite3 "$OUT" 'PRAGMA integrity_check;' 2>/dev/null || echo 'check-failed')"
[ "$CHECK" = "ok" ] || log "WARN: 备份完整性检查返回 $CHECK（请人工确认）"

# ---- 轮转：只保留最近 KEEP 份（按修改时间倒序） ----
removed=0
if ls -1t "$BACKUP_DIR"/app-*.db >/dev/null 2>&1; then
  index=0
  while IFS= read -r old; do
    index=$((index + 1))
    if [ "$index" -gt "$KEEP" ]; then
      rm -f -- "$old" && log "清理过期备份：$old"
      removed=$((removed + 1))
    fi
  done < <(ls -1t "$BACKUP_DIR"/app-*.db)
fi

SIZE="$(wc -c < "$OUT" | tr -d ' ')"
log "完成：$OUT（$SIZE 字节，integrity=$CHECK，保留最近 $KEEP 份，本次清理 $removed 份）"
