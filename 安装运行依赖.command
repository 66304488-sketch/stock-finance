#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
if [[ -f "$SCRIPT_DIR/setup.sh" ]]; then
  exec /bin/zsh "$SCRIPT_DIR/setup.sh"
fi

APP_SETUP="/Applications/行业热力图.app/Contents/Resources/setup.sh"
if [[ -f "$APP_SETUP" ]]; then
  exec /bin/zsh "$APP_SETUP"
fi

echo "未找到 setup.sh。请先将“行业热力图”拖入 Applications，再重试。"
read -k 1 "?按任意键关闭..."
