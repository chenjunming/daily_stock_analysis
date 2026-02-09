# -*- coding: utf-8 -*-
"""
===================================
持仓管理命令
===================================

支持按用户隔离维护持仓信息（成本、仓位）。
"""

import json
import logging
import re
import threading
from datetime import datetime, date, time as dt_time
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple, Dict, Any

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.storage import get_db
from src.analyzer import STOCK_NAME_MAP
from data_provider import DataFetcherManager

logger = logging.getLogger(__name__)


class PositionCommand(BotCommand):
    _daily_quote_cache_lock = threading.Lock()
    _daily_quote_cache: Dict[str, Dict[str, Optional[float]]] = {}

    """
    持仓管理命令。

    用法：
        /position
        /position set 腾讯控股 320 100 8 HK
        /position set 腾讯控股 320 100 HK
        /position set 600519 1680 12
        /position total 1000000
        /position fx 6.94 0.888
        /position batch TSM 180 12; NVDA 700 8; QQQ 420 10
        /position sell 600519 1760 30
        /position clear
        /position remove 腾讯控股
        /position list fast
        /position sync longport
        /pset 600519 1680 12
        /ptotal 1000000
        /pfx 6.94 0.888
        /psell 600519 1760 30
        /pclear
        /pos fast
        /pdel 腾讯控股
        /plist
    """

    @property
    def name(self) -> str:
        return "position"

    @property
    def aliases(self) -> List[str]:
        return ["pos", "仓位", "持仓", "pset", "ptotal", "pfx", "psell", "pdel", "plist", "pclear"]

    @property
    def description(self) -> str:
        return "管理个人持仓（成本/仓位）"

    @property
    def usage(self) -> str:
        return "/position <list|set|batch|total|fx|sell|remove|clear|sync longport> ... [fast]"

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        owner_key = self._build_owner_key(message)
        if not owner_key:
            return BotResponse.error_response("无法识别用户身份，不能维护持仓")

        invoke = (message.content or "").strip().lower()
        if invoke.startswith("/pset"):
            if self._looks_like_batch_payload(args):
                return self._set_position_batch(owner_key, args, message)
            return self._set_position(owner_key, args, message)
        if invoke.startswith("/pdel"):
            return self._remove_position(owner_key, args, message)
        if invoke.startswith("/psell"):
            return self._sell_position(owner_key, args, message)
        if invoke.startswith("/ptotal"):
            return self._set_total_asset(owner_key, args)
        if invoke.startswith("/pfx"):
            return self._set_fx_rates(owner_key, args)
        if invoke.startswith("/pclear"):
            return self._clear_positions(owner_key, message)
        if invoke.startswith("/plist"):
            mode = self._parse_list_mode(args)
            return self._list_positions(owner_key, mode=mode)

        if not args:
            return self._list_positions(owner_key)

        action = (args[0] or "").strip().lower()
        payload = args[1:]
        if action in {"fast", "quick", "lite", "极速", "快速", "轻量"}:
            return self._list_positions(owner_key, mode="fast")
        if action in {"list", "ls", "show", "列表"}:
            mode = self._parse_list_mode(payload)
            return self._list_positions(owner_key, mode=mode)
        if action in {"set", "add", "update", "设置", "新增", "更新"}:
            return self._set_position(owner_key, payload, message)
        if action in {"batch", "bulk", "批量"}:
            return self._set_position_batch(owner_key, payload, message)
        if action in {"total", "budget", "cap", "资金", "总仓"}:
            return self._set_total_asset(owner_key, payload)
        if action in {"fx", "rate", "rates", "汇率"}:
            return self._set_fx_rates(owner_key, payload)
        if action in {"sell", "卖出"}:
            return self._sell_position(owner_key, payload, message)
        if action in {"clear", "clean", "reset", "清空", "清除"}:
            return self._clear_positions(owner_key, message)
        if action in {"remove", "rm", "del", "delete", "删除", "移除"}:
            return self._remove_position(owner_key, payload, message)
        if action in {"sync", "import", "同步", "导入"}:
            return self._sync_positions(owner_key, payload, message)

        # 兼容无 action：默认 set
        return self._set_position(owner_key, args, message)

    @staticmethod
    def _build_owner_key(message: BotMessage) -> str:
        platform = (message.platform or "").strip().lower()
        user_id = (message.user_id or "").strip()
        if not platform or not user_id:
            return ""
        return f"{platform}:{user_id}".lower()

    def _set_position(self, owner_key: str, payload: List[str], message: BotMessage) -> BotResponse:
        parsed = self._parse_set_payload(payload)
        if parsed.get("error"):
            return BotResponse.error_response(parsed["error"])

        query = parsed["query"]
        avg_cost = parsed["avg_cost"]
        shares = parsed.get("shares")
        weight_pct = parsed.get("weight_pct")
        market = parsed.get("market")

        code, name, norm_market, err = self._resolve_stock(query, market)
        if err:
            return BotResponse.error_response(err)

        db = get_db()
        shares, weight_pct, calc_note, err = self._materialize_position_metrics(
            db=db,
            owner_key=owner_key,
            code=code,
            market=norm_market,
            avg_cost=avg_cost,
            parsed=parsed,
        )
        if err:
            return BotResponse.error_response(err)

        result = db.upsert_holding(
            owner_key=owner_key,
            code=code,
            name=name,
            market=norm_market,
            avg_cost=avg_cost,
            shares=shares,
            weight_pct=weight_pct,
            operator=f"{message.platform}:{message.user_id}",
            verified=True
        )
        if not result.get("success"):
            return BotResponse.error_response(result.get("error", "写入持仓失败"))

        action = result.get("action", "update")
        action_name = "新增" if action == "add" else "更新"
        data = result.get("data", {}) or {}
        saved_shares = data.get("shares")
        profile = db.get_portfolio_profile(owner_key)
        total_weight = float(profile.get("total_weight_pct") or 0.0)
        warn = ""
        if total_weight > 100:
            warn = f"⚠️ 当前总仓位 `{total_weight:.2f}%`，已超过 100%，请注意组合风险。"

        lines = [
            "✅ **持仓已保存**",
            "",
            f"• 动作: {action_name}",
            f"• 股票: {name} (`{code}`)",
            f"• 市场: {self._market_name(norm_market)}",
            f"• 成本: `{avg_cost:.4f}`",
            f"• 股数: `{float(saved_shares):.4f}`" if saved_shares is not None else "• 股数: `未记录`",
            f"• 仓位: `{weight_pct:.2f}%`",
            f"• 组合总仓位: `{total_weight:.2f}%`",
        ]
        if calc_note:
            lines.append(f"• 说明: {calc_note}")
        if warn:
            lines.append(warn)
        return BotResponse.markdown_response("\n".join(lines))

    def _set_position_batch(self, owner_key: str, payload: List[str], message: BotMessage) -> BotResponse:
        raw = " ".join([x for x in payload if x]).strip()
        if not raw:
            return BotResponse.error_response(
                "参数不足，用法: `/position batch TSM 180 12; NVDA 700 8; QQQ 420 10`"
            )

        chunks = [x.strip() for x in re.split(r"[;\n；|]+", raw) if x.strip()]
        if not chunks:
            return BotResponse.error_response(
                "未识别到批量条目，用法: `/position batch TSM 180 12; NVDA 700 8; QQQ 420 10`"
            )

        db = get_db()
        success_rows: List[str] = []
        failed_rows: List[str] = []

        for idx, chunk in enumerate(chunks, start=1):
            parsed = self._parse_set_payload(chunk.split())
            if parsed.get("error"):
                failed_rows.append(f"{idx}. `{chunk}` -> {parsed['error']}")
                continue

            query = parsed["query"]
            avg_cost = parsed["avg_cost"]
            shares = parsed.get("shares")
            weight_pct = parsed.get("weight_pct")
            market = parsed.get("market")

            code, name, norm_market, err = self._resolve_stock(query, market)
            if err:
                failed_rows.append(f"{idx}. `{chunk}` -> {err}")
                continue

            shares, weight_pct, calc_note, err = self._materialize_position_metrics(
                db=db,
                owner_key=owner_key,
                code=code,
                market=norm_market,
                avg_cost=avg_cost,
                parsed=parsed,
            )
            if err:
                failed_rows.append(f"{idx}. `{chunk}` -> {err}")
                continue

            result = db.upsert_holding(
                owner_key=owner_key,
                code=code,
                name=name,
                market=norm_market,
                avg_cost=avg_cost,
                shares=shares,
                weight_pct=weight_pct,
                operator=f"{message.platform}:{message.user_id}",
                verified=True
            )
            if not result.get("success"):
                failed_rows.append(f"{idx}. `{chunk}` -> {result.get('error', '写入持仓失败')}")
                continue

            action_name = "新增" if result.get("action") == "add" else "更新"
            saved_data = result.get("data", {}) or {}
            saved_shares = saved_data.get("shares")
            shares_text = f"{float(saved_shares):.4f}" if saved_shares is not None else "未记录"
            success_rows.append(
                f"{idx}. {action_name} {name} (`{code}`) | 成本 `{avg_cost:.4f}` | "
                f"股数 `{shares_text}` | 仓位 `{weight_pct:.2f}%`"
                + (f" | {calc_note}" if calc_note else "")
            )

        if not success_rows:
            return BotResponse.error_response(
                "批量写入失败：\n" + "\n".join(failed_rows[:20])
            )

        profile = db.get_portfolio_profile(owner_key)
        total_weight = float(profile.get("total_weight_pct") or 0.0)

        lines = [
            "✅ **批量持仓写入完成**",
            "",
            f"• 成功: `{len(success_rows)}`",
            f"• 失败: `{len(failed_rows)}`",
            f"• 组合总仓位: `{total_weight:.2f}%`",
            "",
            "**成功明细**",
        ]
        lines.extend([f"• {row}" for row in success_rows[:30]])
        if failed_rows:
            lines.extend(["", "**失败明细**"])
            lines.extend([f"• {row}" for row in failed_rows[:20]])
        if total_weight > 100:
            lines.extend(["", "⚠️ 总仓位已超过 100%，请注意风险控制。"])
        return BotResponse.markdown_response("\n".join(lines))

    def _sell_position(self, owner_key: str, payload: List[str], message: BotMessage) -> BotResponse:
        parsed = self._parse_sell_payload(payload)
        if parsed.get("error"):
            return BotResponse.error_response(parsed["error"])

        query = parsed["query"]
        sell_price = parsed["sell_price"]
        sell_shares = parsed["sell_shares"]
        market = parsed.get("market")

        code = query.upper()
        err = None
        if not self._is_valid_code(code):
            code, _, _, err = self._resolve_stock(query, market)
            if err:
                return BotResponse.error_response(err)
        else:
            if market == "HK" and re.match(r"^\d{5}$", code):
                code = f"HK{code}"

        db = get_db()
        result = db.sell_holding(
            owner_key=owner_key,
            code=code,
            sell_price=sell_price,
            sell_shares=sell_shares,
            operator=f"{message.platform}:{message.user_id}",
        )
        if not result.get("success"):
            return BotResponse.error_response(result.get("error", "卖出失败"))

        action = result.get("action", "sell")
        realized_pnl = float(result.get("realized_pnl") or 0.0)
        pnl_sign = "盈利" if realized_pnl >= 0 else "亏损"
        if action == "sell_all":
            data = result.get("data", {}) or {}
            return BotResponse.markdown_response(
                "✅ **卖出完成（清仓）**\n\n"
                f"• 股票: {data.get('name', code)} (`{code}`)\n"
                f"• 成交价: `{sell_price:.4f}`\n"
                f"• 卖出股数: `{sell_shares:.4f}`\n"
                f"• 已实现{pnl_sign}: `{realized_pnl:+.2f}`"
            )

        data = result.get("data", {}) or {}
        return BotResponse.markdown_response(
            "✅ **卖出完成**\n\n"
            f"• 股票: {data.get('name', code)} (`{code}`)\n"
            f"• 成交价: `{sell_price:.4f}`\n"
            f"• 卖出股数: `{sell_shares:.4f}`\n"
            f"• 剩余股数: `{float(result.get('remain_shares') or 0):.4f}`\n"
            f"• 新仓位: `{float(result.get('remain_weight_pct') or 0):.2f}%`\n"
            f"• 持仓成本(不变): `{float(result.get('avg_cost') or 0):.4f}`\n"
            f"• 已实现{pnl_sign}: `{realized_pnl:+.2f}`"
        )

    def _remove_position(self, owner_key: str, payload: List[str], message: BotMessage) -> BotResponse:
        if not payload:
            return BotResponse.error_response("参数不足，用法: `/position remove <代码|名称>`")

        query = " ".join([x.strip() for x in payload if x.strip()]).strip()
        if not query:
            return BotResponse.error_response("参数不足，用法: `/position remove <代码|名称>`")

        market = None
        if " " in query:
            parts = query.split()
            maybe_market = self._normalize_market(parts[-1])
            if maybe_market:
                market = maybe_market
                query = " ".join(parts[:-1]).strip()

        code = query.upper()
        err = None
        if not self._is_valid_code(code):
            code, _, _, err = self._resolve_stock(query, market)
            if err:
                return BotResponse.error_response(err)
        else:
            if market == "HK" and re.match(r"^\d{5}$", code):
                code = f"HK{code}"

        db = get_db()
        result = db.remove_holding(owner_key, code, operator=f"{message.platform}:{message.user_id}")
        if not result.get("success"):
            return BotResponse.error_response(result.get("error", "删除失败"))

        data = result.get("data", {})
        return BotResponse.markdown_response(
            "🗑️ **持仓已删除**\n\n"
            f"• 股票: {data.get('name', code)} (`{code}`)\n"
            f"• 成本: `{float(data.get('avg_cost') or 0):.4f}`\n"
            f"• 仓位: `{float(data.get('weight_pct') or 0):.2f}%`"
        )

    def _clear_positions(self, owner_key: str, message: BotMessage) -> BotResponse:
        db = get_db()
        rows = db.list_holdings(owner_key)
        if not rows:
            return BotResponse.markdown_response("📦 当前没有持仓记录，无需清空。")

        removed = 0
        failed = 0
        for row in rows:
            result = db.remove_holding(owner_key, row.code, operator=f"{message.platform}:{message.user_id}")
            if result.get("success"):
                removed += 1
            else:
                failed += 1

        if removed == 0:
            return BotResponse.error_response("清空失败，请稍后重试。")

        lines = [
            "🧹 **持仓清空完成**",
            "",
            f"• 已删除: `{removed}`",
            f"• 删除失败: `{failed}`",
        ]
        if failed > 0:
            lines.append("⚠️ 存在部分删除失败，可重试 `/position clear`。")
        return BotResponse.markdown_response("\n".join(lines))

    def _set_total_asset(self, owner_key: str, payload: List[str]) -> BotResponse:
        db = get_db()
        if not payload:
            cfg = db.get_portfolio_user_config(owner_key)
            total = cfg.get("total_asset_cny")
            if total is None or float(total) <= 0:
                return BotResponse.markdown_response(
                    "💼 **当前未设置组合总资产**\n\n"
                    "可用：`/position total 1000000`（单位默认人民币）"
                )
            return BotResponse.markdown_response(
                "💼 **当前组合总资产配置**\n\n"
                f"• 总资产: `{float(total):.2f}` CNY\n"
                f"• USD/CNY: `{float(cfg.get('usd_cny') or 6.94):.4f}`\n"
                f"• HKD/CNY: `{float(cfg.get('hkd_cny') or 0.888):.4f}`"
            )

        parsed = self._parse_total_payload(payload)
        if parsed.get("error"):
            return BotResponse.error_response(parsed["error"])

        total_asset_cny = parsed["total_asset_cny"]
        result = db.upsert_portfolio_user_config(owner_key=owner_key, total_asset_cny=total_asset_cny)
        if not result.get("success"):
            return BotResponse.error_response(result.get("error", "保存总资产失败"))

        data = result.get("data", {}) or {}
        recalc = self._recalculate_weights_by_total(db, owner_key)
        return BotResponse.markdown_response(
            "✅ **组合总资产已保存**\n\n"
            f"• 总资产: `{float(data.get('total_asset_cny') or total_asset_cny):.2f}` CNY\n"
            "• 已按新总资产自动重算持仓仓位\n"
            f"• 已重算: `{recalc.get('updated', 0)}`\n"
            f"• 跳过(无股数): `{recalc.get('skipped', 0)}`\n"
            f"• 失败: `{recalc.get('failed', 0)}`\n"
            "• 后续 `/position set` 省略仓位时，也会按股数与币种自动换算。"
        )

    def _set_fx_rates(self, owner_key: str, payload: List[str]) -> BotResponse:
        db = get_db()
        if not payload:
            cfg = db.get_portfolio_user_config(owner_key)
            return BotResponse.markdown_response(
                "💱 **当前汇率配置**\n\n"
                f"• USD/CNY: `{float(cfg.get('usd_cny') or 6.94):.4f}`\n"
                f"• HKD/CNY: `{float(cfg.get('hkd_cny') or 0.888):.4f}`\n\n"
                "设置示例：`/position fx 6.94 0.888`"
            )

        parsed = self._parse_fx_payload(payload)
        if parsed.get("error"):
            return BotResponse.error_response(parsed["error"])

        result = db.upsert_portfolio_user_config(
            owner_key=owner_key,
            usd_cny=parsed["usd_cny"],
            hkd_cny=parsed["hkd_cny"],
        )
        if not result.get("success"):
            return BotResponse.error_response(result.get("error", "保存汇率失败"))

        data = result.get("data", {}) or {}
        recalc = self._recalculate_weights_by_total(db, owner_key)
        return BotResponse.markdown_response(
            "✅ **汇率已更新**\n\n"
            f"• USD/CNY: `{float(data.get('usd_cny') or parsed['usd_cny']):.4f}`\n"
            f"• HKD/CNY: `{float(data.get('hkd_cny') or parsed['hkd_cny']):.4f}`\n"
            "• 已按新汇率自动重算持仓仓位\n"
            f"• 已重算: `{recalc.get('updated', 0)}` | 跳过(无股数): `{recalc.get('skipped', 0)}` | 失败: `{recalc.get('failed', 0)}`"
        )

    def _recalculate_weights_by_total(self, db, owner_key: str) -> Dict[str, int]:
        cfg = db.get_portfolio_user_config(owner_key)
        total_asset_cny = float(cfg.get("total_asset_cny") or 0.0)
        if total_asset_cny <= 0:
            return {"updated": 0, "skipped": 0, "failed": 0}

        holdings = db.list_holdings(owner_key)
        if not holdings:
            return {"updated": 0, "skipped": 0, "failed": 0}

        code_market_map = {row.code: (row.market or "CN") for row in holdings}
        quotes = self._fetch_quote_snapshots([row.code for row in holdings], code_market_map=code_market_map)
        updated = 0
        skipped = 0
        failed = 0
        usd_cny = float(cfg.get("usd_cny") or 6.94)
        hkd_cny = float(cfg.get("hkd_cny") or 0.888)

        for row in holdings:
            shares = float(getattr(row, "shares", 0) or 0.0)
            if shares <= 0:
                skipped += 1
                continue

            ref_price = (quotes.get(row.code) or {}).get("price")
            new_weight = self._calc_weight_pct_by_total(
                total_asset_cny=total_asset_cny,
                market=(row.market or "CN"),
                ref_price=ref_price,
                avg_cost=float(row.avg_cost or 0.0),
                shares=shares,
                usd_cny=usd_cny,
                hkd_cny=hkd_cny,
            )
            result = db.upsert_holding(
                owner_key=owner_key,
                code=row.code,
                name=row.name,
                market=row.market,
                avg_cost=float(row.avg_cost or 0.0),
                shares=shares,
                weight_pct=float(new_weight),
                operator="system:recalc",
                verified=True
            )
            if result.get("success"):
                updated += 1
            else:
                failed += 1

        return {"updated": updated, "skipped": skipped, "failed": failed}

    def _materialize_position_metrics(
        self,
        db,
        owner_key: str,
        code: str,
        market: str,
        avg_cost: float,
        parsed: Dict[str, Any],
    ) -> Tuple[Optional[float], Optional[float], str, Optional[str]]:
        shares = parsed.get("shares")
        weight_pct = parsed.get("weight_pct")
        metric_value = parsed.get("metric_value")
        metric_is_percent = bool(parsed.get("metric_is_percent"))

        calc_note = ""
        if weight_pct is None and metric_value is not None:
            if metric_is_percent:
                weight_pct = float(metric_value)
            else:
                cfg = db.get_portfolio_user_config(owner_key)
                total_asset_cny = float(cfg.get("total_asset_cny") or 0.0)
                if total_asset_cny > 0:
                    shares = float(metric_value)
                    ref_price = (self._fetch_quote_snapshots([code]).get(code) or {}).get("price")
                    weight_pct = self._calc_weight_pct_by_total(
                        total_asset_cny=total_asset_cny,
                        market=market,
                        ref_price=ref_price,
                        avg_cost=avg_cost,
                        shares=shares,
                        usd_cny=float(cfg.get("usd_cny") or 6.94),
                        hkd_cny=float(cfg.get("hkd_cny") or 0.888),
                    )
                    if ref_price is not None and ref_price > 0:
                        calc_note = (
                            f"仓位按总资产 `{total_asset_cny:.2f} CNY` + 现价 `{float(ref_price):.4f}` 自动换算"
                        )
                    else:
                        calc_note = (
                            f"仓位按总资产 `{total_asset_cny:.2f} CNY` 自动换算（现价缺失，按成本绝对值估算）"
                        )
                else:
                    return shares, weight_pct, calc_note, (
                        "检测到你使用了“成本+股数”格式，但尚未设置总资产。"
                        "请先执行 `/position total <金额>`，"
                        "或将仓位写成百分比（如 `14%`）。"
                    )

        if weight_pct is None:
            return shares, weight_pct, calc_note, "缺少仓位，请提供仓位%或先设置 `/position total <金额>` 并提供股数"
        if weight_pct <= 0 or weight_pct > 100:
            return shares, weight_pct, calc_note, "持仓比例必须在 (0, 100] 范围内"
        if shares is not None and shares <= 0:
            return shares, weight_pct, calc_note, "持仓股数必须大于 0"
        return shares, weight_pct, calc_note, None

    @staticmethod
    def _calc_weight_pct_by_total(
        total_asset_cny: float,
        market: str,
        ref_price: Optional[float],
        avg_cost: float,
        shares: float,
        usd_cny: float,
        hkd_cny: float,
    ) -> float:
        fx = 1.0
        m = (market or "").upper()
        if m == "US":
            fx = float(usd_cny)
        elif m == "HK":
            fx = float(hkd_cny)
        px = float(ref_price) if ref_price is not None and float(ref_price) > 0 else abs(float(avg_cost))
        value_cny = px * float(shares) * fx
        return value_cny / float(total_asset_cny) * 100.0

    @staticmethod
    def _parse_total_payload(payload: List[str]) -> Dict[str, Any]:
        tokens = [x.strip() for x in payload if x.strip()]
        if not tokens:
            return {"error": "参数不足，用法: `/position total <金额>`"}

        unit = "CNY"
        maybe_unit = tokens[-1].upper()
        if maybe_unit in {"CNY", "RMB", "人民币"}:
            unit = "CNY"
            tokens = tokens[:-1]

        if not tokens:
            return {"error": "参数不足，用法: `/position total <金额>`"}

        try:
            amount = float(tokens[0].replace(",", ""))
        except ValueError:
            return {"error": "总资产金额必须是数字，例如：`/position total 1000000`"}
        if amount <= 0:
            return {"error": "总资产金额必须大于 0"}
        if unit != "CNY":
            return {"error": "目前仅支持人民币总资产，请使用 CNY"}
        return {"total_asset_cny": amount}

    @staticmethod
    def _parse_fx_payload(payload: List[str]) -> Dict[str, Any]:
        tokens = [x.strip() for x in payload if x.strip()]
        if len(tokens) < 2:
            return {"error": "参数不足，用法: `/position fx <USD/CNY> <HKD/CNY>`"}
        try:
            usd_cny = float(tokens[0])
            hkd_cny = float(tokens[1])
        except ValueError:
            return {"error": "汇率必须是数字，例如：`/position fx 6.94 0.888`"}
        if usd_cny <= 0 or hkd_cny <= 0:
            return {"error": "汇率必须大于 0"}
        return {"usd_cny": usd_cny, "hkd_cny": hkd_cny}

    @staticmethod
    def _parse_list_mode(payload: List[str]) -> str:
        text = " ".join([x.strip().lower() for x in payload if x.strip()])
        if any(k in text for k in ["fast", "quick", "lite", "极速", "快速", "轻量"]):
            return "fast"
        return "full"

    def _list_positions(self, owner_key: str, mode: str = "full") -> BotResponse:
        db = get_db()
        holdings = db.list_holdings(owner_key)
        if not holdings:
            return BotResponse.markdown_response(
                "📦 **当前没有持仓记录**\n\n"
                "示例：\n"
                "• `/position set 腾讯控股 320 8 HK`\n"
                "• `/pset 600519 1680 12`"
            )

        quotes: Dict[str, Dict[str, Optional[float]]] = {}
        if mode == "full":
            code_market_map = {row.code: (row.market or "CN") for row in holdings}
            quotes = self._fetch_quote_snapshots([row.code for row in holdings], code_market_map=code_market_map)
        else:
            # 快速模式：仅用本地日线收盘价兜底，不拉实时行情
            for row in holdings:
                price = None
                try:
                    latest = db.get_latest_data(row.code, days=1)
                    if latest and latest[0].close:
                        price = float(latest[0].close)
                except Exception:
                    pass
                quotes[row.code] = {"price": price, "pre_close": None}
        cfg = db.get_portfolio_user_config(owner_key)
        usd_cny = float(cfg.get("usd_cny") or 6.94)
        hkd_cny = float(cfg.get("hkd_cny") or 0.888)
        market_currency: Dict[str, str] = {"CN": "CNY", "HK": "HKD", "US": "USD"}
        market_currency_prefix: Dict[str, str] = {"CN": "¥", "HK": "HK$", "US": "$"}

        grouped: Dict[str, List[Dict[str, Any]]] = {"CN": [], "HK": [], "US": []}
        total_weight = 0.0
        total_day_pnl_cny = 0.0
        total_day_pnl_available = False
        missing_day_pnl_count = 0
        market_day_pnl_native: Dict[str, float] = {"CN": 0.0, "HK": 0.0, "US": 0.0}
        market_day_pnl_available: Dict[str, bool] = {"CN": False, "HK": False, "US": False}

        for row in holdings:
            q = quotes.get(row.code, {})
            price = q.get("price")
            pre_close = q.get("pre_close")
            change_amount = q.get("change_amount")
            pnl_pct = None
            avg_cost = float(row.avg_cost or 0.0)
            if price and avg_cost != 0:
                pnl_pct = (float(price) - avg_cost) / avg_cost * 100.0
            shares = float(getattr(row, "shares", 0) or 0.0)
            day_pnl = None
            if shares > 0:
                fx = 1.0
                market = (row.market or "CN").upper()
                if market == "US":
                    fx = usd_cny
                elif market == "HK":
                    fx = hkd_cny
                if change_amount is not None:
                    day_pnl = float(change_amount) * shares
                elif price is not None and pre_close is not None:
                    day_pnl = (float(price) - float(pre_close)) * shares
                if day_pnl is not None:
                    total_day_pnl_cny += day_pnl * fx
                    total_day_pnl_available = True
                    market_day_pnl_native[market] = market_day_pnl_native.get(market, 0.0) + day_pnl
                    market_day_pnl_available[market] = True
                else:
                    missing_day_pnl_count += 1
            grouped.setdefault(row.market, []).append({
                "name": row.name,
                "code": row.code,
                "market": (row.market or "CN").upper(),
                "avg_cost": row.avg_cost,
                "shares": shares,
                "weight_pct": row.weight_pct,
                "current_price": price,
                "pre_close": pre_close,
                "change_amount": change_amount,
                "pnl_pct": pnl_pct,
                "day_pnl": day_pnl,
                "price_session": self._resolve_display_session((row.market or "CN").upper(), q.get("price_session")),
            })
            total_weight += float(row.weight_pct or 0.0)

        lines = ["📦 **我的持仓**", ""]
        for market in ["CN", "HK", "US"]:
            rows = grouped.get(market) or []
            if not rows:
                continue
            lines.append(f"**{self._market_name(market)}（{len(rows)}）**")
            # 5列表格，适配飞书 column_set 稳定渲染
            lines.append("| 标的 | 成本/现价 | 股数 | 仓位 | 盈亏(总%/日%/日额) |")
            lines.append("|---|---|---:|---:|---|")
            for item in rows:
                pnl = item["pnl_pct"]
                pnl_text = "N/A" if pnl is None else f"{pnl:+.2f}%"
                price_text = "N/A" if item["current_price"] is None else f"{item['current_price']:.4f}"
                prefix = market_currency_prefix.get(item.get("market", market), "")
                day_pnl_text = "N/A" if item.get("day_pnl") is None else f"{prefix}{float(item['day_pnl']):+.2f}"
                day_change_pct = None
                pre_close = item.get("pre_close")
                if pre_close is not None and float(pre_close) > 0:
                    if item.get("change_amount") is not None:
                        day_change_pct = float(item["change_amount"]) / float(pre_close) * 100.0
                    elif item.get("current_price") is not None:
                        day_change_pct = (float(item["current_price"]) - float(pre_close)) / float(pre_close) * 100.0
                day_change_text = "N/A" if day_change_pct is None else f"{day_change_pct:+.2f}%"
                session_tag = self._price_session_text(item.get("price_session"))
                symbol = f"{item['name']} (`{item['code']}`){(' ' + session_tag) if session_tag else ''}"
                cost_price = f"{float(item['avg_cost'] or 0.0):.4f} / {price_text}"
                pnl_mix = f"{pnl_text} / {day_change_text} / {day_pnl_text}"
                lines.append(
                    f"| {symbol} | {cost_price} | {item['shares']:.4f} | "
                    f"{item['weight_pct']:.2f}% | {pnl_mix} |"
                )
            if market_day_pnl_available.get(market):
                cur = market_currency.get(market, "CNY")
                prefix = market_currency_prefix.get(market, "")
                lines.append(
                    f"| **{self._market_name(market)}当日盈亏({cur})** |  |  |  | "
                    f"**{prefix}{market_day_pnl_native.get(market, 0.0):+.2f}** |"
                )
            lines.append("")

        lines.append(f"**组合总仓位**: `{total_weight:.2f}%`")
        if total_day_pnl_available:
            lines.append(f"**组合当日盈亏(CNY)**: `¥{total_day_pnl_cny:+.2f}`")
        if missing_day_pnl_count > 0:
            lines.append(f"_当日盈亏缺失 `{missing_day_pnl_count}` 只：昨收或日变动数据不足（已避免误算）。_")
        if mode == "fast":
            lines.append("_当前为快速模式：未拉取实时昨收价，当日盈亏未计算。_")
        if total_weight > 100:
            lines.append("⚠️ 总仓位已超过 100%，请注意风险控制。")
        return BotResponse.markdown_response("\n".join(lines))

    def _sync_positions(self, owner_key: str, payload: List[str], message: BotMessage) -> BotResponse:
        """
        同步外部券商持仓到本地，目前支持 Longport。
        """
        target = (payload[0].strip().lower() if payload else "longport")
        if target not in {"longport", "长桥", "lb"}:
            return BotResponse.error_response("暂仅支持 `/position sync longport`")
        return self._sync_longport_positions(owner_key, message)

    def _sync_longport_positions(self, owner_key: str, message: BotMessage) -> BotResponse:
        db = get_db()
        positions, err = self._fetch_longport_stock_positions()
        if err:
            return BotResponse.error_response(f"长桥持仓同步失败: {err}")
        if not positions:
            return BotResponse.markdown_response("📭 长桥当前无股票持仓。")

        code_market_map = {p["code"]: p["market"] for p in positions}
        quotes = self._fetch_quote_snapshots([p["code"] for p in positions], code_market_map=code_market_map)

        cfg = db.get_portfolio_user_config(owner_key)
        total_asset_cny = float(cfg.get("total_asset_cny") or 0.0)
        usd_cny = float(cfg.get("usd_cny") or 6.94)
        hkd_cny = float(cfg.get("hkd_cny") or 0.888)

        prepared: List[Dict[str, Any]] = []
        for p in positions:
            code = p["code"]
            market = p["market"]
            shares = float(p["shares"] or 0.0)
            avg_cost = float(p["avg_cost"] or 0.0)
            if shares <= 0:
                continue
            if avg_cost == 0:
                # 长桥返回成本缺失时，回退现价作为占位成本，避免写入失败
                avg_cost = float((quotes.get(code) or {}).get("price") or 0.0)
            if avg_cost == 0:
                continue

            price = float((quotes.get(code) or {}).get("price") or 0.0)
            # 仅用于仓位估算的参考价格应为正值；成本本身保留原始符号。
            ref_price = price if price > 0 else abs(avg_cost)
            fx = 1.0
            if market == "US":
                fx = usd_cny
            elif market == "HK":
                fx = hkd_cny
            value_cny = ref_price * shares * fx
            prepared.append({
                "code": code,
                "name": p["name"] or code,
                "market": market,
                "shares": shares,
                "avg_cost": avg_cost,
                "value_cny": value_cny,
            })

        if not prepared:
            return BotResponse.error_response("长桥持仓同步失败：未获取到可写入的有效持仓（成本/股数为空）")

        weight_map = self._build_sync_weight_map(prepared, total_asset_cny=total_asset_cny)
        normalized_by_value = bool(weight_map.get("_normalized_by_value"))

        success_rows: List[str] = []
        failed_rows: List[str] = []
        for item in prepared:
            code = item["code"]
            weight_pct = float(weight_map.get(code) or 0.0)
            if weight_pct <= 0:
                failed_rows.append(f"{code} -> 仓位计算为 0，已跳过")
                continue
            result = db.upsert_holding(
                owner_key=owner_key,
                code=code,
                name=item["name"],
                market=item["market"],
                avg_cost=float(item["avg_cost"]),
                shares=float(item["shares"]),
                weight_pct=weight_pct,
                operator=f"{message.platform}:{message.user_id}:sync_longport",
                verified=True,
            )
            if result.get("success"):
                action = "新增" if result.get("action") == "add" else "更新"
                success_rows.append(
                    f"{action} {item['name']} (`{code}`) | 股数 `{item['shares']:.4f}` | "
                    f"成本 `{item['avg_cost']:.4f}` | 仓位 `{weight_pct:.2f}%`"
                )
            else:
                failed_rows.append(f"{code} -> {result.get('error', '写入失败')}")

        profile = db.get_portfolio_profile(owner_key)
        lines = [
            "✅ **长桥持仓同步完成**",
            "",
            f"• 成功: `{len(success_rows)}`",
            f"• 失败: `{len(failed_rows)}`",
            f"• 同步源: `Longport Trade API`",
            f"• 当前组合总仓位: `{float(profile.get('total_weight_pct') or 0.0):.2f}%`",
        ]
        if total_asset_cny <= 0:
            lines.append("• 仓位计算: 未设置总资产，已按持仓市值占比归一化到 100%")
        elif normalized_by_value:
            lines.append("• 仓位计算: 检测到总仓位超限，已按持仓市值占比归一化到 100%")
        else:
            lines.append(f"• 仓位计算: 按总资产 `{total_asset_cny:.2f} CNY` 换算")

        lines.append("")
        lines.append("**成功明细**")
        lines.extend([f"• {row}" for row in success_rows[:40]])
        if failed_rows:
            lines.extend(["", "**失败明细**"])
            lines.extend([f"• {row}" for row in failed_rows[:20]])
        return BotResponse.markdown_response("\n".join(lines))

    def _fetch_longport_stock_positions(self) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        try:
            from src.config import get_config
            from longport.openapi import Config as LongportConfig, TradeContext
        except Exception as e:
            return [], f"缺少 longport 依赖: {e}"

        cfg = get_config()
        app_key = (cfg.longport_app_key or "").strip()
        app_secret = (cfg.longport_app_secret or "").strip()
        access_token = (cfg.longport_access_token or "").strip()
        if not (app_key and app_secret and access_token):
            return [], "请先配置 LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN"

        try:
            lp_cfg = LongportConfig(
                app_key=app_key,
                app_secret=app_secret,
                access_token=access_token,
                http_url=cfg.longport_http_url or None,
                quote_ws_url=cfg.longport_quote_ws_url or None,
            )
            ctx = TradeContext(lp_cfg)
            resp = ctx.stock_positions()
        except Exception as e:
            return [], str(e)

        rows: List[Dict[str, Any]] = []
        for ch in getattr(resp, "channels", []) or []:
            for p in getattr(ch, "positions", []) or []:
                symbol = str(getattr(p, "symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                code, market = self._longport_symbol_to_internal(symbol)
                if not code:
                    continue
                name = str(getattr(p, "symbol_name", "") or "").strip() or code
                try:
                    shares = float(getattr(p, "quantity", 0) or 0)
                except Exception:
                    shares = 0.0
                try:
                    avg_cost = float(getattr(p, "cost_price", 0) or 0)
                except Exception:
                    avg_cost = 0.0
                rows.append({
                    "code": code,
                    "market": market,
                    "name": name,
                    "shares": shares,
                    "avg_cost": avg_cost,
                })
        return rows, None

    @staticmethod
    def _longport_symbol_to_internal(symbol: str) -> Tuple[Optional[str], Optional[str]]:
        s = (symbol or "").strip().upper()
        if s.endswith(".US"):
            return s[:-3], "US"
        if s.endswith(".HK"):
            raw = s[:-3]
            if raw.isdigit():
                return f"HK{raw.zfill(5)}", "HK"
            return None, None
        if s.endswith(".SH") or s.endswith(".SZ"):
            raw = s[:-3]
            if raw.isdigit() and len(raw) == 6:
                return raw, "CN"
            return None, None
        return None, None

    @staticmethod
    def _build_sync_weight_map(rows: List[Dict[str, Any]], total_asset_cny: float) -> Dict[str, float]:
        values = {r["code"]: max(0.0, float(r.get("value_cny") or 0.0)) for r in rows}
        total_value = sum(values.values())
        out: Dict[str, float] = {}
        normalized = False

        if total_asset_cny > 0:
            raw_weights = {code: (val / total_asset_cny * 100.0 if total_asset_cny > 0 else 0.0) for code, val in values.items()}
            raw_sum = sum(raw_weights.values())
            max_w = max(raw_weights.values()) if raw_weights else 0.0
            if raw_sum <= 100.0 + 1e-6 and max_w <= 100.0 + 1e-6:
                out.update(raw_weights)
            else:
                normalized = True
        else:
            normalized = True

        if normalized:
            if total_value <= 0:
                # 极端情况下等权
                n = max(1, len(rows))
                for r in rows:
                    out[r["code"]] = 100.0 / n
            else:
                for code, val in values.items():
                    out[code] = val / total_value * 100.0

        out["_normalized_by_value"] = 1.0 if normalized else 0.0
        return out

    @staticmethod
    def _infer_market_by_code(code: str) -> str:
        c = (code or "").strip().upper()
        if re.match(r"^\d{6}$", c):
            return "CN"
        if re.match(r"^HK\d{5}$", c) or re.match(r"^\d{5}$", c):
            return "HK"
        return "US"

    @staticmethod
    def _market_tz(market: str) -> ZoneInfo:
        m = (market or "").upper()
        if m == "US":
            return ZoneInfo("America/New_York")
        if m == "HK":
            return ZoneInfo("Asia/Hong_Kong")
        return ZoneInfo("Asia/Shanghai")

    @staticmethod
    def _is_market_open_now(market: str) -> bool:
        """
        交易时段判断（近似规则，足够用于“是否查实时”）。
        """
        m = (market or "").upper()
        now = datetime.now(PositionCommand._market_tz(m))
        if now.weekday() >= 5:  # 周末
            return False

        t = now.time()
        if m == "CN":
            return (dt_time(9, 30) <= t <= dt_time(11, 30)) or (dt_time(13, 0) <= t <= dt_time(15, 0))
        if m == "HK":
            return (dt_time(9, 30) <= t <= dt_time(12, 0)) or (dt_time(13, 0) <= t <= dt_time(16, 0))
        # US (NYSE/NASDAQ regular)
        return dt_time(9, 30) <= t <= dt_time(16, 0)

    @staticmethod
    def _market_day_str(market: str) -> str:
        now = datetime.now(PositionCommand._market_tz(market))
        return now.strftime("%Y-%m-%d")

    @staticmethod
    def _is_valid_prev_close_gap(latest_date: Optional[date], prev_date: Optional[date], market: str) -> bool:
        """
        校验“昨收”是否可信，避免本地日线断档导致把多日涨跌误算成当日盈亏。
        允许范围放宽到 7 个自然日，覆盖常见周末与小长假。
        """
        if not latest_date or not prev_date:
            return False
        gap = (latest_date - prev_date).days
        return 1 <= gap <= 7

    @classmethod
    def _is_local_day_pnl_date_available(cls, latest_date: Optional[date], market: str) -> bool:
        """
        本地日线是否可用于“当日盈亏”。
        - 工作日：仅当 latest_date == 市场今日，避免把“上一交易日涨跌”误当当日；
        - 周末：允许回退到最近一个交易日（通常周五），便于周末复盘查看。
        """
        if latest_date is None:
            return False
        now = datetime.now(cls._market_tz(market))
        market_today = now.date()
        if latest_date == market_today:
            return True
        if market_today.weekday() >= 5:
            gap = (market_today - latest_date).days
            return 1 <= gap <= 2 and latest_date.weekday() < 5
        return False

    @classmethod
    def _db_prev_close_candidate(cls, db, code: str, market: str) -> Optional[float]:
        """
        从本地日线推导“昨收候选值”。
        规则：
        - 若最新记录日期等于市场今日，则昨收取第 2 条 close；
        - 否则昨收取最新记录 close（表示本地尚未落今日K线）。
        """
        try:
            rows = db.get_latest_data(code, days=3)
            if not rows:
                return None
            market_today = datetime.now(cls._market_tz(market)).date()
            latest_date = getattr(rows[0], "date", None)
            # 本地日线过旧时不参与昨收校准，避免把多日变动误算成当日盈亏
            if latest_date is None or (market_today - latest_date).days > 7:
                return None
            # 仅在“本地日期可用于当日盈亏”时参与昨收校准：
            # - 工作日必须是市场当日
            # - 周末允许最近一个交易日（通常周五）
            # 这样可规避本地数据写入日期异常（如周末日期）导致的昨收误覆盖。
            if not cls._is_local_day_pnl_date_available(latest_date, market):
                return None
            if latest_date == market_today and len(rows) > 1 and rows[1].close:
                return float(rows[1].close)
            if rows[0].close:
                return float(rows[0].close)
        except Exception:
            return None
        return None

    @classmethod
    def _normalize_prev_close(
        cls,
        db,
        code: str,
        market: str,
        current_price: Optional[float],
        pre_close: Optional[float],
    ) -> Optional[float]:
        """
        对实时源返回的昨收做一致性校准，避免异常昨收放大当日盈亏。
        当本地候选昨收存在，且实时昨收偏差 > 2% 时，采用本地候选值。
        """
        symbol = f"{(market or '').upper()}:{(code or '').upper()}"
        # 美股优先信任 yfinance 的 pre_close，避免被本地旧日线覆盖放大日盈亏。
        m = (market or "").upper()
        if m == "US":
            candidate = cls._db_prev_close_candidate(db, code, market)
            # 实时昨收缺失时，直接用本地候选昨收
            if pre_close is None or pre_close <= 0:
                logger.info(
                    f"[POS PNL] prev_close归一化 {symbol} -> 使用本地昨收(实时缺失) "
                    f"rt={pre_close} db={candidate} final={candidate}"
                )
                return candidate
            # 本地候选缺失时，退回实时昨收
            if candidate is None or candidate <= 0:
                logger.info(
                    f"[POS PNL] prev_close归一化 {symbol} -> 使用实时昨收(本地缺失) "
                    f"rt={pre_close} db={candidate} final={pre_close}"
                )
                return pre_close
            # 两者偏差过大（>1.5%）时，优先使用本地候选昨收，避免异常值放大日盈亏
            if abs(float(pre_close) - candidate) / candidate > 0.015:
                logger.info(
                    f"[POS PNL] prev_close归一化 {symbol} -> 使用本地昨收(偏差过大) "
                    f"rt={pre_close} db={candidate} final={candidate}"
                )
                return candidate
            logger.info(
                f"[POS PNL] prev_close归一化 {symbol} -> 使用实时昨收(偏差可接受) "
                f"rt={pre_close} db={candidate} final={pre_close}"
            )
            return pre_close

        candidate = cls._db_prev_close_candidate(db, code, market)
        is_open_now = cls._is_market_open_now(m)

        # 非美股在开盘时优先信任实时昨收，避免本地日线滞后导致“当日涨跌幅”被压缩。
        if is_open_now:
            if pre_close is not None and pre_close > 0:
                logger.info(
                    f"[POS PNL] prev_close归一化 {symbol} -> 非美股开盘中使用实时昨收 "
                    f"rt={pre_close} db={candidate} final={pre_close}"
                )
                return pre_close
            if candidate is not None and candidate > 0:
                logger.info(
                    f"[POS PNL] prev_close归一化 {symbol} -> 非美股开盘中使用本地昨收(实时缺失) "
                    f"rt={pre_close} db={candidate} final={candidate}"
                )
                return candidate
            logger.info(
                f"[POS PNL] prev_close归一化 {symbol} -> 非美股开盘中昨收缺失 "
                f"rt={pre_close} db={candidate} final=None"
            )
            return None

        if candidate is None or candidate <= 0:
            logger.info(
                f"[POS PNL] prev_close归一化 {symbol} -> 非美股使用实时昨收(本地缺失) "
                f"rt={pre_close} db={candidate} final={pre_close}"
            )
            return pre_close
        if pre_close is None or pre_close <= 0:
            logger.info(
                f"[POS PNL] prev_close归一化 {symbol} -> 非美股使用本地昨收(实时缺失) "
                f"rt={pre_close} db={candidate} final={candidate}"
            )
            return candidate
        if abs(float(pre_close) - candidate) / candidate > 0.02:
            logger.info(
                f"[POS PNL] prev_close归一化 {symbol} -> 非美股使用本地昨收(偏差过大) "
                f"rt={pre_close} db={candidate} final={candidate}"
            )
            return candidate
        logger.info(
            f"[POS PNL] prev_close归一化 {symbol} -> 非美股使用实时昨收(偏差可接受) "
            f"rt={pre_close} db={candidate} final={pre_close}"
        )
        return pre_close

    @classmethod
    def _cache_key(cls, code: str, market: str) -> str:
        return f"{(market or '').upper()}:{(code or '').upper()}:{cls._market_day_str(market)}"

    @classmethod
    def _get_cached_daily_quote(cls, code: str, market: str) -> Optional[Dict[str, Optional[float]]]:
        key = cls._cache_key(code, market)
        with cls._daily_quote_cache_lock:
            return cls._daily_quote_cache.get(key)

    @classmethod
    def _set_cached_daily_quote(cls, code: str, market: str, quote: Dict[str, Optional[float]]) -> None:
        key = cls._cache_key(code, market)
        with cls._daily_quote_cache_lock:
            cls._daily_quote_cache[key] = quote

    @staticmethod
    def _derive_prev_close_from_quote(
        current_price: Optional[float],
        change_amount: Optional[float],
        change_pct: Optional[float],
    ) -> Optional[float]:
        """
        根据实时行情字段反推昨收，兼容部分数据源 pre_close 偶发异常。
        优先使用涨跌幅反推：pre_close = price / (1 + change_pct / 100)。
        """
        if current_price is None or current_price <= 0:
            return None

        # 优先使用涨跌幅（部分源在闭市时该字段更稳定）
        if change_pct is not None:
            try:
                den = 1.0 + float(change_pct) / 100.0
                if den > 0:
                    candidate = float(current_price) / den
                    if candidate > 0:
                        return candidate
            except Exception:
                pass

        # 回退到涨跌额
        if change_amount is not None:
            try:
                candidate = float(current_price) - float(change_amount)
                if candidate > 0:
                    return candidate
            except Exception:
                pass

        return None

    def _fetch_quote_snapshots(
        self,
        codes: List[str],
        code_market_map: Optional[Dict[str, str]] = None
    ) -> Dict[str, Dict[str, Optional[float]]]:
        manager = DataFetcherManager()
        quotes: Dict[str, Dict[str, Optional[float]]] = {}
        db = get_db()
        for code in codes:
            market = (code_market_map or {}).get(code) or self._infer_market_by_code(code)
            current_price: Optional[float] = None
            pre_close: Optional[float] = None
            open_price: Optional[float] = None
            change_amount: Optional[float] = None
            is_open_now = self._is_market_open_now(market)

            # 闭市优先用日级缓存/本地日线，避免每次打实时接口
            if not is_open_now:
                cached = self._get_cached_daily_quote(code, market)
                if cached:
                    # 闭市先使用缓存里的“价格/开盘”兜底；当日盈亏仍以下方本地日线
                    # + 实时源重算，避免缓存中的旧 pre_close 导致日盈亏误算。
                    current_price = cached.get("price")
                    open_price = cached.get("open")
                try:
                    latest = db.get_latest_data(code, days=2)
                    if latest and latest[0].close:
                        current_price = float(latest[0].close)
                        open_price = float(latest[0].open) if latest[0].open is not None else None
                        latest_date = getattr(latest[0], "date", None)
                        local_day_available = self._is_local_day_pnl_date_available(latest_date, market)
                        if (
                            local_day_available
                            and pre_close is None
                            and
                            len(latest) > 1
                            and latest[1].close
                            and self._is_valid_prev_close_gap(
                                getattr(latest[0], "date", None),
                                getattr(latest[1], "date", None),
                                market,
                            )
                        ):
                            pre_close = float(latest[1].close)
                        if local_day_available and pre_close is not None:
                            change_amount = current_price - pre_close
                        elif not local_day_available:
                            # 本地最新日线不是“市场当天”，不计算当日盈亏，避免误把前一日涨跌当今日。
                            pre_close = None
                            change_amount = None
                    snapshot = {
                        "price": current_price,
                        "pre_close": pre_close,
                        "open": open_price,
                        "change_amount": change_amount,
                    }
                    self._set_cached_daily_quote(code, market, snapshot)
                except Exception:
                    pass

            try:
                quote = manager.get_realtime_quote(code)
                if quote:
                    p = getattr(quote, "price", None)
                    pc = getattr(quote, "pre_close", None)
                    op = getattr(quote, "open_price", None)
                    ca = getattr(quote, "change_amount", None)
                    cp = getattr(quote, "change_pct", None)
                    if p is not None and p > 0:
                        current_price = float(p)
                    if pc is not None and pc > 0:
                        pre_close = float(pc)
                    if op is not None and op > 0:
                        open_price = float(op)
                    if ca is not None:
                        change_amount = float(ca)
                    derived_pre_close = self._derive_prev_close_from_quote(
                        current_price=current_price,
                        change_amount=change_amount,
                        change_pct=float(cp) if cp is not None else None,
                    )
                    if derived_pre_close is not None:
                        if pre_close is None or pre_close <= 0:
                            pre_close = derived_pre_close
                        else:
                            diff_ratio = abs(float(pre_close) - derived_pre_close) / derived_pre_close
                            if diff_ratio > 0.01:
                                logger.info(
                                    f"[POS PNL] prev_close校准 {(market or '').upper()}:{(code or '').upper()} -> "
                                    f"使用反推昨收 rt_pre_close={pre_close} derived={derived_pre_close}"
                                )
                                pre_close = derived_pre_close
                    pre_close = self._normalize_prev_close(
                        db=db,
                        code=code,
                        market=market,
                        current_price=current_price,
                        pre_close=pre_close,
                    )
                    if current_price is not None and pre_close is not None:
                        change_amount = current_price - pre_close
                if current_price is not None:
                    if pre_close is not None:
                        change_amount = current_price - pre_close
                    quotes[code] = {
                        "price": current_price,
                        "pre_close": pre_close,
                        "open": open_price,
                        "change_amount": change_amount,
                        "price_session": getattr(quote, "price_session", None),
                    }
                    continue
            except Exception:
                pass

            # 实时返回不完整时，补本地日线（尤其 pre_close）
            if current_price is None or pre_close is None or open_price is None:
                try:
                    latest = db.get_latest_data(code, days=2)
                    if latest:
                        if current_price is None and latest[0].close:
                            current_price = float(latest[0].close)
                        if open_price is None and latest[0].open is not None:
                            open_price = float(latest[0].open)
                        latest_date = getattr(latest[0], "date", None)
                        local_day_available = self._is_local_day_pnl_date_available(latest_date, market)
                        # 盘中且已拿到实时价时，不用本地旧日线补昨收，避免当日盈亏放大
                        if (
                            pre_close is None
                            and (not is_open_now or current_price is None)
                            and local_day_available
                        ):
                            if (
                                len(latest) > 1
                                and latest[1].close
                                and self._is_valid_prev_close_gap(
                                    getattr(latest[0], "date", None),
                                    getattr(latest[1], "date", None),
                                    market,
                                )
                            ):
                                pre_close = float(latest[1].close)
                        if change_amount is None and current_price is not None and pre_close is not None:
                            change_amount = current_price - pre_close
                except Exception:
                    pass

            try:
                latest = db.get_latest_data(code, days=1)
                if latest and latest[0].close:
                    if current_price is None:
                        current_price = float(latest[0].close)
                    if open_price is None and latest[0].open is not None:
                        open_price = float(latest[0].open)
                    if change_amount is None and current_price is not None and pre_close is not None:
                        change_amount = current_price - pre_close
            except Exception:
                pass
            quotes[code] = {
                "price": current_price,
                "pre_close": pre_close,
                "open": open_price,
                "change_amount": change_amount,
                "price_session": None,
            }
        return quotes

    @staticmethod
    def _price_session_text(raw: Optional[str]) -> str:
        s = (raw or "").strip().lower()
        mapping = {
            "regular": "[常规]",
            "pre": "[盘前]",
            "post": "[盘后]",
            "overnight": "[夜盘]",
        }
        return mapping.get(s, "")

    @classmethod
    def _resolve_display_session(cls, market: str, raw: Optional[str]) -> Optional[str]:
        """
        价格会话兜底：
        - 优先使用数据源返回的 price_session；
        - 美股若缺失则按纽约时间推断（盘前/常规/盘后/夜盘）。
        """
        normalized = (raw or "").strip().lower()
        if normalized in {"regular", "pre", "post", "overnight"}:
            return normalized

        m = (market or "").upper()
        if m != "US":
            return normalized or None

        now = datetime.now(cls._market_tz("US"))
        if now.weekday() >= 5:
            return normalized or None

        t = now.time()
        if dt_time(4, 0) <= t < dt_time(9, 30):
            return "pre"
        if dt_time(9, 30) <= t <= dt_time(16, 0):
            return "regular"
        if dt_time(16, 0) < t <= dt_time(20, 0):
            return "post"
        return "overnight"

    @staticmethod
    def _parse_set_payload(payload: List[str]) -> Dict[str, Any]:
        """
        解析 set 参数：
          <代码|名称> <成本> <仓位%> [市场]
          <代码|名称> <成本> <股数> <仓位%> [市场]
          <代码|名称> <成本> <股数> [市场]   # 需先配置 /position total
          若名称有空格，取最后 2~4 个 token 做数字+市场解析。
        """
        tokens = [x.strip() for x in payload if x.strip()]
        if len(tokens) < 3:
            return {"error": "参数不足，用法: `/position set <代码|名称> <成本> <仓位%> [市场]`"}

        market = None
        maybe_market = PositionCommand._normalize_market(tokens[-1])
        if maybe_market:
            market = maybe_market
            tokens = tokens[:-1]
            if len(tokens) < 3:
                return {"error": "参数不足，用法: `/position set <代码|名称> <成本> <仓位%> [市场]`"}

        shares = None
        weight_pct = None
        metric_value = None
        metric_is_percent = False

        try:
            # 三数字模式：<成本> <股数> <仓位%>
            maybe_cost = float(tokens[-3]) if len(tokens) >= 4 else None
            maybe_shares = float(tokens[-2]) if len(tokens) >= 4 else None
            maybe_weight = float(tokens[-1].replace("%", "")) if len(tokens) >= 4 else None
            if maybe_cost is not None and maybe_shares is not None and maybe_weight is not None:
                avg_cost = maybe_cost
                shares = maybe_shares
                weight_pct = maybe_weight
                query_tokens = tokens[:-3]
            else:
                raise ValueError("fallback")
        except Exception:
            # 双数字模式：<成本> <仓位%>（兼容旧用法）或 <成本> <股数>（配置 total 后自动换算）
            try:
                avg_cost = float(tokens[-2])
                metric_value = float(tokens[-1].replace("%", ""))
                metric_is_percent = "%" in tokens[-1]
                query_tokens = tokens[:-2]
            except ValueError:
                return {"error": "参数格式错误，例如：`/position set 腾讯控股 320 8 HK` 或 `/position set 腾讯控股 320 100 HK`"}

        query = " ".join(query_tokens).strip()
        if not query:
            return {"error": "缺少股票代码或名称"}

        if shares is not None and shares <= 0:
            return {"error": "持仓股数必须大于 0"}
        if weight_pct is not None and (weight_pct <= 0 or weight_pct > 100):
            return {"error": "持仓比例必须在 (0, 100] 范围内"}
        if metric_value is not None and metric_value <= 0:
            return {"error": "股数/仓位必须大于 0"}

        return {
            "query": query,
            "avg_cost": avg_cost,
            "shares": shares,
            "weight_pct": weight_pct,
            "metric_value": metric_value,
            "metric_is_percent": metric_is_percent,
            "market": market,
        }

    @staticmethod
    def _parse_sell_payload(payload: List[str]) -> Dict[str, Any]:
        """
        解析 sell 参数：
          <代码|名称> <卖出价> <卖出股数> [市场]
        """
        tokens = [x.strip() for x in payload if x.strip()]
        if len(tokens) < 3:
            return {"error": "参数不足，用法: `/position sell <代码|名称> <卖出价> <卖出股数> [市场]`"}

        market = None
        maybe_market = PositionCommand._normalize_market(tokens[-1])
        if maybe_market:
            market = maybe_market
            tokens = tokens[:-1]
            if len(tokens) < 3:
                return {"error": "参数不足，用法: `/position sell <代码|名称> <卖出价> <卖出股数> [市场]`"}

        try:
            sell_shares = float(tokens[-1].replace("%", ""))
            sell_price = float(tokens[-2])
        except ValueError:
            return {"error": "卖出价格和股数必须是数字，例如：`/position sell TSM 188 30`"}

        query = " ".join(tokens[:-2]).strip()
        if not query:
            return {"error": "缺少股票代码或名称"}
        if sell_price <= 0:
            return {"error": "卖出价格必须大于 0"}
        if sell_shares <= 0:
            return {"error": "卖出股数必须大于 0"}

        return {
            "query": query,
            "sell_price": sell_price,
            "sell_shares": sell_shares,
            "market": market,
        }

    @staticmethod
    def _looks_like_batch_payload(payload: List[str]) -> bool:
        text = " ".join([x for x in payload if x]).strip()
        if not text:
            return False
        return any(sep in text for sep in [";", "；", "\n", "|"])

    @staticmethod
    def _market_name(market: str) -> str:
        return {"CN": "A股", "HK": "港股", "US": "美股"}.get((market or "").upper(), market)

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
    def _is_valid_code(code: str) -> bool:
        code = (code or "").upper().strip()
        if re.match(r"^\d{6}$", code):
            return True
        if re.match(r"^HK\d{5}$", code):
            return True
        if re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", code):
            return True
        return False

    @staticmethod
    def _normalize_code_and_market(code: str, market: Optional[str]) -> Tuple[str, str, Optional[str]]:
        code = (code or "").strip().upper()
        market = (market or "").strip().upper() or None

        if re.match(r"^HK\d{5}$", code):
            inferred = "HK"
            normalized = code
        elif re.match(r"^\d{5}$", code):
            inferred = "HK"
            normalized = f"HK{code}"
        elif re.match(r"^\d{6}$", code):
            inferred = "CN"
            normalized = code
        elif re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", code):
            inferred = "US"
            normalized = code
        else:
            return "", "", f"无效股票代码: {code}"

        final_market = market or inferred
        if final_market != inferred:
            if market == "HK" and re.match(r"^\d{5}$", code):
                return normalized, "HK", None
            return "", "", f"代码与市场不匹配: {code} / {final_market}"
        return normalized, final_market, None

    def _resolve_stock(self, query: str, market: Optional[str]) -> Tuple[str, str, str, Optional[str]]:
        q = (query or "").strip()
        if not q:
            return "", "", "", "请输入股票代码或名称"

        q_upper = q.upper()
        normalized_code, normalized_market, err = self._normalize_code_and_market(q_upper, market)
        if not err:
            name = (
                STOCK_NAME_MAP.get(normalized_code)
                or STOCK_NAME_MAP.get(normalized_code.replace("HK", ""))
                or normalized_code
            )
            # 如果静态映射没命中，走搜索服务再确认名称
            if name == normalized_code:
                hit = self._search_stock(normalized_code, normalized_market)
                if hit:
                    name = hit.get("name") or name
            if name == normalized_code:
                hist_name = self._resolve_name_from_history(normalized_code)
                if hist_name:
                    name = hist_name
            if name == normalized_code:
                ai_name = self._resolve_name_with_ai(normalized_code, normalized_market)
                if ai_name:
                    name = ai_name
            if not name or name == normalized_code:
                return "", "", "", f"无法确认股票名称: {normalized_code}"
            return normalized_code, name, normalized_market, None

        # 按名称解析
        for code, name in STOCK_NAME_MAP.items():
            if (name or "").strip().lower() == q.lower():
                candidate = f"HK{code}" if re.match(r"^\d{5}$", code) else code
                candidate_market = "HK" if candidate.startswith("HK") else ("CN" if candidate.isdigit() else "US")
                if market and candidate_market != market:
                    continue
                return candidate, name, candidate_market, None

        hit = self._search_stock(q, market)
        if hit:
            return (
                (hit.get("code") or "").upper(),
                (hit.get("name") or "").strip() or (hit.get("code") or "").upper(),
                (hit.get("market") or "CN").upper(),
                None,
            )

        return "", "", "", f"未找到匹配股票：{query}"

    @staticmethod
    def _search_stock(keyword: str, market: Optional[str]) -> Optional[Dict[str, str]]:
        try:
            from web.services import get_stock_search_service
            service = get_stock_search_service()
            resp = service.search(keyword=keyword, market=market, limit=1)
            data = resp.get("data", []) if isinstance(resp, dict) else []
            if data:
                return data[0]
        except Exception:
            return None
        return None

    @staticmethod
    def _resolve_name_from_history(code: str) -> Optional[str]:
        """
        当搜索服务不可用或未命中时，从本地历史分析记录回退名称。
        """
        try:
            from src.storage import get_db
            db = get_db()
            rows = db.get_analysis_history(code=code, days=3650, limit=1)
            if rows:
                name = (getattr(rows[0], "name", "") or "").strip()
                if name and name.upper() != code.upper():
                    return name
        except Exception:
            pass
        return None

    @staticmethod
    def _resolve_name_with_ai(code: str, market: str) -> Optional[str]:
        """
        最后一层兜底：让 AI 根据“代码+市场”推断标准股票简称。
        """
        try:
            from src.config import get_config
            cfg = get_config()
            if not (cfg.gemini_api_key or cfg.openai_api_key):
                return None

            from src.analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer()
            if not analyzer.is_available():
                return None

            prompt = (
                "你是证券代码助手。请根据给定代码和市场，返回该标的常用中文/英文简称。"
                "如果无法确定，返回空字符串。"
                "仅输出 JSON: {\"name\":\"...\"}，不要输出任何其它文本。\n"
                f"代码: {code}\n"
                f"市场: {market}\n"
            )
            out = analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.0, "max_output_tokens": 80},
            )
            raw = (out or "").strip()
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return None
            data = json.loads(m.group(0))
            name = str(data.get("name", "") or "").strip()
            if not name:
                return None
            # 过滤无效/高风险输出
            if name.upper() == code.upper():
                return None
            if re.match(r"^(UNKNOWN|N/A|NONE|无|未知)$", name, re.IGNORECASE):
                return None
            if len(name) > 64:
                return None
            return name
        except Exception:
            return None
