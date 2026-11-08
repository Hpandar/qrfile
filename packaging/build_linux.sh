#!/usr/bin/env bash
# 在 Linux 上构建独立可执行文件。
#
# PyInstaller **不能跨平台交叉编译**：Windows 的 exe 必须在 Windows 上打，
# Linux 的 ELF 必须在 Linux 上打。这个脚本负责后者。
#
# 用法：
#   bash packaging/build_linux.sh             # 单文件版 dist/qrfile（CLI）
#   bash packaging/build_linux.sh --gui       # 单文件版 dist/qrfile-gui
#   bash packaging/build_linux.sh --all       # 两个都打（推荐）
#   bash packaging/build_linux.sh --onedir    # 目录版
#   bash packaging/build_linux.sh --no-verify # 跳过构建后的自检
#
# 建议在**目标发行版或更老的发行版**上构建 —— PyInstaller 产物依赖构建机的 glibc。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

BUILD_GUI=0
BUILD_CLI=1
for arg in "$@"; do
  case "$arg" in
    --gui) BUILD_GUI=1; BUILD_CLI=0 ;;
    --all) BUILD_GUI=1; BUILD_CLI=1 ;;
  esac
done

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "找不到 $PYTHON，请先安装 Python 3.11+" >&2
  exit 1
fi

# 播放器需要 tkinter。没有它，CLI 的 send/push 打不开全屏窗口。
# 已有 .venv 时以它的解释器为准（它可能和 $PYTHON 不是同一个）。
CHECK_PYTHON="$PYTHON"
if [ -x .venv/bin/python ]; then
  CHECK_PYTHON=".venv/bin/python"
fi
if [ "$BUILD_CLI" = 1 ] && ! "$CHECK_PYTHON" -c "import tkinter" >/dev/null 2>&1; then
  cat >&2 <<'EOF'
缺少 tkinter（全屏播放器需要）。按发行版安装：
  Debian/Ubuntu: sudo apt install python3-tk
  Fedora/RHEL:   sudo dnf install python3-tkinter
  Arch:          sudo pacman -S tk
  openSUSE:      sudo zypper install python3-tk
EOF
  exit 1
fi

if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip
if [ "$BUILD_GUI" = 1 ]; then
  python -m pip install -e '.[gui]' pyinstaller
else
  python -m pip install -e . pyinstaller
fi

cat <<'EOF'
说明：
  * X11 抓屏由 mss 负责（已作为依赖安装），窗口定位可选安装 wmctrl 或 xdotool；
  * Wayland 抓屏需要 grim：sudo apt install grim；
  * 无图形界面的服务器上，send 请用 --dump-pages，decode 不受影响；
  * GUI 产物必须和 CLI 产物放在同一目录（GUI 会调用同目录的 qrfile）。

EOF

exec python packaging/build.py "$@"
