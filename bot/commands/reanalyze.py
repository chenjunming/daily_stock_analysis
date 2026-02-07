# -*- coding: utf-8 -*-
"""
===================================
重新分析命令
===================================
"""

from typing import List, Optional

from bot.commands.analyze import AnalyzeCommand


class ReanalyzeCommand(AnalyzeCommand):
    """
    强制重新分析（忽略当日缓存）。
    """

    @property
    def name(self) -> str:
        return "reanalyze"

    @property
    def aliases(self) -> List[str]:
        return ["ra", "重新分析", "重跑分析", "强制分析"]

    @property
    def description(self) -> str:
        return "强制重新分析指定股票（忽略当日缓存）"

    @property
    def usage(self) -> str:
        return "/reanalyze <股票代码|股票名称> [CN|HK|US] [full]"

    def validate_args(self, args: List[str]) -> Optional[str]:
        if not args:
            return "请输入要重新分析的股票代码或名称"
        return None

    def execute(self, message, args):
        # 始终追加 force 标记，复用 AnalyzeCommand 的主逻辑
        force_args = list(args) + ["--force"]
        return super().execute(message, force_args)

