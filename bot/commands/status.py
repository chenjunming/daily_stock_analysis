# -*- coding: utf-8 -*-
"""
===================================
状态命令
===================================

显示系统运行状态和配置信息。
"""

import platform
import json
import re
import sys
from datetime import datetime
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.storage import get_db
from src.analyzer import STOCK_NAME_MAP
from src.time_utils import utc8_now


class StatusCommand(BotCommand):
    """
    状态命令
    
    显示系统运行状态，包括：
    - 服务状态
    - 配置信息
    - 可用功能
    """
    
    @property
    def name(self) -> str:
        return "status"
    
    @property
    def aliases(self) -> List[str]:
        return ["s", "状态", "info"]
    
    @property
    def description(self) -> str:
        return "显示系统状态"
    
    @property
    def usage(self) -> str:
        return "/status"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行状态命令"""
        from src.config import get_config
        
        config = get_config()
        
        # 收集状态信息
        status_info = self._collect_status(config)
        
        # 格式化输出
        text = self._format_status(status_info, message.platform)
        
        return BotResponse.markdown_response(text)
    
    def _collect_status(self, config) -> dict:
        """收集系统状态信息"""
        db = get_db()
        # 先对历史“名称=代码”的记录做一次落库补全，确保后续展示可复用
        try:
            db.backfill_watchlist_names()
        except Exception:
            pass

        grouped_names = {"CN": [], "HK": [], "US": []}

        # 优先使用 WebUI 数据库自选股（包含名称与市场）
        watchlist_items = db.get_watchlist()
        if watchlist_items:
            # 对“名称缺失或等于代码”的标的进行批量补全
            to_resolve_codes = []
            code_market_map = {}
            for item in watchlist_items:
                code = (getattr(item, "code", "") or "").strip().upper()
                market = (getattr(item, "market", "CN") or "CN").upper()
                name = (getattr(item, "name", "") or "").strip()
                if not code:
                    continue
                if not name or name.upper() == code:
                    # HK 代码在某些数据源中可能需要去掉 HK 前缀
                    query_code = code[2:] if code.startswith("HK") and len(code) == 7 else code
                    to_resolve_codes.append(query_code)
                    code_market_map[query_code] = market

            resolved_map = {}
            for c in to_resolve_codes:
                name_guess = STOCK_NAME_MAP.get(c) or ""
                if (not name_guess) and re.match(r'^\d{5}$', c):
                    name_guess = STOCK_NAME_MAP.get(f"HK{c}") or ""
                resolved_map[c] = name_guess

            # 数据源仍无法识别时：一次性批量交给 AI 推断
            unresolved_codes = [c for c in to_resolve_codes if c not in resolved_map]
            if unresolved_codes:
                ai_map = self._resolve_names_with_ai_batch(
                    unresolved_codes=unresolved_codes,
                    code_market_map=code_market_map,
                    config=config,
                )
                resolved_map.update(ai_map)

            # 将本次解析出的名称回写数据库，后续不再重复查询
            for item in watchlist_items:
                code = (getattr(item, "code", "") or "").strip().upper()
                name = (getattr(item, "name", "") or "").strip()
                if not code:
                    continue
                if name and name.upper() != code:
                    continue
                query_code = code[2:] if code.startswith("HK") and len(code) == 7 else code
                resolved_name = (
                    resolved_map.get(query_code)
                    or STOCK_NAME_MAP.get(code)
                    or STOCK_NAME_MAP.get(query_code)
                )
                if resolved_name and resolved_name.upper() != code:
                    try:
                        db.update_watchlist_name(code, resolved_name)
                    except Exception:
                        pass

            for item in watchlist_items:
                market = (getattr(item, "market", "CN") or "CN").upper()
                name = (getattr(item, "name", "") or "").strip()
                code = (getattr(item, "code", "") or "").strip().upper()
                query_code = code[2:] if code.startswith("HK") and len(code) == 7 else code
                display_name = (
                    name
                    if name and name.upper() != code
                    else resolved_map.get(query_code)
                    or STOCK_NAME_MAP.get(code)
                    or STOCK_NAME_MAP.get(query_code)
                    or code
                )
                if market not in grouped_names:
                    grouped_names[market] = []
                grouped_names[market].append(display_name)
        else:
            # 回退到 .env 中的 STOCK_LIST
            for code in config.stock_list:
                code = (code or "").strip().upper()
                if not code:
                    continue
                if code.startswith("HK") and len(code) == 7:
                    market = "HK"
                elif code.isdigit() and len(code) == 6:
                    market = "CN"
                else:
                    market = "US"
                if market not in grouped_names:
                    grouped_names[market] = []
                grouped_names[market].append(STOCK_NAME_MAP.get(code, code))

        stock_count = sum(len(v) for v in grouped_names.values())

        status = {
            "timestamp": utc8_now().strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system(),
            "stock_count": stock_count,
            "stock_grouped_names": grouped_names,
        }
        
        # AI 配置状态
        status["ai_gemini"] = bool(config.gemini_api_key)
        status["ai_openai"] = bool(config.openai_api_key)
        
        # 搜索服务状态
        status["search_bocha"] = len(config.bocha_api_keys) > 0
        status["search_tavily"] = len(config.tavily_api_keys) > 0
        status["search_serpapi"] = len(config.serpapi_keys) > 0
        
        # 通知渠道状态
        status["notify_wechat"] = bool(config.wechat_webhook_url)
        status["notify_feishu"] = bool(config.feishu_webhook_url)
        status["notify_telegram"] = bool(config.telegram_bot_token and config.telegram_chat_id)
        status["notify_email"] = bool(config.email_sender and config.email_password)
        
        return status

    @staticmethod
    def _resolve_names_with_ai_batch(unresolved_codes: List[str], code_market_map: dict, config) -> dict:
        """
        使用 AI 一次性批量推断代码对应名称（兜底方案）。
        返回 {code: name}
        """
        if not unresolved_codes:
            return {}
        if not (getattr(config, "gemini_api_key", None) or getattr(config, "openai_api_key", None)):
            return {}

        # 控制一次请求规模，避免提示过长
        codes = unresolved_codes[:60]
        lines = [f"{c} ({code_market_map.get(c, 'CN')})" for c in codes]
        prompt = (
            "你是证券代码映射助手。请根据常见中港美市场代码，返回代码对应的股票简称。\n"
            "只返回 JSON 对象，key 是代码，value 是中文或英文简称。\n"
            "如果不确定某个代码，value 填空字符串。\n\n"
            "待识别代码列表：\n"
            + "\n".join(lines)
            + "\n\n输出示例：{\"600519\":\"贵州茅台\",\"AAPL\":\"苹果\",\"159637\":\"\"}"
        )

        try:
            from src.analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer()
            if not analyzer.is_available():
                return {}

            out = analyzer._call_api_with_retry(
                prompt,
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 1200,
                }
            )
            json_text = out.strip()
            m = re.search(r"\{[\s\S]*\}", json_text)
            if m:
                json_text = m.group(0)
            data = json.loads(json_text)
            if not isinstance(data, dict):
                return {}

            result = {}
            for c in codes:
                name = str(data.get(c, "") or "").strip()
                if name and name.upper() != c.upper():
                    result[c] = name
            return result
        except Exception:
            return {}
    
    def _format_status(self, status: dict, platform: str) -> str:
        """格式化状态信息"""
        # 状态图标
        def icon(enabled: bool) -> str:
            return "✅" if enabled else "❌"
        
        lines = [
            "📊 **股票分析助手 - 系统状态**",
            "",
            f"🕐 时间: {status['timestamp']}",
            f"🐍 Python: {status['python_version']}",
            f"💻 平台: {status['platform']}",
            "",
            "---",
            "",
            "**📈 自选股配置**",
            f"• 股票数量: {status['stock_count']} 只",
        ]

        grouped = status.get("stock_grouped_names", {})
        market_labels = {"CN": "A股", "HK": "港股", "US": "美股"}
        for market in ["CN", "HK", "US"]:
            names = grouped.get(market, []) or []
            if not names:
                continue
            preview = ", ".join(names[:8])
            if len(names) > 8:
                preview += f" ... 等 {len(names)} 只"
            lines.append(f"• {market_labels.get(market, market)}: {preview}")
        
        lines.extend([
            "",
            "**🤖 AI 分析服务**",
            f"• Gemini API: {icon(status['ai_gemini'])}",
            f"• OpenAI API: {icon(status['ai_openai'])}",
            "",
            "**🔍 搜索服务**",
            f"• Bocha: {icon(status['search_bocha'])}",
            f"• Tavily: {icon(status['search_tavily'])}",
            f"• SerpAPI: {icon(status['search_serpapi'])}",
            "",
            "**📢 通知渠道**",
            f"• 企业微信: {icon(status['notify_wechat'])}",
            f"• 飞书: {icon(status['notify_feishu'])}",
            f"• Telegram: {icon(status['notify_telegram'])}",
            f"• 邮件: {icon(status['notify_email'])}",
        ])
        
        # AI 服务总体状态
        ai_available = status['ai_gemini'] or status['ai_openai']
        if ai_available:
            lines.extend([
                "",
                "---",
                "✅ **系统就绪，可以开始分析！**",
            ])
        else:
            lines.extend([
                "",
                "---",
                "⚠️ **AI 服务未配置，分析功能不可用**",
                "请配置 Gemini 或 OpenAI API Key",
            ])
        
        return "\n".join(lines)
