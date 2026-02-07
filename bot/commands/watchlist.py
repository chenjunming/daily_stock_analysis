# -*- coding: utf-8 -*-
"""
===================================
自选股管理命令
===================================

支持在机器人会话中添加自选股（写入 WebUI 同一数据库）。
"""

import re
from typing import List, Optional, Tuple

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.storage import get_db
from src.analyzer import STOCK_NAME_MAP


class WatchlistCommand(BotCommand):
    """
    自选股管理命令

    用法：
        /watchlist add 600519 贵州茅台
        /watchlist add HK00700 腾讯控股
        /watchlist add 腾讯控股
        /watchlist add AAPL Apple US
        /watchlist remove 600519
        /watchlist remove 腾讯控股
        /remove 腾讯控股
        /watchlist 600036 招商银行
    """

    @property
    def name(self) -> str:
        return "watchlist"

    @property
    def aliases(self) -> List[str]:
        return ["wl", "add", "添加", "remove", "rm", "删除", "移除", "自选", "自选股"]

    @property
    def description(self) -> str:
        return "管理自选股（支持查看/添加/删除）"

    @property
    def usage(self) -> str:
        return "/watchlist <list|add|remove> ..."

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        if not args:
            return self._render_watchlist()

        action = args[0].lower()
        payload = args[1:]

        # 显式查看列表
        if action in {"list", "ls", "列表"}:
            return self._render_watchlist()

        # 删除模式（支持 /watchlist remove xxx）
        if action in {"remove", "rm", "del", "delete", "删除", "移除"}:
            return self._remove_watchlist(payload)

        # 删除别名模式（支持 /remove xxx /rm xxx /删除 xxx）
        if self._looks_like_remove_invocation(message):
            return self._remove_watchlist(args)

        # 兼容省略 add：/watchlist 600519 贵州茅台
        if action not in {"add", "a", "添加"}:
            payload = args

        if not payload:
            return BotResponse.error_response(f"参数不足\n用法: `{self.usage}`")

        code_raw = payload[0].strip().upper()
        rest = payload[1:]

        market = None
        if rest:
            maybe_market = self._normalize_market(rest[-1])
            if maybe_market:
                market = maybe_market
                rest = rest[:-1]

        name = " ".join(rest).strip() if rest else None
        verified_by_lookup = False

        normalized_code, normalized_market, err = self._normalize_code_and_market(code_raw, market)
        # 不是合法代码时，按名称解析
        if err:
            query_name = " ".join(payload).strip()
            normalized_code, resolved_name, normalized_market, err = self._resolve_by_name(query_name, market)
            if err:
                return BotResponse.error_response(err)
            if not name:
                name = resolved_name
            verified_by_lookup = True

        if not name:
            name = (
                STOCK_NAME_MAP.get(normalized_code)
                or STOCK_NAME_MAP.get(normalized_code.replace("HK", ""))
                or normalized_code
            )

        db = get_db()
        created = db.add_watchlist(
            code=normalized_code,
            name=name,
            market=normalized_market,
            verified=verified_by_lookup
        )
        if not created:
            # 区分“已存在”和“无法确认名称”两种场景
            existing = db.get_watchlist()
            exists = any((getattr(x, "code", "") or "").upper() == normalized_code for x in existing)
            if exists:
                return BotResponse.error_response(f"{normalized_code} 已在自选股中")
            return BotResponse.error_response(
                f"无法添加 {normalized_code}：未找到对应股票名称或股票不存在"
            )

        market_label = {"CN": "A股", "HK": "港股", "US": "美股"}.get(normalized_market, normalized_market)
        return BotResponse.markdown_response(
            "✅ **已添加自选股**\n\n"
            f"• 名称: {name}\n"
            f"• 代码: `{normalized_code}`\n"
            f"• 市场: {market_label}"
        )

    def _remove_watchlist(self, payload: List[str]) -> BotResponse:
        """删除自选股（支持代码或名称）。"""
        if not payload:
            return BotResponse.error_response("参数不足\n用法: `/watchlist remove <代码|名称>`")

        query = " ".join([x.strip() for x in payload if x.strip()]).strip()
        if not query:
            return BotResponse.error_response("参数不足\n用法: `/watchlist remove <代码|名称>`")

        market = None
        parts = query.split()
        if len(parts) >= 2:
            maybe_market = self._normalize_market(parts[-1])
            if maybe_market:
                market = maybe_market
                query = " ".join(parts[:-1]).strip()

        code_upper = query.upper()
        normalized_code, _, code_err = self._normalize_code_and_market(code_upper, market)

        db = get_db()
        display_name = query

        if code_err:
            # 按名称解析代码后删除
            normalized_code, resolved_name, _, err = self._resolve_by_name(query, market)
            if err:
                return BotResponse.error_response(err)
            display_name = resolved_name or query
        else:
            display_name = (
                STOCK_NAME_MAP.get(normalized_code)
                or STOCK_NAME_MAP.get(normalized_code.replace("HK", ""))
                or query
            )

        removed = db.remove_watchlist(normalized_code)
        if not removed:
            return BotResponse.error_response(f"{normalized_code} 不在自选股中")

        return BotResponse.markdown_response(
            "🗑️ **已删除自选股**\n\n"
            f"• 名称: {display_name}\n"
            f"• 代码: `{normalized_code}`"
        )

    @staticmethod
    def _render_watchlist() -> BotResponse:
        """展示全部自选股（按市场分组）。"""
        db = get_db()
        items = db.get_watchlist()
        if not items:
            return BotResponse.markdown_response(
                "📌 **当前没有自选股**\n\n"
                "可用示例：\n"
                "• `/watchlist add 600519 贵州茅台`\n"
                "• `/watchlist add 腾讯控股`\n"
                "• `/watchlist remove 腾讯控股`"
            )

        grouped = {"CN": [], "HK": [], "US": []}
        for item in items:
            market = (getattr(item, "market", "CN") or "CN").upper()
            code = (getattr(item, "code", "") or "").upper()
            name = (getattr(item, "name", "") or "").strip() or code
            grouped.setdefault(market, []).append((name, code))

        market_titles = {"CN": "A股", "HK": "港股", "US": "美股"}
        lines = ["📌 **当前自选股列表**", ""]
        total = 0
        for market in ["CN", "HK", "US"]:
            stocks = grouped.get(market, [])
            if not stocks:
                continue
            total += len(stocks)
            lines.append(f"**{market_titles.get(market, market)}（{len(stocks)}）**")
            for name, code in stocks[:30]:
                lines.append(f"• {name} (`{code}`)")
            if len(stocks) > 30:
                lines.append(f"• ... 其余 {len(stocks) - 30} 只")
            lines.append("")

        lines.append(f"合计：{total} 只")
        lines.append("")
        lines.append("添加示例：`/watchlist add 腾讯控股`")
        lines.append("删除示例：`/watchlist remove 腾讯控股`")
        return BotResponse.markdown_response("\n".join(lines))

    @staticmethod
    def _looks_like_remove_invocation(message: BotMessage) -> bool:
        """判断是否使用了删除别名命令（如 /remove /rm /删除）。"""
        content = (message.content or "").strip().lower()
        return (
            content.startswith("/remove")
            or content.startswith("/rm")
            or content.startswith("删除")
            or content.startswith("移除")
        )

    @staticmethod
    def _normalize_market(text: str) -> Optional[str]:
        t = (text or "").strip().upper()
        market_map = {
            "CN": "CN",
            "A": "CN",
            "A股".upper(): "CN",
            "HK": "HK",
            "H": "HK",
            "港股".upper(): "HK",
            "US": "US",
            "USA": "US",
            "美股".upper(): "US",
        }
        return market_map.get(t)

    @staticmethod
    def _normalize_code_and_market(code: str, market: Optional[str]) -> Tuple[str, str, Optional[str]]:
        code = (code or "").strip().upper()
        market = (market or "").strip().upper() or None

        # 港股：HK00700 或 00700
        if re.match(r'^HK\d{5}$', code):
            inferred_market = "HK"
            normalized_code = code
        elif re.match(r'^\d{5}$', code):
            inferred_market = "HK"
            normalized_code = f"HK{code}"
        # A股：6位数字
        elif re.match(r'^\d{6}$', code):
            inferred_market = "CN"
            normalized_code = code
        # 美股：1-5字母，可带点后缀
        elif re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', code):
            inferred_market = "US"
            normalized_code = code
        else:
            return code, market or "", "无效股票代码格式（A股6位 / 港股HK+5位或5位 / 美股字母代码）"

        final_market = market or inferred_market
        if market and market != inferred_market:
            # 允许指定 HK + 5位代码时推断为 HK，其余严格一致
            return normalized_code, final_market, f"代码 {code} 与市场 {market} 不匹配"

        return normalized_code, final_market, None

    @staticmethod
    def _resolve_by_name(query: str, market: Optional[str]) -> Tuple[str, str, str, Optional[str]]:
        """按名称解析股票，返回 (code, name, market, error)。"""
        keyword = (query or "").strip()
        if not keyword:
            return "", "", market or "", "请输入股票代码或名称"

        try:
            from web.services import get_stock_search_service
            service = get_stock_search_service()
            result = service.search(keyword=keyword, market=market, limit=5)
            data = result.get("data", []) if isinstance(result, dict) else []
            if not data:
                return "", "", market or "", f"未找到匹配股票：{keyword}"

            # 精确名称优先
            exact = [x for x in data if (x.get("name", "").strip().lower() == keyword.lower())]
            hit = exact[0] if exact else data[0]

            # 同分歧义提示
            if not exact and len(data) > 1:
                sample = " / ".join([f"{x.get('name','')}({x.get('code','')})" for x in data[:3]])
                return "", "", market or "", f"找到多个匹配：{sample}。请补充市场或直接用代码。"

            code = (hit.get("code") or "").upper()
            name = (hit.get("name") or code).strip()
            mkt = (hit.get("market") or market or "").upper()
            if not code:
                return "", "", market or "", f"未找到匹配股票：{keyword}"
            return code, name, mkt, None
        except Exception as e:
            return "", "", market or "", f"名称解析失败：{e}"
