#!/bin/bash
# 行业热力图 Python 环境一键安装脚本
# 双击运行或在终端执行: bash setup.sh

echo "========================================="
echo "  行业热力图 - Python 环境安装"
echo "========================================="
echo ""

# 查找 Python
PYTHON=""
for cmd in python3 python; do
    if command -v $cmd &>/dev/null; then
        PYTHON=$cmd
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ 未找到 Python3，请先安装 Python:"
    echo "   brew install python3"
    echo "   或从 https://www.python.org/downloads/ 下载"
    echo ""
    read -p "按回车键退出..."
    exit 1
fi

echo "✅ 找到 Python: $($PYTHON --version)"
echo ""

# 核心依赖（热力图/资金流向/市值功能）
echo "📦 安装核心依赖..."
$PYTHON -m pip install --upgrade pip -q
$PYTHON -m pip install \
    fastapi uvicorn \
    akshare pandas openpyxl \
    baostock \
    requests httpx \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    2>&1 | tail -3

# 可选依赖（AI 功能）
echo ""
echo "📦 安装 AI 依赖（可选，用于 AI 日报/问答）..."
$PYTHON -m pip install \
    anthropic openai \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    2>&1 | tail -2

echo ""
echo "========================================="
echo "  ✅ 安装完成！"
echo "  现在可以打开「行业热力图.app」了"
echo "========================================="
echo ""
read -p "按回车键退出..."
