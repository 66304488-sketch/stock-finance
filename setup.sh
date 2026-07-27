#!/bin/bash
# 行业热力图 Python 环境一键安装脚本
# 双击运行或在终端执行: bash setup.sh

set -e
set -u
set -o pipefail

pause_for_user() {
    printf "按回车键退出..."
    read -r _answer
}

echo "========================================="
echo "  行业热力图 - Python 环境安装"
echo "========================================="
echo ""

# 与桌面端保持相同优先级：优先使用已经具备核心依赖的解释器；
# 若都未安装依赖，则给优先级最高的可执行解释器安装。
PYTHON=""
FIRST_PYTHON=""
for cmd in \
    /opt/anaconda3/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    python3 python; do
    RESOLVED=""
    if [[ "$cmd" == /* ]]; then
        [[ -x "$cmd" ]] && RESOLVED="$cmd"
    else
        RESOLVED="$(command -v "$cmd" 2>/dev/null || true)"
    fi
    if [[ -n "$RESOLVED" ]]; then
        [[ -z "$FIRST_PYTHON" ]] && FIRST_PYTHON="$RESOLVED"
        if "$RESOLVED" -c "import fastapi, uvicorn, akshare, pandas, requests, httpx, openpyxl, baostock; from akshare.stock.stock_share_changes_cninfo import py_mini_racer" >/dev/null 2>&1; then
            PYTHON="$RESOLVED"
            break
        fi
    fi
done
[[ -z "$PYTHON" ]] && PYTHON="$FIRST_PYTHON"

if [ -z "$PYTHON" ]; then
    echo "❌ 未找到 Python3，请先安装 Python:"
    echo "   brew install python3"
    echo "   或从 https://www.python.org/downloads/ 下载"
    echo ""
    pause_for_user
    exit 1
fi

echo "✅ 找到 Python: $($PYTHON --version)"
echo "   $PYTHON"
echo ""

LOG_FILE="$(mktemp -t stock-finance-setup.XXXXXX)"
trap 'rm -f "$LOG_FILE"' EXIT

# 核心依赖（热力图/资金流向/市值功能）
echo "📦 安装核心依赖..."
if ! "$PYTHON" -m pip install --upgrade pip -q >"$LOG_FILE" 2>&1; then
    tail -20 "$LOG_FILE"
    echo "❌ pip 升级失败，请检查网络和 Python 写入权限。"
    pause_for_user
    exit 1
fi
if ! "$PYTHON" -m pip install \
    fastapi uvicorn \
    akshare mini-racer pandas openpyxl \
    baostock \
    requests httpx \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    >"$LOG_FILE" 2>&1; then
    tail -20 "$LOG_FILE"
    echo "❌ 核心依赖安装失败，软件尚不能运行。"
    pause_for_user
    exit 1
fi
tail -3 "$LOG_FILE"

if ! "$PYTHON" -c "import fastapi, uvicorn, akshare, pandas, requests, httpx, openpyxl, baostock; from akshare.stock.stock_share_changes_cninfo import py_mini_racer" >/dev/null 2>&1; then
    echo "❌ 安装完成后依赖校验仍未通过。"
    pause_for_user
    exit 1
fi

# 可选依赖（AI 功能）
echo ""
echo "📦 安装 AI 依赖（可选，用于 AI 日报/问答）..."
if ! "$PYTHON" -m pip install \
    anthropic openai \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    >"$LOG_FILE" 2>&1; then
    tail -10 "$LOG_FILE"
    echo "⚠️ AI 可选依赖安装失败，不影响行情和策略功能。"
else
    tail -2 "$LOG_FILE"
fi

echo ""
echo "========================================="
echo "  ✅ 安装完成！"
echo "  现在可以打开「行业热力图.app」了"
echo "========================================="
echo ""
pause_for_user
