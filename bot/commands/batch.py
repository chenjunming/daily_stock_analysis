# -*- coding: utf-8 -*-
"""
===================================
批量分析命令
===================================

批量分析自选股列表中的所有股票。
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional
import re

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.analyzer import STOCK_NAME_MAP, AnalysisResult
from src.time_utils import utc8_now, utc8_today, as_utc8

logger = logging.getLogger(__name__)


class BatchCommand(BotCommand):
    """
    批量分析命令
    
    批量分析配置中的自选股列表，生成汇总报告。
    
    用法：
        /batch      - 分析所有自选股
        /batch 3    - 只分析前3只
        /batch HK   - 只分析港股
        /batch US 5 - 只分析前5只美股
        /batch holdings US - 按持仓分析美股
    """
    
    @property
    def name(self) -> str:
        return "batch"
    
    @property
    def aliases(self) -> List[str]:
        return ["b", "批量", "全部"]
    
    @property
    def description(self) -> str:
        return "批量分析自选股"
    
    @property
    def usage(self) -> str:
        return "/batch [holdings|position] [CN|HK|US|A股|港股|美股] [数量]"
    
    @property
    def admin_only(self) -> bool:
        """批量分析需要管理员权限（防止滥用）"""
        return False  # 可以根据需要设为 True
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行批量分析命令"""
        from src.config import get_config
        from src.storage import get_db
        
        config = get_config()
        db = get_db()

        source, market, limit, parse_error = self._parse_source_market_and_limit(args)
        if parse_error:
            return BotResponse.error_response(parse_error)

        watchlist_items = db.get_watchlist()
        if source == "holdings":
            owner_key = self._build_owner_key(message)
            if not owner_key:
                return BotResponse.error_response("无法识别用户身份，不能读取持仓")
            holding_rows = db.list_holdings(owner_key)
            if market:
                stock_list = [
                    row.code for row in holding_rows
                    if getattr(row, "code", None) and (getattr(row, "market", "") or "").upper() == market
                ]
            else:
                stock_list = [row.code for row in holding_rows if getattr(row, "code", None)]
        else:
            # 优先使用 WebUI 管理的数据库自选股；为空时回退到 .env 的 STOCK_LIST
            if market:
                stock_list = [
                    item.code for item in watchlist_items
                    if getattr(item, "code", None) and (getattr(item, "market", "") or "").upper() == market
                ]
            else:
                stock_list = [item.code for item in watchlist_items if getattr(item, "code", None)]
            if not stock_list:
                config.refresh_stock_list()
                if market:
                    stock_list = [c for c in config.stock_list if self._infer_market(c) == market]
                else:
                    stock_list = config.stock_list

        if not stock_list:
            if market:
                return BotResponse.error_response(
                    f"{self._market_name(market)} {('持仓' if source == 'holdings' else '自选股')}为空，请先补充数据"
                )
            return BotResponse.error_response(
                "列表为空，请先补充持仓或自选股"
            )
        
        # 限制分析数量
        if limit:
            stock_list = stock_list[:limit]

        code_name_map = self._build_code_name_map(stock_list, watchlist_items)
        
        logger.info(
            f"[BatchCommand] 开始批量分析 {len(stock_list)} 只股票"
            + (f", market={market}" if market else "")
        )
        
        # 在后台线程中执行分析
        thread = threading.Thread(
            target=self._run_batch_analysis,
            args=(stock_list, code_name_map, message, market),
            daemon=True
        )
        thread.start()

        preview = []
        for code in stock_list[:5]:
            preview.append(self._display_code(code, code_name_map))

        market_desc = f"{self._market_name(market)} | " if market else ""
        source_desc = "持仓" if source == "holdings" else "自选股"
        return BotResponse.markdown_response(
            f"✅ **批量分析任务已启动**\n\n"
            f"• 范围: {market_desc}{source_desc}\n"
            f"• 分析数量: {len(stock_list)} 只\n"
            f"• 股票列表: {', '.join(preview)}"
            f"{'...' if len(stock_list) > 5 else ''}\n\n"
            f"分析完成后将自动推送汇总报告。"
        )
    
    def _run_batch_analysis(
        self,
        stock_list: List[str],
        code_name_map: Dict[str, str],
        message: BotMessage,
        market: Optional[str] = None
    ) -> None:
        """后台执行批量分析"""
        try:
            from src.config import get_config
            from src.enums import ReportType
            from src.core.pipeline import StockAnalysisPipeline
            from bot.platforms.feishu_stream import (
                FEISHU_SDK_AVAILABLE,
                FeishuReplyClient,
                FeishuCardStreamSession,
            )
            
            config = get_config()
            report_type_str = getattr(config, 'report_type', 'simple').lower()
            report_type = ReportType.FULL if report_type_str == "full" else ReportType.SIMPLE
            started_at = utc8_now()

            batch_status: Dict[str, Dict[str, Any]] = {
                code: {"status": "queued", "detail": "排队中"} for code in stock_list
            }
            done = 0
            success = 0
            failed = 0
            current_code = "-"

            stream_session = None
            if (
                message.platform == "feishu"
                and getattr(config, "feishu_stream_enabled", False)
                and getattr(config, "feishu_stream_card_enabled", True)
                and FEISHU_SDK_AVAILABLE
                and message.chat_id
                and config.feishu_app_id
                and config.feishu_app_secret
            ):
                try:
                    reply_client = FeishuReplyClient(config.feishu_app_id, config.feishu_app_secret)
                    stream_session = FeishuCardStreamSession(
                        reply_client=reply_client,
                        chat_id=message.chat_id,
                        update_interval=float(getattr(config, "feishu_stream_card_update_interval", 2.0)),
                        max_updates=int(getattr(config, "feishu_stream_card_max_updates", 120)),
                    )
                    stream_session.start(
                        self._render_batch_progress_card(
                            batch_id="batch_" + uuid.uuid4().hex[:8],
                            stock_list=stock_list,
                            code_name_map=code_name_map,
                            batch_status=batch_status,
                            done=done,
                            success=success,
                            failed=failed,
                            current_code=current_code,
                            started_at=started_at,
                            version=1,
                            market=market,
                        )
                    )
                except Exception as e:
                    logger.warning(f"[BatchCommand] 初始化批量流式卡片失败，回退普通模式: {e}")
                    stream_session = None

            def progress_reporter(event: Dict[str, Any]) -> None:
                nonlocal done, success, failed, current_code
                code = event.get("code")
                if not code or code not in batch_status:
                    return
                stage = str(event.get("stage", "running"))
                detail = str(event.get("detail", "处理中"))
                current_code = code
                batch_status[code]["status"] = stage
                batch_status[code]["detail"] = detail
                if stage in {"stock_done", "analyze_done"} and batch_status[code].get("_counted") is not True:
                    batch_status[code]["_counted"] = True
                    done += 1
                    success += 1
                if stage in {"stock_failed", "stock_error", "analyze_error"} and batch_status[code].get("_counted") is not True:
                    batch_status[code]["_counted"] = True
                    done += 1
                    failed += 1
                if stream_session:
                    content = self._render_batch_progress_card(
                        batch_id="batch",
                        stock_list=stock_list,
                        code_name_map=code_name_map,
                        batch_status=batch_status,
                        done=done,
                        success=success,
                        failed=failed,
                        current_code=current_code,
                        started_at=started_at,
                        version=stream_session.version + 1,
                        market=market,
                    )
                    stream_session.update(content)
            
            # 创建分析管道
            pipeline = StockAnalysisPipeline(
                config=config,
                source_message=message,
                query_id=uuid.uuid4().hex,
                query_source="bot",
                progress_reporter=progress_reporter
            )
            
            # 执行分析（逐只处理，便于批量进度流式更新）
            results = []
            for code in stock_list:
                try:
                    cached_result = self._load_today_analysis_result(code)
                    if cached_result:
                        results.append(cached_result)
                        if batch_status[code].get("_counted") is not True:
                            batch_status[code]["_counted"] = True
                            batch_status[code]["status"] = "cached"
                            batch_status[code]["detail"] = f"{code} 今日已分析，复用历史结果"
                            current_code = code
                            done += 1
                            success += 1
                            if stream_session:
                                content = self._render_batch_progress_card(
                                    batch_id="batch",
                                    stock_list=stock_list,
                                    code_name_map=code_name_map,
                                    batch_status=batch_status,
                                    done=done,
                                    success=success,
                                    failed=failed,
                                    current_code=current_code,
                                    started_at=started_at,
                                    version=stream_session.version + 1,
                                    market=market,
                                )
                                stream_session.update(content, force=True)
                        continue

                    result = pipeline.process_single_stock(
                        code=code,
                        skip_analysis=False,
                        single_stock_notify=False,
                        report_type=report_type,
                    )
                    if result:
                        results.append(result)
                except Exception as e:
                    logger.error(f"[BatchCommand] 批处理单股失败 code={code}: {e}")
                    if batch_status[code].get("_counted") is not True:
                        batch_status[code]["_counted"] = True
                        batch_status[code]["status"] = "stock_error"
                        batch_status[code]["detail"] = str(e)[:100]
                        done += 1
                        failed += 1

            # 汇总落库（不推送全量个股报告，详情通过卡片“查看详情”按需获取）
            if results:
                # 仅保存本地报告，不做渠道推送，避免刷屏
                pipeline._send_notifications(results, skip_push=True)

            if stream_session:
                final_card = self._render_batch_final_card(
                    stock_list=stock_list,
                    code_name_map=code_name_map,
                    results=results,
                    batch_status=batch_status,
                    done=done,
                    success=success,
                    failed=failed,
                    started_at=started_at,
                    version=stream_session.version + 1,
                    market=market,
                )
                stream_session.finish(final_card)
                # 额外发送一张“摘要+按钮”卡片，避免单卡更新失败导致用户看不到关键内容
                try:
                    stream_session.reply_client.create_card_to_chat(
                        chat_id=message.chat_id,
                        content=self._render_batch_summary_action_card(
                            stock_list=stock_list,
                            code_name_map=code_name_map,
                            results=results,
                            batch_status=batch_status,
                            done=done,
                            success=success,
                            failed=failed,
                            started_at=started_at,
                            market=market,
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[BatchCommand] 发送批量摘要副卡片失败: {e}")
            
            logger.info(f"[BatchCommand] 批量分析完成，成功 {len(results)} 只")
            
        except Exception as e:
            logger.error(f"[BatchCommand] 批量分析失败: {e}")
            logger.exception(e)

    @staticmethod
    def _render_batch_progress_card(
        batch_id: str,
        stock_list: List[str],
        code_name_map: Dict[str, str],
        batch_status: Dict[str, Dict[str, Any]],
        done: int,
        success: int,
        failed: int,
        current_code: str,
        started_at: datetime,
        version: int,
        market: Optional[str] = None,
    ) -> str:
        total = len(stock_list)
        now = utc8_now()
        elapsed = int((now - started_at).total_seconds())
        percent = int((done / total) * 100) if total > 0 else 0
        lines = [
            "## 📦 批量分析进行中",
            "",
            f"- 批次ID: `{batch_id}`",
            f"- 市场: {BatchCommand._market_name(market) if market else '全部'}",
            f"- 总数: **{total}**",
            f"- 已完成: **{done}/{total}** ({percent}%)",
            f"- 成功: **{success}**",
            f"- 失败: **{failed}**",
            f"- 当前: {BatchCommand._display_stock(current_code, code_name_map)}",
            f"- 已耗时: {elapsed}s",
            "",
            "### 任务明细（最近）",
        ]
        for code in stock_list[:20]:
            st = batch_status.get(code, {})
            stage = st.get("status", "queued")
            detail = BatchCommand._replace_code_with_name(
                st.get("detail", ""), code, code_name_map
            )
            lines.append(f"- {BatchCommand._display_stock(code, code_name_map)}: {stage} | {detail}")
        lines.append("")
        lines.append(f"`v{version}` | 更新时间: {now.strftime('%H:%M:%S')} (UTC+8)")
        return "\n".join(lines)

    @staticmethod
    def _render_batch_final_card(
        stock_list: List[str],
        code_name_map: Dict[str, str],
        results: List[AnalysisResult],
        batch_status: Dict[str, Dict[str, Any]],
        done: int,
        success: int,
        failed: int,
        started_at: datetime,
        version: int,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc8_now()
        elapsed = int((now - started_at).total_seconds())
        lines = [
            "## ✅ 批量分析完成",
            "",
        ]

        # 决策仪表盘摘要（置顶，提升可读性）
        lines.extend(
            BatchCommand._build_dashboard_summary(
                results=results,
                code_name_map=code_name_map,
                stock_list=stock_list,
                batch_status=batch_status,
            )
        )

        lines.extend([
            f"- 市场: {BatchCommand._market_name(market) if market else '全部'}",
            f"- 总数: **{len(stock_list)}**",
            f"- 已完成: **{done}**",
            f"- 成功: **{success}**",
            f"- 失败: **{failed}**",
            f"- 总耗时: {elapsed}s",
            "",
            "点击下方按钮可查看单股详细报告。",
            "若按钮异常，可发送：`/history <股票代码>`",
            "",
            "### 失败明细",
        ])
        fail_rows = []
        for code in stock_list:
            st = batch_status.get(code, {})
            stage = st.get("status", "")
            if stage in {"stock_failed", "stock_error", "analyze_error"}:
                detail = BatchCommand._replace_code_with_name(st.get('detail', stage), code, code_name_map)
                fail_rows.append(f"- {BatchCommand._display_stock(code, code_name_map)}: {detail}")
        if fail_rows:
            lines.extend(fail_rows[:20])
        else:
            lines.append("- 无")
        lines.append("")
        lines.append(f"`v{version}` | 完成时间: {now.strftime('%H:%M:%S')} (UTC+8)")
        summary_md = "\n".join(lines)

        # 展示全部成功标的按钮
        success_codes = []
        for code in stock_list:
            st = batch_status.get(code, {})
            stage = st.get("status", "")
            if stage not in {"stock_failed", "stock_error", "analyze_error"}:
                success_codes.append(code)

        action_buttons = []
        for code in success_codes:
            name = code_name_map.get(code, code)
            button_text = f"查看 {name} 详情"
            if len(button_text) > 16:
                button_text = f"查看 {name[:10]}…"
            action_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": button_text},
                    "type": "default",
                    "value": {"action": "show_stock_detail", "code": code},
                }
            )

        elements = [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": summary_md}
            }
        ]
        if action_buttons:
            elements.append(
                {
                    "tag": "action",
                    "actions": action_buttons,
                }
            )

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "批量分析结果"},
                "template": "green",
            },
            "elements": elements,
        }

    @staticmethod
    def _render_batch_summary_action_card(
        stock_list: List[str],
        code_name_map: Dict[str, str],
        results: List[AnalysisResult],
        batch_status: Dict[str, Dict[str, Any]],
        done: int,
        success: int,
        failed: int,
        started_at: datetime,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        批量完成后补发一张更短的摘要卡片，降低更新失败后的信息缺失风险。
        """
        now = utc8_now()
        elapsed = int((now - started_at).total_seconds())
        lines = [
            "## ✅ 批量分析摘要",
            "",
        ]
        lines.extend(
            BatchCommand._build_dashboard_summary(
                results=results,
                code_name_map=code_name_map,
                stock_list=stock_list,
                batch_status=batch_status,
            )
        )
        lines.extend([
            f"- 市场: {BatchCommand._market_name(market) if market else '全部'}",
            f"- 总数: **{len(stock_list)}** | 完成: **{done}** | 成功: **{success}** | 失败: **{failed}** | 耗时: {elapsed}s",
            "",
            "点击下方按钮查看个股详情",
            "",
            f"完成时间: {now.strftime('%H:%M:%S')} (UTC+8)",
        ])
        summary_md = "\n".join(lines)

        success_codes = []
        for code in stock_list:
            st = batch_status.get(code, {})
            stage = st.get("status", "")
            if stage not in {"stock_failed", "stock_error", "analyze_error"}:
                success_codes.append(code)

        action_buttons = []
        for code in success_codes:
            name = code_name_map.get(code, code)
            button_text = f"查看 {name} 详情"
            if len(button_text) > 16:
                button_text = f"查看 {name[:10]}…"
            action_buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": button_text},
                    "type": "default",
                    "value": {"action": "show_stock_detail", "code": code},
                }
            )

        elements = [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": summary_md},
            }
        ]
        if action_buttons:
            elements.append({"tag": "action", "actions": action_buttons})

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "批量分析摘要"},
                "template": "blue",
            },
            "elements": elements,
        }

    @staticmethod
    def _parse_source_market_and_limit(args: List[str]) -> tuple[str, Optional[str], Optional[int], Optional[str]]:
        source = "watchlist"
        market = None
        limit = None
        source_map = {
            "HOLDING": "holdings",
            "HOLDINGS": "holdings",
            "POSITION": "holdings",
            "POSITIONS": "holdings",
            "持仓": "holdings",
        }
        market_map = {
            "CN": "CN", "A股": "CN", "A": "CN",
            "HK": "HK", "港股": "HK", "H": "HK",
            "US": "US", "美股": "US", "USA": "US",
        }
        for raw in args:
            token = (raw or "").strip()
            if not token:
                continue
            src = source_map.get(token.upper(), source_map.get(token))
            if src:
                source = src
                continue
            m = market_map.get(token.upper(), market_map.get(token))
            if m:
                if market and market != m:
                    return source, None, None, f"市场参数冲突: {market} 与 {m}"
                market = m
                continue
            try:
                value = int(token)
                if value <= 0:
                    return source, None, None, "数量必须大于0"
                if limit is not None:
                    return source, None, None, f"重复数量参数: {token}"
                limit = value
            except ValueError:
                return source, None, None, f"无效参数: {token}（应为数据源/市场/数量）"
        return source, market, limit, None

    @staticmethod
    def _build_owner_key(message: BotMessage) -> str:
        platform = (message.platform or "").strip().lower()
        user_id = (message.user_id or "").strip()
        if not platform or not user_id:
            return ""
        return f"{platform}:{user_id}".lower()

    @staticmethod
    def _infer_market(code: str) -> str:
        c = (code or "").strip().upper()
        if re.match(r"^\d{6}$", c):
            return "CN"
        if re.match(r"^HK\d{5}$", c) or re.match(r"^\d{5}$", c):
            return "HK"
        return "US"

    @staticmethod
    def _market_name(market: Optional[str]) -> str:
        names = {"CN": "A股", "HK": "港股", "US": "美股"}
        if not market:
            return "全部"
        return names.get(market, market)

    @staticmethod
    def _build_code_name_map(stock_list: List[str], watchlist_items: List[Any]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for item in watchlist_items or []:
            code = (getattr(item, "code", "") or "").upper().strip()
            name = (getattr(item, "name", "") or "").strip()
            if code and name:
                mapping[code] = name

        for code in stock_list:
            if code in mapping:
                continue
            name = (
                STOCK_NAME_MAP.get(code)
                or STOCK_NAME_MAP.get(code.replace("HK", ""))
                or code
            )
            mapping[code] = name
        return mapping

    @staticmethod
    def _display_stock(code: str, code_name_map: Dict[str, str]) -> str:
        c = (code or "").strip().upper()
        if not c or c == "-":
            return "`-`"
        name = (code_name_map or {}).get(c, c)
        return f"{name} (`{c}`)"

    @staticmethod
    def _display_code(code: str, code_name_map: Dict[str, str]) -> str:
        c = (code or "").strip().upper()
        name = (code_name_map or {}).get(c, c)
        if name == c:
            return c
        return f"{name}({c})"

    @staticmethod
    def _replace_code_with_name(detail: str, code: str, code_name_map: Dict[str, str]) -> str:
        text = str(detail or "")
        c = (code or "").strip().upper()
        if not c:
            return text
        name = (code_name_map or {}).get(c, c)
        if name != c:
            return text.replace(c, f"{name}({c})")
        return text

    @staticmethod
    def _build_dashboard_summary(
        results: List[AnalysisResult],
        code_name_map: Dict[str, str],
        stock_list: List[str],
        batch_status: Dict[str, Dict[str, Any]],
    ) -> List[str]:
        """
        生成批量卡片中的“决策仪表盘摘要”。
        """
        success_codes: List[str] = []
        for code in stock_list:
            stage = (batch_status.get(code, {}) or {}).get("status", "")
            if stage not in {"stock_failed", "stock_error", "analyze_error"}:
                success_codes.append(code)

        if not results:
            lines = [
                "### 决策仪表盘摘要",
                f"- 共处理 {len(stock_list)} 只 | 成功: {len(success_codes)} | 失败: {max(0, len(stock_list) - len(success_codes))}",
                "",
                "### 分析结果摘要（全部）",
            ]
            if not success_codes:
                lines.append("- 暂无可展示结果")
            else:
                for code in success_codes:
                    name = (code_name_map or {}).get(code, code)
                    lines.append(f"- {name}(`{code}`): 已完成 | 评分 - | 请点击下方按钮查看详情")
            lines.append("")
            return lines

        buy = 0
        hold = 0
        sell = 0
        for r in results:
            advice = (getattr(r, "operation_advice", "") or "").strip()
            if any(k in advice for k in ["买", "加仓"]):
                buy += 1
            elif any(k in advice for k in ["卖", "减仓"]):
                sell += 1
            else:
                hold += 1

        sorted_results = sorted(
            results,
            key=lambda x: int(getattr(x, "sentiment_score", 0) or 0),
            reverse=True
        )

        lines = [
            "### 决策仪表盘摘要",
            f"- 共分析 {len(results)} 只 | 🟢买入/加仓: {buy} | ⚪观望/持有: {hold} | 🔴卖出/减仓: {sell}",
            "",
            "### 分析结果摘要（全部）",
        ]
        for r in sorted_results:
            code = (getattr(r, "code", "") or "").upper()
            name = (code_name_map or {}).get(code, getattr(r, "name", "") or code)
            advice = (getattr(r, "operation_advice", "") or "-").strip()
            trend = (getattr(r, "trend_prediction", "") or "-").strip()
            score = int(getattr(r, "sentiment_score", 0) or 0)
            lines.append(f"- {name}(`{code}`): {advice} | 评分 {score} | {trend}")
        lines.append("")
        return lines

    @staticmethod
    def _load_today_analysis_result(code: str) -> Optional[AnalysisResult]:
        """
        读取当天已生成的分析结果，存在则直接复用。
        """
        try:
            from src.storage import get_db

            db = get_db()
            history = db.get_analysis_history(code=code, days=1, limit=20)
            if not history:
                return None

            today = utc8_today()
            record = None
            for row in history:
                created_at = getattr(row, "created_at", None)
                created_at_utc8 = as_utc8(created_at)
                if created_at_utc8 and created_at_utc8.date() == today:
                    record = row
                    break
            if record is None:
                return None

            raw = {}
            try:
                raw = json.loads(getattr(record, "raw_result", "") or "{}")
            except Exception:
                raw = {}

            return AnalysisResult(
                code=(raw.get("code") or getattr(record, "code", code) or code),
                name=(raw.get("name") or getattr(record, "name", code) or code),
                sentiment_score=int(raw.get("sentiment_score") or getattr(record, "sentiment_score", 50) or 50),
                trend_prediction=(raw.get("trend_prediction") or getattr(record, "trend_prediction", "震荡") or "震荡"),
                operation_advice=(raw.get("operation_advice") or getattr(record, "operation_advice", "观望") or "观望"),
                decision_type=(raw.get("decision_type") or "hold"),
                confidence_level=(raw.get("confidence_level") or "中"),
                dashboard=raw.get("dashboard"),
                analysis_summary=(raw.get("analysis_summary") or getattr(record, "analysis_summary", "") or ""),
            )
        except Exception as e:
            logger.debug(f"[BatchCommand] 读取当日缓存失败 code={code}: {e}")
            return None
