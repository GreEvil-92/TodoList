#!/usr/bin/env bash
# 安卓 APK 构建脚本（必须在 Linux 上运行，Windows 请用 WSL2 / Docker）
#
# 用法：
#   bash scripts/build_android.sh            # 构建 debug APK
#   bash scripts/build_android.sh release    # 构建 release APK（需先配置签名）
#
# 为什么需要这个脚本：
#   buildozer 只读取「当前目录」下的 buildozer.spec，且 source.dir 写的是 .，
#   所以 spec 必须位于项目根目录；而配置源文件维护在 scripts/config/ 下。
#   脚本负责把配置同步到根目录，再调用 buildozer。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "$(uname -s)" != "Linux" ]; then
    echo "✗ buildozer 只能在 Linux 上运行。Windows 请先在 WSL2(Ubuntu) 或 Docker 里执行。" >&2
    exit 1
fi

if ! command -v buildozer >/dev/null 2>&1; then
    echo "✗ 未安装 buildozer。请先执行：" >&2
    echo "    pip3 install --user buildozer cython==0.29.36" >&2
    exit 1
fi

# 同步构建配置到项目根目录（buildozer 只读当前目录下的 buildozer.spec，
# 且 source.dir 为 .；manifest 补丁在 spec 里已按相对根目录的路径引用，无需复制）
cp -f scripts/config/buildozer.spec ./buildozer.spec

TARGET="${1:-debug}"
if [ "$TARGET" = "release" ]; then
    BUILD_CMD="buildozer -v android release"
else
    BUILD_CMD="buildozer -v android debug"
fi

echo "▶ 开始构建（首次会下载 Android SDK/NDK，约 3-6 GB，耗时 30-90 分钟）"
echo "  命令：$BUILD_CMD"
# shellcheck disable=SC2086
$BUILD_CMD

echo
echo "✓ 构建完成，产物："
ls -1 bin/*.apk 2>/dev/null || echo "  （未找到 apk，请检查上方日志）"
echo
echo "安装到手机：adb install -r bin/<你的apk>.apk"
