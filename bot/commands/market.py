# -*- coding: utf-8 -*-
"""
===================================
大盘复盘命令
===================================

执行大盘复盘分析，生成市场概览报告。
"""

import logging
import threading
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse

logger = logging.getLogger(__name__)


class MarketCommand(BotCommand):
    """
    大盘复盘命令
    
    执行大盘复盘分析，包括：
    - 主要指数表现
    - 板块热点
    - 市场情绪
    - 后市展望
    
    用法：
        /market - 执行大盘复盘
    """

    @property
    def name(self) -> str:
        return "market"

    @property
    def aliases(self) -> List[str]:
        return ["m", "大盘", "复盘", "行情"]

    @property
    def description(self) -> str:
        return "大盘复盘分析"

    @property
    def usage(self) -> str:
        return "/market [CN|HK|US]"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行大盘复盘命令"""
        logger.info(f"[MarketCommand] 开始大盘复盘分析")
        market_hint = self._parse_market_hint(args)

        # 在后台线程中执行复盘（避免阻塞）
        thread = threading.Thread(
            target=self._run_market_review,
            args=(message, market_hint),
            daemon=True
        )
        thread.start()

        return BotResponse.markdown_response(
            "✅ **大盘复盘任务已启动**\n\n"
            "正在分析：\n"
            "• 主要指数表现\n"
            "• 板块热点分析\n"
            "• 市场情绪判断\n"
            "• 后市展望\n\n"
            "分析完成后将自动推送结果。"
        )

    @staticmethod
    def _parse_market_hint(args: List[str]) -> Optional[str]:
        if not args:
            return None
        token = (args[0] or "").strip().upper()
        market_map = {
            "CN": "CN",
            "A股": "CN",
            "A": "CN",
            "HK": "HK",
            "港股": "HK",
            "H": "HK",
            "US": "US",
            "美股": "US",
            "USA": "US",
        }
        return market_map.get(token)

    @staticmethod
    def _build_owner_key(message: BotMessage) -> Optional[str]:
        platform = (message.platform or "").strip().lower()
        user_id = (message.user_id or "").strip()
        if not platform or not user_id:
            return None
        return f"{platform}:{user_id}".lower()

    def _run_market_review(self, message: BotMessage, market_hint: Optional[str] = None) -> None:
        """后台执行大盘复盘"""
        try:
            from src.config import get_config
            from src.notification import NotificationService
            from src.market_analyzer import MarketAnalyzer
            from src.search_service import SearchService
            from src.analyzer import GeminiAnalyzer

            config = get_config()
            notifier = NotificationService(source_message=message)

            # 初始化搜索服务
            search_service = None
            if config.bocha_api_keys or config.tavily_api_keys or config.serpapi_keys:
                search_service = SearchService(
                    bocha_keys=config.bocha_api_keys,
                    tavily_keys=config.tavily_api_keys,
                    serpapi_keys=config.serpapi_keys
                )

            # 初始化 AI 分析器
            analyzer = None
            if config.gemini_api_key or config.openai_api_key:
                analyzer = GeminiAnalyzer()

            # 执行复盘
            market_analyzer = MarketAnalyzer(
                search_service=search_service,
                analyzer=analyzer
            )

            review_report = market_analyzer.run_daily_review(
                query_text=(message.content or "").strip(),
                market_hint=market_hint,
                owner_key=self._build_owner_key(message),
            )

            if review_report:
                # 推送结果
                report_content = f"🎯 **大盘复盘**\n\n{review_report}"
                notifier.send(report_content)
                logger.info("[MarketCommand] 大盘复盘完成并已推送")
            else:
                logger.warning("[MarketCommand] 大盘复盘返回空结果")

        except Exception as e:
            logger.error(f"[MarketCommand] 大盘复盘失败: {e}")
            logger.exception(e)
