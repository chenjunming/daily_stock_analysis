#!/bin/bash
# ===================================
# WebUI 启动脚本
# ===================================
# 启动本地 WebUI 面板

set -e

cd "$(dirname "$0")"

echo "📊 启动股票分析系统 WebUI..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "✓ 项目位置: $(pwd)"
echo "✓ Python 版本: $(python3 --version)"
echo ""

# 读取配置
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
    echo "✓ 已加载配置文件: .env"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🚀 WebUI 启动中..."
echo ""
echo "📝 访问地址: http://${WEBUI_HOST:-127.0.0.1}:${WEBUI_PORT:-8000}"
echo ""
echo "📌 常用功能："
echo "   • 配置管理 - 编辑自选股和 API Key"
echo "   • 手动分析 - 点击\"开始分析\"按钮执行分析"
echo "   • 查看结果 - 实时查看分析报告"
echo "   • 定时任务 - 设置每日定时执行"
echo ""
echo "💡 提示："
echo "   • 首次运行请先配置 Gemini API Key"
echo "   • 然后配置飞书 Webhook URL 用于接收推送通知"
echo "   • 如需停止服务，按 Ctrl+C"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 启动 WebUI
python3 main.py --webui-only
