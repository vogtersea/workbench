#!/usr/bin/env bash
# 安装 pre-commit 钩子：每次提交前扫描暂存区，命中敏感词就拦下。
# 用法：bash scripts/install_privacy_hook.sh
set -euo pipefail

cd "$(dirname "$0")/.."
HOOK=".git/hooks/pre-commit"

if [ ! -d .git ]; then
  echo "当前目录不是 git 仓库"
  exit 1
fi

cat > "$HOOK" <<'HOOK_EOF'
#!/bin/sh
# 由 scripts/install_privacy_hook.sh 生成：提交前扫描暂存区的敏感词
PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY="python"
"$PY" scripts/privacy_scan.py --staged --quiet || {
  echo ""
  echo "提交已拦截：暂存区含敏感内容。"
  echo "确认无误可绕过：git commit --no-verify"
  exit 1
}
HOOK_EOF

chmod +x "$HOOK"
echo "已安装：$HOOK"
echo "试运行一次全量扫描："
.venv/Scripts/python.exe scripts/privacy_scan.py || true
