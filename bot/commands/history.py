# -*- coding: utf-8 -*-
"""
===================================
历史报告命令
===================================
"""

import json
from typing import List, Optional, Any

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from bot.commands.analyze import AnalyzeCommand
from src.analyzer import AnalysisResult
from src.time_utils import as_utc8


class HistoryCommand(BotCommand):
    """
    查询个股分析历史。
    """

    @property
    def name(self) -> str:
        return "history"

    @property
    def aliases(self) -> List[str]:
        return ["his", "历史", "记录"]

    @property
    def description(self) -> str:
        return "查看个股历史分析报告"

    @property
    def usage(self) -> str:
        return "/history <股票代码|股票名称> [数量] | /history <股票代码|股票名称> <task_id>"

    def validate_args(self, args: List[str]) -> Optional[str]:
        if not args:
            return "请输入股票代码或名称"
        return None

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        from src.storage import get_db
        from src.notification import NotificationService

        tokens = [a.strip() for a in args if a.strip()]
        if not tokens:
            return BotResponse.error_response(f"参数不足\n用法: `{self.usage}`")

        limit = 5
        query_id = None

        if len(tokens) >= 2:
            tail = tokens[-1]
            if tail.isdigit():
                limit = max(1, min(20, int(tail)))
                tokens = tokens[:-1]
            elif "_" in tail and len(tail) >= 10:
                query_id = tail
                tokens = tokens[:-1]

        query = " ".join(tokens).strip()
        if not query:
            return BotResponse.error_response(f"参数不足\n用法: `{self.usage}`")

        resolver = AnalyzeCommand()
        code, resolved_name, err = resolver._resolve_stock_code(query, market=None)  # noqa: SLF001
        if err:
            return BotResponse.error_response(err)

        db = get_db()
        if query_id:
            rows = db.get_analysis_history(query_id=query_id, days=365, limit=1)
            if not rows:
                return BotResponse.error_response(f"未找到 task_id=`{query_id}` 的报告")
            row = rows[0]
            if (getattr(row, "code", "") or "").upper() != code.upper():
                return BotResponse.error_response(
                    f"task_id=`{query_id}` 不属于 {resolved_name} (`{code}`)"
                )
            result = self._record_to_result(row, code)
            report = NotificationService().generate_dashboard_report([result])
            return BotResponse.markdown_response(
                f"📚 **历史报告详情**\n\n"
                f"• 股票: {resolved_name} (`{code}`)\n"
                f"• task_id: `{query_id}`\n\n"
                f"{report}"
            )

        rows = db.get_analysis_history(code=code, days=365, limit=limit)
        if not rows:
            return BotResponse.error_response(f"未找到 {resolved_name} (`{code}`) 的历史报告")

        lines = [
            "📚 **历史报告列表**",
            "",
            f"• 股票: {resolved_name} (`{code}`)",
            f"• 数量: {len(rows)} 条",
            "",
        ]
        for idx, row in enumerate(rows, start=1):
            task_id = getattr(row, "query_id", "") or "-"
            created_at = as_utc8(getattr(row, "created_at", None))
            time_text = created_at.strftime("%Y-%m-%d %H:%M:%S (UTC+8)") if created_at else "-"
            advice = getattr(row, "operation_advice", "") or "-"
            trend = getattr(row, "trend_prediction", "") or "-"
            summary = (getattr(row, "analysis_summary", "") or "").strip().replace("\n", " ")
            if len(summary) > 80:
                summary = summary[:80] + "..."
            lines.extend([
                f"**{idx}.** `{task_id}`",
                f"• 时间: {time_text}",
                f"• 建议: {advice} | 趋势: {trend}",
                f"• 摘要: {summary or '-'}",
                "",
            ])

        lines.append("查看指定报告：`/history <股票> <task_id>`")
        return BotResponse.markdown_response("\n".join(lines))

    @staticmethod
    def _record_to_result(row: Any, fallback_code: str) -> AnalysisResult:
        raw = {}
        try:
            raw = json.loads(getattr(row, "raw_result", "") or "{}")
        except Exception:
            raw = {}
        return AnalysisResult(
            code=(raw.get("code") or getattr(row, "code", fallback_code) or fallback_code),
            name=(raw.get("name") or getattr(row, "name", fallback_code) or fallback_code),
            sentiment_score=int(raw.get("sentiment_score") or getattr(row, "sentiment_score", 50) or 50),
            trend_prediction=(raw.get("trend_prediction") or getattr(row, "trend_prediction", "震荡") or "震荡"),
            operation_advice=(raw.get("operation_advice") or getattr(row, "operation_advice", "观望") or "观望"),
            decision_type=(raw.get("decision_type") or "hold"),
            confidence_level=(raw.get("confidence_level") or "中"),
            dashboard=raw.get("dashboard"),
            analysis_summary=(raw.get("analysis_summary") or getattr(row, "analysis_summary", "") or ""),
        )
