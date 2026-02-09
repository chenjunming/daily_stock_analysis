# -*- coding: utf-8 -*-
"""
===================================
股票分析命令
===================================

分析指定股票，调用 AI 生成分析报告。
"""

import json
import re
import logging
from typing import List, Optional

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse
from src.analyzer import STOCK_NAME_MAP, AnalysisResult
from src.time_utils import utc8_today, as_utc8

logger = logging.getLogger(__name__)


class AnalyzeCommand(BotCommand):
    """
    股票分析命令
    
    分析指定股票代码，生成 AI 分析报告并推送。
    
    用法：
        /analyze 600519         - 分析贵州茅台
        /analyze 贵州茅台        - 按名称分析（自动匹配代码）
        /analyze 腾讯 港股       - 名称+市场消歧
        /analyze AAPL full     - 分析并生成完整报告
    """
    
    @property
    def name(self) -> str:
        return "analyze"
    
    @property
    def aliases(self) -> List[str]:
        return ["a", "分析", "查"]
    
    @property
    def description(self) -> str:
        return "分析指定股票（支持持仓一键分析）"
    
    @property
    def usage(self) -> str:
        return "/analyze <股票代码|股票名称|pos> [CN|HK|US] [full] [-f|--force]"
    
    def validate_args(self, args: List[str]) -> Optional[str]:
        """验证参数"""
        if not args:
            return "请输入股票代码或名称"
        return None
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行分析命令"""
        # 解析参数：<代码|名称> [CN|HK|US] [full]
        market = None
        report_type = "full"
        force_reanalyze = False
        tokens = [a.strip() for a in args if a.strip()]

        force_tokens = {"--force", "-f", "force", "重新", "重跑", "reanalyze"}
        stripped_tokens = []
        for t in tokens:
            if t.lower() in force_tokens or t in force_tokens:
                force_reanalyze = True
                continue
            stripped_tokens.append(t)
        tokens = stripped_tokens

        if tokens and tokens[-1].lower() in ["full", "完整", "详细"]:
            report_type = "full"
            tokens = tokens[:-1]

        if tokens and tokens[-1].upper() in ["CN", "HK", "US", "A股", "港股", "美股"]:
            market_text = tokens[-1].upper()
            market = {"A股": "CN", "港股": "HK", "美股": "US"}.get(market_text, market_text)
            tokens = tokens[:-1]

        query = " ".join(tokens).strip()
        if not query:
            return BotResponse.error_response("请输入股票代码或名称")

        if self._is_positions_query(query):
            return self._analyze_positions(message, market=market, report_type=report_type, force_reanalyze=force_reanalyze)

        code, resolved_name, err = self._resolve_stock_code(query, market)
        if err:
            return BotResponse.error_response(err)

        if not force_reanalyze:
            running = self._find_running_task(code)
            if running:
                task_id = running.get("task_id", "")
                return BotResponse.markdown_response(
                    "⏳ **该股票分析任务正在执行中**\n\n"
                    f"• 股票: {resolved_name} (`{code}`)\n"
                    f"• 任务 ID: `{task_id}`\n"
                    "• 请稍后查看结果，无需重复提交。"
                )

            cached = self._load_today_analysis_report(code)
            if cached:
                return BotResponse.markdown_response(
                    "♻️ **检测到今日已有分析结果，已直接返回历史报告**\n\n"
                    f"• 股票: {resolved_name} (`{code}`)\n"
                    "• 若需重新生成，请使用：`/reanalyze <股票代码|名称>` 或 `/a <股票> -f`\n\n"
                    f"{cached}"
                )

        logger.info(f"[AnalyzeCommand] 分析股票: {code}({resolved_name}), 报告类型: {report_type}")
        
        try:
            # 调用分析服务
            from web.services import get_analysis_service
            from src.enums import ReportType
            from src.config import get_config
            
            service = get_analysis_service()
            config = get_config()

            stream_context = None
            if (
                message.platform == "feishu"
                and getattr(config, "feishu_stream_enabled", False)
                and getattr(config, "feishu_stream_card_enabled", True)
                and message.chat_id
            ):
                stream_context = {
                    "platform": "feishu",
                    "chat_id": message.chat_id,
                    "user_id": message.user_id,
                    "origin_message_id": message.message_id,
                }
            
            # 提交异步分析任务
            result = service.submit_analysis(
                code=code,
                report_type=ReportType.from_str(report_type),
                source_message=message,
                stream_context=stream_context,
                force_reanalyze=force_reanalyze,
            )
            
            if result.get("success"):
                task_id = result.get("task_id", "")
                if result.get("dedup") and result.get("dedup_type") == "running":
                    return BotResponse.markdown_response(
                        f"⏳ **分析任务已在执行中（已复用）**\n\n"
                        f"• 股票: {resolved_name} (`{code}`)\n"
                        f"• 任务 ID: `{task_id}`\n\n"
                        f"请稍后等待结果推送，无需重复提交。"
                    )
                if result.get("dedup") and result.get("dedup_type") == "history":
                    cached = self._load_today_analysis_report(code)
                    if cached:
                        return BotResponse.markdown_response(
                            "♻️ **检测到今日已有分析结果，已直接返回历史报告**\n\n"
                            f"• 股票: {resolved_name} (`{code}`)\n"
                            "• 若需重新生成，请使用：`/reanalyze <股票代码|名称>` 或 `/a <股票> -f`\n\n"
                            f"{cached}"
                        )
                    query_id = str(result.get("history_query_id", "") or "").strip()
                    if query_id:
                        exact = self._load_report_by_query_id(query_id, code)
                        if exact:
                            return BotResponse.markdown_response(
                                "♻️ **检测到今日已有分析结果，已直接返回历史报告**\n\n"
                                f"• 股票: {resolved_name} (`{code}`)\n"
                                f"• 历史任务: `{query_id}`\n"
                                "• 若需重新生成，请使用：`/reanalyze <股票代码|名称>` 或 `/a <股票> -f`\n\n"
                                f"{exact}"
                            )
                    return BotResponse.markdown_response(
                        "♻️ **检测到今日已有分析结果，已复用历史报告**\n\n"
                        f"• 股票: {resolved_name} (`{code}`)\n"
                        "• 若需重新生成，请使用：`/reanalyze <股票代码|名称>` 或 `/a <股票> -f`"
                    )
                return BotResponse.markdown_response(
                    f"✅ **分析任务已提交**\n\n"
                    f"• 股票: {resolved_name} (`{code}`)\n"
                    f"• 报告类型: {ReportType.from_str(report_type).display_name}\n"
                    f"• 任务 ID: `{task_id[:20]}...`\n\n"
                    f"分析完成后将自动推送结果。"
                )
            else:
                error = result.get("error", "未知错误")
                return BotResponse.error_response(f"提交分析任务失败: {error}")
                
        except Exception as e:
            logger.error(f"[AnalyzeCommand] 执行失败: {e}")
            return BotResponse.error_response(f"分析失败: {str(e)[:100]}")

    @staticmethod
    def _is_positions_query(query: str) -> bool:
        q = (query or "").strip().lower()
        return q in {"pos", "position", "positions", "portfolio", "持仓", "仓位", "我的持仓"}

    @staticmethod
    def _build_owner_key(message: BotMessage) -> str:
        platform = (message.platform or "").strip().lower()
        user_id = (message.user_id or "").strip()
        if not platform or not user_id:
            return ""
        return f"{platform}:{user_id}".lower()

    def _analyze_positions(
        self,
        message: BotMessage,
        market: Optional[str],
        report_type: str,
        force_reanalyze: bool,
    ) -> BotResponse:
        try:
            from bot.commands.batch import BatchCommand
        except Exception as e:
            logger.error(f"[AnalyzeCommand] 加载 BatchCommand 失败: {e}")
            return BotResponse.error_response("持仓批量分析功能初始化失败")

        # 复用 /batch holdings 流程：单条摘要 + 飞书卡片按钮查看单股详情
        batch_args: List[str] = ["holdings"]
        if market:
            batch_args.append(market)
        response = BatchCommand().execute(message, batch_args)
        if force_reanalyze:
            response.text = response.text + (
                "\n\n已启用强制重跑模式：本次不复用当日历史。"
            )
        return response

    @staticmethod
    def _is_valid_code(code: str) -> bool:
        """校验代码格式：A股/港股/美股"""
        code = (code or "").upper()
        is_a_stock = re.match(r'^\d{6}$', code)
        is_hk_stock = re.match(r'^HK\d{5}$', code)
        is_us_stock = re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', code)
        return bool(is_a_stock or is_hk_stock or is_us_stock)

    def _resolve_stock_code(self, query: str, market: Optional[str]) -> tuple[str, str, Optional[str]]:
        """
        将“股票名称或代码”解析为标准代码。
        返回: (code, name, error)
        """
        q = (query or "").strip()
        if not q:
            return "", "", "请输入股票代码或名称"

        q_upper = q.upper()
        if self._is_valid_code(q_upper):
            name = (
                STOCK_NAME_MAP.get(q_upper)
                or STOCK_NAME_MAP.get(q_upper.replace("HK", ""))
                or q_upper
            )
            return q_upper, name, None

        # 先用内置映射做精确名称匹配
        for code, name in STOCK_NAME_MAP.items():
            if (name or "").strip().lower() == q.lower():
                normalized_code = f"HK{code}" if re.match(r'^\d{5}$', code) else code
                if market and market == "CN" and normalized_code.startswith("HK"):
                    continue
                if market and market == "HK" and not normalized_code.startswith("HK"):
                    continue
                if market and market == "US" and (normalized_code.isdigit() or normalized_code.startswith("HK")):
                    continue
                return normalized_code, name, None

        # 再走搜索服务模糊匹配
        try:
            from web.services import get_stock_search_service
            search_service = get_stock_search_service()
            resp = search_service.search(keyword=q, market=market, limit=1)
            data = resp.get("data", []) if isinstance(resp, dict) else []
            if data:
                hit = data[0]
                code = (hit.get("code") or "").upper()
                name = (hit.get("name") or code).strip()
                if code:
                    return code, name, None
        except Exception as e:
            logger.debug(f"[AnalyzeCommand] 名称解析搜索失败: {e}")

        return "", "", f"未找到匹配股票：{query}，请尝试输入更完整名称或直接输入代码"

    @staticmethod
    def _load_today_analysis_report(code: str) -> Optional[str]:
        """
        读取今日已生成的分析报告（若存在）。
        """
        try:
            from src.storage import get_db
            from src.notification import NotificationService

            db = get_db()
            candidates = AnalyzeCommand._code_candidates(code)
            rows = []
            for c in candidates:
                rows.extend(db.get_analysis_history(code=c, days=2, limit=20))
            if not rows:
                return None

            # 去重并按时间倒序
            uniq = {}
            for r in rows:
                rid = getattr(r, "id", None) or id(r)
                uniq[rid] = r
            history = sorted(
                list(uniq.values()),
                key=lambda x: getattr(x, "created_at", None) or 0,
                reverse=True,
            )

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

            result = AnalysisResult(
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
            notifier = NotificationService()
            return notifier.generate_dashboard_report([result])
        except Exception as e:
            logger.warning(f"[AnalyzeCommand] 读取今日历史分析失败 code={code}: {e}")
            return None

    @staticmethod
    def _code_candidates(code: str) -> List[str]:
        c = (code or "").strip().upper()
        if c.isdigit() and len(c) < 6:
            c = c.zfill(6)
        out = [c]
        if c.startswith("HK") and len(c) == 7 and c[2:].isdigit():
            out.append(c[2:])
        if c.isdigit():
            out.append(c.lstrip("0") or "0")
            if len(c) == 5:
                out.append(f"HK{c}")
            if len(c) <= 6:
                out.append(c.zfill(6))
        return list(dict.fromkeys([x for x in out if x]))

    @staticmethod
    def _load_report_by_query_id(query_id: str, fallback_code: str) -> Optional[str]:
        """
        按 query_id 精确加载历史报告正文。
        """
        try:
            from src.storage import get_db
            from src.notification import NotificationService

            db = get_db()
            rows = db.get_analysis_history(query_id=query_id, days=365, limit=1)
            if not rows:
                return None
            row = rows[0]
            raw = {}
            try:
                raw = json.loads(getattr(row, "raw_result", "") or "{}")
            except Exception:
                raw = {}
            result = AnalysisResult(
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
            return NotificationService().generate_dashboard_report([result])
        except Exception:
            return None

    @staticmethod
    def _find_running_task(code: str) -> Optional[dict]:
        """检查同一股票是否已有运行中的分析任务。"""
        try:
            from web.services import get_analysis_service

            candidates = set(AnalyzeCommand._code_candidates(code))
            svc = get_analysis_service()
            tasks = svc.list_tasks(limit=200)
            for t in tasks:
                status = str(t.get("status", "")).lower()
                task_code = str(t.get("code", "")).strip().upper()
                if status in {"running", "queued"} and task_code in candidates:
                    return t
            return None
        except Exception:
            return None
