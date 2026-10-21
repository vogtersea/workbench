#!/usr/bin/env bash
# 构建 Windows 桌面版：PyInstaller onedir -> dist/Workbench/，可选再用 Inno Setup 打安装包。
# 用法：bash scripts/build_desktop.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PY:-.venv/Scripts/python.exe}"

echo "== 1/5 检查桌面依赖 =="
"$PY" -c "import webview, pystray, PIL, winotify, PyInstaller" || {
  echo "缺少桌面依赖，请先运行： $PY -m pip install -r requirements-desktop.txt"
  exit 1
}

echo "== 2/5 生成图标 =="
"$PY" scripts/make_icon.py

echo "== 3/5 清理旧的构建产物 =="
rm -rf build/Workbench dist/Workbench
mkdir -p build

echo "== 4/5 PyInstaller 打包（onedir）=="
"$PY" -m PyInstaller --noconfirm --clean workbench-desktop.spec

if [ ! -x dist/Workbench/Workbench.exe ]; then
  echo "打包失败：dist/Workbench/Workbench.exe 不存在"
  exit 1
fi
echo "   产出：dist/Workbench/Workbench.exe"

echo "== 5/5 可选：打安装包（需要 Inno Setup 6 的 ISCC.exe）=="
ISCC="${ISCC:-}"
if [ -z "$ISCC" ]; then
  # 先问注册表（Inno Setup 可能装在任意盘/目录），再退回常见路径
  REG_LOC=$(reg query "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\Inno Setup 6_is1" //v InstallLocation 2>/dev/null | grep -i installlocation | sed 's/.*REG_SZ[[:space:]]*//' | tr -d '\r')
  if [ -n "$REG_LOC" ]; then
    WINPATH=$(cygpath -u "$REG_LOC" 2>/dev/null || echo "")
    [ -n "$WINPATH" ] && [ -x "$WINPATH/ISCC.exe" ] && ISCC="$WINPATH/ISCC.exe"
  fi
fi
if [ -z "$ISCC" ]; then
  for candidate in "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" "/c/Program Files/Inno Setup 6/ISCC.exe" "/d/Program Files (x86)/Inno Setup 6/ISCC.exe"; do
    [ -x "$candidate" ] && ISCC="$candidate" && break
  done
fi
if [ -n "$ISCC" ] && [ -x "$ISCC" ]; then
  "$ISCC" installer/workbench.iss
  echo "   安装包：dist/Workbench-Setup.exe"
else
  echo "   未找到 ISCC.exe（Inno Setup 6 未安装），跳过安装包。"
  echo "   安装后重跑本脚本即可出 dist/Workbench-Setup.exe；也可直接用 dist/Workbench/ 免安装目录。"
fi

echo "完成。"