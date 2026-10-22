#!/usr/bin/env bash
# 构建 Android APK：同步 app/ 到 Android 工程 -> Gradle 构建 -> 复制产物到 dist/。
#
# 前置（一次性，见 android/README.md）：JDK 17、Android SDK(platforms;android-34,
# build-tools;34.0.0)、Gradle 8.7。环境变量：ANDROID_HOME、JAVA_HOME，可选 GRADLE。
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ANDROID_HOME:?请设置 ANDROID_HOME 指向 Android SDK}"
GRADLE="${GRADLE:-gradle}"

echo "== 1/4 同步 app/ 到 Android 工程（唯一代码来源）=="
rm -rf android/app/src/main/python/app
cp -r app android/app/src/main/python/app
find android/app/src/main/python/app -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "== 2/4 Gradle 构建 =="
(cd android && "$GRADLE" --no-daemon assembleDebug)

echo "== 3/4 复制产物到 dist/ =="
mkdir -p dist
VERSION="$(.venv/Scripts/python.exe -c "import app.brand as b; print(b.VERSION)" 2>/dev/null || echo 0)"
APK="dist/worklog-${VERSION}-beta-debug.apk"
cp android/app/build/outputs/apk/debug/app-beta-debug.apk "$APK"
ls -lh "$APK"

echo "== 4/4 完成 =="
echo "APK: $APK"
echo "注意：debug 签名，仅供自用；正式分发请自行配置 release keystore。"
