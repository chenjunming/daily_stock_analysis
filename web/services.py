# -*- coding: utf-8 -*-
"""
===================================
Web 服务层 - 业务逻辑
===================================

职责：
1. 配置管理服务 (ConfigService)
2. 分析任务服务 (AnalysisService)
"""

from __future__ import annotations

import os
import re
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, Dict, Any, List, Union

import pandas as pd

from src.enums import ReportType
from src.storage import get_db
from src.time_utils import utc8_now, utc8_today, as_utc8
from bot.models import BotMessage
from data_provider import DataFetcherManager

logger = logging.getLogger(__name__)

# ============================================================
# 配置管理服务
# ============================================================

_ENV_PATH = os.getenv("ENV_FILE", ".env")

_STOCK_LIST_RE = re.compile(
    r"^(?P<prefix>\s*STOCK_LIST\s*=\s*)(?P<value>.*?)(?P<suffix>\s*)$"
)


class ConfigService:
    """
    配置管理服务
    
    负责 .env 文件中 STOCK_LIST 的读写操作
    """
    
    def __init__(self, env_path: Optional[str] = None):
        self.env_path = env_path or _ENV_PATH
    
    def read_env_text(self) -> str:
        """读取 .env 文件内容"""
        try:
            with open(self.env_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""
    
    def write_env_text(self, text: str) -> None:
        """写入 .env 文件内容"""
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write(text)
    
    def get_stock_list(self) -> str:
        """获取当前自选股列表字符串"""
        env_text = self.read_env_text()
        return self._extract_stock_list(env_text)
    
    def set_stock_list(self, stock_list: str) -> str:
        """
        设置自选股列表
        
        Args:
            stock_list: 股票代码字符串（逗号或换行分隔）
            
        Returns:
            规范化后的股票列表字符串
        """
        env_text = self.read_env_text()
        normalized = self._normalize_stock_list(stock_list)
        updated = self._update_stock_list(env_text, normalized)
        self.write_env_text(updated)
        return normalized
    
    def get_env_filename(self) -> str:
        """获取 .env 文件名"""
        return os.path.basename(self.env_path)
    
    def _extract_stock_list(self, env_text: str) -> str:
        """从环境文件中提取 STOCK_LIST 值"""
        for line in env_text.splitlines():
            m = _STOCK_LIST_RE.match(line)
            if m:
                raw = m.group("value").strip()
                # 去除引号
                if (raw.startswith('"') and raw.endswith('"')) or \
                   (raw.startswith("'") and raw.endswith("'")):
                    raw = raw[1:-1]
                return raw
        return ""
    
    def _normalize_stock_list(self, value: str) -> str:
        """规范化股票列表格式"""
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        parts = [p for p in parts if p]
        return ",".join(parts)
    
    def _update_stock_list(self, env_text: str, new_value: str) -> str:
        """更新环境文件中的 STOCK_LIST"""
        lines = env_text.splitlines(keepends=False)
        out_lines: List[str] = []
        replaced = False
        
        for line in lines:
            m = _STOCK_LIST_RE.match(line)
            if not m:
                out_lines.append(line)
                continue
            
            out_lines.append(f"{m.group('prefix')}{new_value}{m.group('suffix')}")
            replaced = True
        
        if not replaced:
            if out_lines and out_lines[-1].strip() != "":
                out_lines.append("")
            out_lines.append(f"STOCK_LIST={new_value}")
        
        trailing_newline = env_text.endswith("\n") if env_text else True
        out = "\n".join(out_lines)
        return out + ("\n" if trailing_newline else "")


# ============================================================
# 分析任务服务
# ============================================================

class AnalysisService:
    """
    分析任务服务
    
    负责：
    1. 管理异步分析任务
    2. 执行股票分析
    3. 触发通知推送
    """
    
    _instance: Optional['AnalysisService'] = None
    _lock = threading.Lock()
    
    def __init__(self, max_workers: int = 3):
        self._executor: Optional[ThreadPoolExecutor] = None
        self._max_workers = max_workers
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._tasks_lock = threading.Lock()
    
    @classmethod
    def get_instance(cls) -> 'AnalysisService':
        """获取单例实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    @property
    def executor(self) -> ThreadPoolExecutor:
        """获取或创建线程池"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="analysis_"
            )
        return self._executor
    
    def submit_analysis(
        self, 
        code: str, 
        report_type: Union[ReportType, str] = ReportType.SIMPLE,
        source_message: Optional[BotMessage] = None,
        save_context_snapshot: Optional[bool] = None,
        stream_context: Optional[Dict[str, Any]] = None,
        force_reanalyze: bool = False,
    ) -> Dict[str, Any]:
        """
        提交异步分析任务
        
        Args:
            code: 股票代码
            report_type: 报告类型枚举
            
        Returns:
            任务信息字典
        """
        # 确保 report_type 是枚举类型
        if isinstance(report_type, str):
            report_type = ReportType.from_str(report_type)

        normalized_code = self._normalize_code(code)

        # 服务层防重：同股票已有运行任务时，不重复提交
        if not force_reanalyze:
            existing = self._find_running_task(normalized_code)
            if existing:
                task_id = existing.get("task_id", "")
                logger.info(f"[AnalysisService] 命中运行中任务，跳过重复提交: code={normalized_code}, task_id={task_id}")
                return {
                    "success": True,
                    "message": "已有运行中的任务，已复用",
                    "code": normalized_code,
                    "task_id": task_id,
                    "report_type": report_type.value,
                    "dedup": True,
                    "dedup_type": "running",
                }
            today_hit = self._find_today_analysis_history(normalized_code)
            if today_hit:
                logger.info(f"[AnalysisService] 命中当日历史报告，跳过重复提交: code={normalized_code}")
                return {
                    "success": True,
                    "message": "当日已有分析报告，已复用",
                    "code": normalized_code,
                    "task_id": "",
                    "report_type": report_type.value,
                    "dedup": True,
                    "dedup_type": "history",
                    "history_query_id": today_hit.get("query_id", ""),
                }
        
        task_id = f"{normalized_code}_{utc8_now().strftime('%Y%m%d_%H%M%S_%f')}"
        
        # 提交到线程池
        self.executor.submit(
            self._run_analysis,
            normalized_code,
            task_id,
            report_type,
            source_message,
            save_context_snapshot,
            stream_context
        )
        
        logger.info(f"[AnalysisService] 已提交股票 {normalized_code} 的分析任务, task_id={task_id}, report_type={report_type.value}")
        
        return {
            "success": True,
            "message": "分析任务已提交，将异步执行并推送通知",
            "code": normalized_code,
            "task_id": task_id,
            "report_type": report_type.value
        }

    @staticmethod
    def _normalize_code(code: str) -> str:
        c = (code or "").strip().upper()
        if c.isdigit() and len(c) < 6:
            return c.zfill(6)
        return c

    def _code_candidates(self, code: str) -> List[str]:
        c = self._normalize_code(code)
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

    def _find_running_task(self, code: str) -> Optional[Dict[str, Any]]:
        candidates = set(self._code_candidates(code))
        with self._tasks_lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            status = str(task.get("status", "")).lower()
            if status not in {"running", "queued"}:
                continue
            task_code = self._normalize_code(str(task.get("code", "")).strip().upper())
            task_candidates = set(self._code_candidates(task_code))
            if candidates.intersection(task_candidates):
                return task
        return None

    def _find_today_analysis_history(self, code: str) -> Optional[Dict[str, Any]]:
        """
        查找 UTC+8 当天该股票的最新分析历史。
        """
        try:
            db = get_db()
            today = utc8_today()
            latest = None
            latest_ts = None
            for c in self._code_candidates(code):
                rows = db.get_analysis_history(code=c, days=2, limit=20)
                for row in rows:
                    created_at = getattr(row, "created_at", None)
                    created_at_utc8 = as_utc8(created_at)
                    if created_at_utc8 and created_at_utc8.date() == today:
                        if latest_ts is None or created_at_utc8 > latest_ts:
                            latest_ts = created_at_utc8
                            latest = row
            if latest is None:
                return None
            return {
                "query_id": getattr(latest, "query_id", "") or "",
                "code": getattr(latest, "code", "") or code,
            }
        except Exception:
            return None
    
    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务状态"""
        with self._tasks_lock:
            return self._tasks.get(task_id)
    
    def list_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """列出最近的任务"""
        with self._tasks_lock:
            tasks = list(self._tasks.values())
        # 按开始时间倒序
        tasks.sort(key=lambda x: x.get('start_time', ''), reverse=True)
        return tasks[:limit]

    def get_analysis_history(
        self,
        code: Optional[str] = None,
        query_id: Optional[str] = None,
        days: int = 30,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        获取分析历史记录
        """
        db = get_db()
        records = db.get_analysis_history(code=code, query_id=query_id, days=days, limit=limit)
        return [r.to_dict() for r in records]
    
    def _run_analysis(
        self, 
        code: str, 
        task_id: str, 
        report_type: ReportType = ReportType.SIMPLE,
        source_message: Optional[BotMessage] = None,
        save_context_snapshot: Optional[bool] = None,
        stream_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        执行单只股票分析
        
        内部方法，在线程池中运行
        
        Args:
            code: 股票代码
            task_id: 任务ID
            report_type: 报告类型枚举
        """
        # 初始化任务状态
        with self._tasks_lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "code": code,
                "status": "running",
                "start_time": utc8_now().isoformat(),
                "result": None,
                "error": None,
                "report_type": report_type.value
            }
        
        try:
            # 延迟导入避免循环依赖
            from src.config import get_config
            from main import StockAnalysisPipeline
            from bot.platforms.feishu_stream import (
                FEISHU_SDK_AVAILABLE,
                FeishuReplyClient,
                FeishuCardStreamSession,
            )
            
            logger.info(f"[AnalysisService] 开始分析股票: {code}")

            config = get_config()
            stream_session = None
            if (
                stream_context
                and stream_context.get("platform") == "feishu"
                and getattr(config, "feishu_stream_card_enabled", True)
                and FEISHU_SDK_AVAILABLE
            ):
                try:
                    app_id = getattr(config, "feishu_app_id", None)
                    app_secret = getattr(config, "feishu_app_secret", None)
                    chat_id = stream_context.get("chat_id")
                    if app_id and app_secret and chat_id:
                        reply_client = FeishuReplyClient(app_id, app_secret)
                        stream_session = FeishuCardStreamSession(
                            reply_client=reply_client,
                            chat_id=chat_id,
                            update_interval=float(getattr(config, "feishu_stream_card_update_interval", 2.0)),
                            max_updates=int(getattr(config, "feishu_stream_card_max_updates", 120)),
                            at_user=False,
                        )
                        init_content = self._render_analyze_progress_card(
                            code=code,
                            report_type=report_type.value,
                            stage="queued",
                            detail="任务已进入队列",
                            percent=2,
                            task_id=task_id,
                            started_at=utc8_now().isoformat(),
                            version=1,
                        )
                        started = stream_session.start(init_content)
                        if started:
                            stream_context["stream_message_id"] = stream_session.message_id
                except Exception as e:
                    logger.warning(f"[AnalysisService] 初始化飞书流式卡片失败，回退普通模式: {e}")
                    stream_session = None
            
            started_at = utc8_now().isoformat()

            def progress_reporter(event: Dict[str, Any]) -> None:
                if not stream_session:
                    return
                try:
                    content = self._render_analyze_progress_card(
                        code=code,
                        report_type=report_type.value,
                        stage=str(event.get("stage", "running")),
                        detail=str(event.get("detail", "处理中")),
                        percent=int(event.get("percent", 0)),
                        task_id=task_id,
                        started_at=started_at,
                        version=stream_session.version + 1,
                    )
                    stream_session.update(content, force=False)
                except Exception as e:
                    logger.debug(f"[AnalysisService] 流式进度更新失败: {e}")

            # 创建分析管道
            pipeline = StockAnalysisPipeline(
                config=config,
                max_workers=1,
                source_message=source_message,
                query_id=task_id,
                query_source="web",
                save_context_snapshot=save_context_snapshot,
                progress_reporter=progress_reporter if stream_session else None
            )
            
            # 执行单只股票分析（启用单股推送）
            result = pipeline.process_single_stock(
                code=code,
                skip_analysis=False,
                single_stock_notify=True,
                report_type=report_type
            )
            
            if result:
                result_data = {
                    "code": result.code,
                    "name": result.name,
                    "sentiment_score": result.sentiment_score,
                    "operation_advice": result.operation_advice,
                    "trend_prediction": result.trend_prediction,
                    "analysis_summary": result.analysis_summary,
                }
                
                with self._tasks_lock:
                    self._tasks[task_id].update({
                        "status": "completed",
                        "end_time": utc8_now().isoformat(),
                        "result": result_data
                    })

                if stream_session:
                    final_content = self._render_analyze_final_card(
                        code=code,
                        result=result_data,
                        task_id=task_id,
                        report_type=report_type.value,
                        started_at=started_at,
                        ok=True,
                        version=stream_session.version + 1,
                    )
                    stream_session.finish(final_content)
                
                logger.info(f"[AnalysisService] 股票 {code} 分析完成: {result.operation_advice}")
                return {"success": True, "task_id": task_id, "result": result_data}
            else:
                with self._tasks_lock:
                    self._tasks[task_id].update({
                        "status": "failed",
                        "end_time": utc8_now().isoformat(),
                        "error": "分析返回空结果"
                    })

                if stream_session:
                    final_content = self._render_analyze_final_card(
                        code=code,
                        result={},
                        task_id=task_id,
                        report_type=report_type.value,
                        started_at=started_at,
                        ok=False,
                        error="分析返回空结果",
                        version=stream_session.version + 1,
                    )
                    stream_session.finish(final_content)
                
                logger.warning(f"[AnalysisService] 股票 {code} 分析失败: 返回空结果")
                return {"success": False, "task_id": task_id, "error": "分析返回空结果"}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[AnalysisService] 股票 {code} 分析异常: {error_msg}")
            
            with self._tasks_lock:
                self._tasks[task_id].update({
                    "status": "failed",
                    "end_time": utc8_now().isoformat(),
                    "error": error_msg
                })

            try:
                stream_session = locals().get("stream_session")
                started_at = locals().get("started_at", utc8_now().isoformat())
                if stream_session:
                    final_content = self._render_analyze_final_card(
                        code=code,
                        result={},
                        task_id=task_id,
                        report_type=report_type.value,
                        started_at=started_at,
                        ok=False,
                        error=error_msg,
                        version=stream_session.version + 1,
                    )
                    stream_session.finish(final_content)
            except Exception:
                pass
            
            return {"success": False, "task_id": task_id, "error": error_msg}

    @staticmethod
    def _render_analyze_progress_card(
        code: str,
        report_type: str,
        stage: str,
        detail: str,
        percent: int,
        task_id: str,
        started_at: str,
        version: int,
    ) -> str:
        now = utc8_now()
        try:
            start_dt = datetime.fromisoformat(started_at)
            elapsed = int((now - start_dt).total_seconds())
        except Exception:
            elapsed = 0
        stage_map = {
            "queued": "排队中",
            "stock_start": "开始处理",
            "fetch_start": "获取行情",
            "fetch_done": "行情就绪",
            "intel_search_start": "搜索情报",
            "intel_search_done": "情报完成",
            "llm_start": "AI 分析中",
            "llm_done": "AI 分析完成",
            "history_save": "写入历史",
            "analyze_done": "分析完成",
        }
        stage_text = stage_map.get(stage, stage)
        p = max(0, min(100, int(percent)))
        return (
            f"## 📈 分析进行中\n\n"
            f"- 股票: `{code}`\n"
            f"- 报告类型: `{report_type}`\n"
            f"- 任务ID: `{task_id[:24]}...`\n"
            f"- 进度: **{p}%**\n"
            f"- 当前阶段: **{stage_text}**\n"
            f"- 详情: {detail}\n"
            f"- 已耗时: {elapsed}s\n\n"
            f"`v{version}` | 更新时间: {now.strftime('%H:%M:%S')} (UTC+8)"
        )

    @staticmethod
    def _render_analyze_final_card(
        code: str,
        result: Dict[str, Any],
        task_id: str,
        report_type: str,
        started_at: str,
        ok: bool,
        error: Optional[str] = None,
        version: int = 1,
    ) -> str:
        now = utc8_now()
        try:
            start_dt = datetime.fromisoformat(started_at)
            elapsed = int((now - start_dt).total_seconds())
        except Exception:
            elapsed = 0
        if ok:
            name = result.get("name") or code
            advice = result.get("operation_advice") or "-"
            trend = result.get("trend_prediction") or "-"
            score = result.get("sentiment_score")
            summary = (result.get("analysis_summary") or "分析完成").strip()
            if len(summary) > 120:
                summary = summary[:120] + "..."
            return (
                f"## ✅ 分析完成\n\n"
                f"- 股票: **{name}** (`{code}`)\n"
                f"- 建议: **{advice}**\n"
                f"- 趋势: {trend}\n"
                f"- 评分: {score if score is not None else '-'}\n"
                f"- 报告类型: `{report_type}`\n"
                f"- 任务ID: `{task_id}`\n"
                f"- 耗时: {elapsed}s\n\n"
                f"摘要: {summary}\n\n"
                f"`v{version}` | 完成时间: {now.strftime('%H:%M:%S')} (UTC+8)"
            )
        return (
            f"## ❌ 分析失败\n\n"
            f"- 股票: `{code}`\n"
            f"- 报告类型: `{report_type}`\n"
            f"- 任务ID: `{task_id}`\n"
            f"- 耗时: {elapsed}s\n"
            f"- 错误: {(error or '未知错误')[:160]}\n\n"
            f"`v{version}` | 更新时间: {now.strftime('%H:%M:%S')} (UTC+8)"
        )


# ============================================================
# 便捷函数
# ============================================================

def get_config_service() -> ConfigService:
    """获取配置服务实例"""
    return ConfigService()


def get_analysis_service() -> AnalysisService:
    """获取分析服务单例"""
    return AnalysisService.get_instance()

# ============================================================
# 自选股服务
# ============================================================

class WatchlistService:
    """
    自选股管理服务
    
    负责：
    1. 管理自选股 CRUD 操作
    2. 按市场分组
    3. 获取分析历史
    """
    
    def __init__(self):
        self.db = get_db()
    
    def add_stock(self, code: str, name: Optional[str] = None, market: str = "CN") -> Dict[str, Any]:
        """添加自选股"""
        try:
            result = self.db.add_watchlist(code, name, market)
            if result:
                return {
                    "success": True,
                    "code": result.get("code"),
                    "name": result.get("name"),
                    "market": result.get("market")
                }
            else:
                code_upper = (code or "").strip().upper()
                exists = any(
                    (getattr(item, "code", "") or "").upper() == code_upper
                    for item in self.db.get_watchlist()
                )
                if exists:
                    return {"success": False, "error": "股票已在自选股中"}
                return {"success": False, "error": "未找到该股票的有效名称，请检查代码/市场是否正确"}
        except Exception as e:
            logger.error(f"添加自选股失败: {e}")
            return {"success": False, "error": str(e)}
    
    def remove_stock(self, code: str) -> Dict[str, Any]:
        """删除自选股"""
        try:
            result = self.db.remove_watchlist(code)
            return {"success": result}
        except Exception as e:
            logger.error(f"删除自选股失败: {e}")
            return {"success": False, "error": str(e)}
    
    def get_watchlist(self, market: Optional[str] = None) -> Dict[str, Any]:
        """获取自选股列表"""
        try:
            watchlist = self.db.get_watchlist(market) if market else self.db.get_watchlist()
            return {
                "success": True,
                "data": [item.to_dict() for item in watchlist]
            }
        except Exception as e:
            logger.error(f"获取自选股列表失败: {e}")
            return {"success": False, "error": str(e)}
    
    def get_watchlist_grouped(self) -> Dict[str, Any]:
        """按市场分组获取自选股"""
        try:
            grouped = self.db.get_watchlist_grouped()
            return {
                "success": True,
                "data": {
                    market: [item if isinstance(item, dict) else item.to_dict() for item in items]
                    for market, items in grouped.items()
                }
            }
        except Exception as e:
            logger.error(f"获取分组自选股失败: {e}")
            return {"success": False, "error": str(e)}
    
    def get_stock_analysis(self, code: str, limit: int = 10) -> Dict[str, Any]:
        """获取股票的历史分析报告"""
        try:
            history = self.db.get_analysis_history(code=code, limit=limit)
            return {
                "success": True,
                "code": code,
                "data": [item.to_dict() for item in history]
            }
        except Exception as e:
            logger.error(f"获取分析历史失败: {e}")
            return {"success": False, "error": str(e)}


def get_watchlist_service() -> WatchlistService:
    """获取自选股服务实例"""
    return WatchlistService()


class StockSearchService:
    """
    股票模糊搜索服务

    特性：
    1. 支持 A 股 / 港股 / 美股
    2. 代码前缀匹配 + 名称包含匹配
    3. 使用内存缓存减少重复外部请求
    """

    _instance: Optional['StockSearchService'] = None
    _lock = threading.Lock()

    def __init__(self):
        self.db = get_db()
        self._catalog_lock = threading.Lock()
        self._catalog: Dict[str, List[Dict[str, str]]] = {
            "CN": [],
            "HK": [],
            "US": [],
        }
        self._market_loaded: Dict[str, bool] = {
            "CN": False,
            "HK": False,
            "US": False,
        }
        self._db_fallback_loaded = False

    @classmethod
    def get_instance(cls) -> 'StockSearchService':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def search(self, keyword: str, market: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
        kw = (keyword or "").strip().upper()
        if not kw:
            return {"success": True, "data": []}

        limit = max(1, min(limit, 50))
        markets = [market.upper()] if market and market.upper() in {"CN", "HK", "US"} else ["CN", "HK", "US"]

        # 先加载本地兜底（watchlist + 历史），保证快速可用
        self._ensure_db_fallback_loaded()

        candidates: List[Dict[str, str]] = []
        for m in markets:
            candidates.extend(self._catalog.get(m, []))

        ranked = self._rank_and_filter(candidates, kw)
        if len(ranked) >= min(5, limit):
            return {"success": True, "data": ranked[:limit]}

        # 若本地结果不足，再按需加载目标市场完整候选池
        for m in markets:
            self._ensure_market_loaded(m)
            candidates.extend(self._catalog.get(m, []))

        # 去重后重排
        dedup: Dict[tuple, Dict[str, str]] = {}
        for item in candidates:
            key = (item.get("market"), item.get("code"))
            dedup[key] = item

        ranked = self._rank_and_filter(list(dedup.values()), kw)
        if len(ranked) < min(5, limit):
            online_hits = self._search_online_multi_source(keyword=kw, market=market)
            if online_hits:
                with self._catalog_lock:
                    for hit in online_hits:
                        mkt = (hit.get("market") or "").upper()
                        code = (hit.get("code") or "").upper()
                        name = (hit.get("name") or code).strip() or code
                        if mkt not in {"CN", "HK", "US"} or not code:
                            continue
                        exists = any(
                            (x.get("code") or "").upper() == code
                            for x in self._catalog.setdefault(mkt, [])
                        )
                        if not exists:
                            self._catalog[mkt].append({"code": code, "name": name, "market": mkt})
                            dedup[(mkt, code)] = {"code": code, "name": name, "market": mkt}
                ranked = self._rank_and_filter(list(dedup.values()), kw)
        return {"success": True, "data": ranked[:limit]}

    def _search_online_multi_source(self, keyword: str, market: Optional[str]) -> List[Dict[str, str]]:
        """
        在线多源兜底搜索（仅在本地候选不足时触发）。

        执行顺序：
        1) 先走 DataFetcherManager 统一实时行情链路（遵循实时优先级配置）
        2) 若仍未命中，再按 fetcher.priority 逐个尝试 akshare/tushare/yfinance
        """
        kw = (keyword or "").strip().upper()
        if not kw:
            return []

        target_market = (market or "").strip().upper()
        if target_market not in {"CN", "HK", "US"}:
            target_market = self._infer_market(kw)

        # 非代码关键词（如“苹果”）不走在线精确兜底，避免高成本低命中
        is_code_like = bool(
            re.match(r"^\d{6}$", kw)
            or re.match(r"^\d{5}$", kw)
            or re.match(r"^HK\d{5}$", kw)
            or re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", kw)
        )
        if not is_code_like:
            return []

        candidates: List[str] = [kw]
        if target_market == "HK":
            if re.match(r"^\d{5}$", kw):
                candidates = [f"HK{kw}", kw]
            elif re.match(r"^HK\d{5}$", kw):
                candidates = [kw, kw[2:]]
        elif target_market == "CN" and re.match(r"^\d{6}$", kw):
            candidates = [kw]
        elif target_market == "US":
            candidates = [kw]

        try:
            manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"[StockSearch] 初始化 DataFetcherManager 失败: {e}")
            return []

        hits: List[Dict[str, str]] = []

        def _append_hit(quote_obj: Any, fallback_code: str) -> bool:
            quote_name = str(getattr(quote_obj, "name", "") or "").strip()
            quote_code = str(getattr(quote_obj, "code", "") or fallback_code).strip().upper()
            if not quote_code:
                return False
            if not quote_name or quote_name.upper() == quote_code:
                return False
            hits.append({
                "code": quote_code,
                "name": quote_name,
                "market": target_market,
            })
            return True

        # 层 1：统一实时行情链路
        for code in candidates[:2]:
            try:
                quote = manager.get_realtime_quote(code)
                if quote and _append_hit(quote, code):
                    break
            except Exception as e:
                logger.debug(f"[StockSearch] 在线兜底失败 code={code}, market={target_market}: {e}")

        if hits:
            return hits

        # 层 2：按优先级逐个调用 fetcher（akshare/tushare/yfinance）
        try:
            fetchers = sorted(getattr(manager, "_fetchers", []), key=lambda f: getattr(f, "priority", 99))
        except Exception:
            fetchers = []

        for code in candidates[:2]:
            for fetcher in fetchers:
                fname = str(getattr(fetcher, "name", "") or "")
                if fname not in {"AkshareFetcher", "TushareFetcher", "YfinanceFetcher"}:
                    continue
                if target_market == "US" and fname not in {"YfinanceFetcher", "AkshareFetcher"}:
                    continue
                if target_market in {"CN", "HK"} and fname == "YfinanceFetcher":
                    continue

                try:
                    # 1) 优先使用 stock_name 接口（轻量）
                    if hasattr(fetcher, "get_stock_name"):
                        name = str(fetcher.get_stock_name(code) or "").strip()
                        if name and name.upper() != code.upper():
                            hits.append({"code": code.upper(), "name": name, "market": target_market})
                            return hits
                except Exception:
                    pass

                try:
                    # 2) 退化为实时行情接口
                    quote = None
                    if fname == "AkshareFetcher" and hasattr(fetcher, "get_realtime_quote"):
                        quote = fetcher.get_realtime_quote(code, source="em")
                    elif hasattr(fetcher, "get_realtime_quote"):
                        quote = fetcher.get_realtime_quote(code)
                    if quote and _append_hit(quote, code):
                        return hits
                except Exception:
                    pass

        return hits

    def _ensure_market_loaded(self, market: str) -> None:
        market = market.upper()
        if market not in {"CN", "HK", "US"}:
            return
        if self._market_loaded.get(market):
            return
        with self._catalog_lock:
            if self._market_loaded.get(market):
                return
            if market == "CN":
                self._load_cn_catalog()
            elif market == "HK":
                self._load_hk_catalog()
            else:
                self._load_us_catalog()
            self._market_loaded[market] = True

    def _ensure_db_fallback_loaded(self) -> None:
        if self._db_fallback_loaded:
            return
        with self._catalog_lock:
            if self._db_fallback_loaded:
                return
            self._load_builtin_map()
            self._load_db_fallback()
            self._db_fallback_loaded = True

    def _load_cn_catalog(self) -> None:
        try:
            merged: Dict[str, Dict[str, str]] = {}

            # 优先使用 akshare 全市场列表（通常最稳定）
            try:
                import akshare as ak
                if hasattr(ak, "stock_zh_a_spot_em"):
                    df = ak.stock_zh_a_spot_em()
                    if df is not None and not df.empty:
                        rows = []
                        for _, row in df.iterrows():
                            code = str(row.get("代码", "")).strip()
                            name = str(row.get("名称", "")).strip()
                            if not code:
                                continue
                            rows.append({
                                "code": code.zfill(6),
                                "name": name or code.zfill(6),
                                "market": "CN",
                            })
                        for item in rows:
                            merged[item["code"]] = item
            except Exception as e:
                logger.debug(f"[StockSearch] akshare CN 列表加载失败: {e}")

            # 复用已实现的 get_stock_list（Tushare/Baostock）
            manager = DataFetcherManager()
            for fetcher in getattr(manager, "_fetchers", []):
                if not hasattr(fetcher, "get_stock_list"):
                    continue
                try:
                    df = fetcher.get_stock_list()
                    if df is None or df.empty:
                        continue
                    normalized = self._normalize_df(df, market="CN")
                    for item in normalized:
                        merged[item["code"]] = item
                    if len(merged) >= 3000:
                        break
                except Exception as e:
                    logger.debug(f"[StockSearch] CN 列表加载失败 ({fetcher.name}): {e}")
            self._catalog["CN"] = list(merged.values())
            logger.info(f"[StockSearch] CN 候选池加载完成: {len(self._catalog['CN'])}")
        except Exception as e:
            logger.warning(f"[StockSearch] CN 候选池加载异常: {e}")

    def _load_hk_catalog(self) -> None:
        try:
            import akshare as ak
            df = ak.stock_hk_spot_em()
            if df is None or df.empty:
                return
            rows = []
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).strip()
                name = str(row.get("名称", "")).strip()
                if not code:
                    continue
                rows.append({
                    "code": f"HK{code.zfill(5)}",
                    "name": name or f"HK{code.zfill(5)}",
                    "market": "HK",
                })
            self._catalog["HK"] = rows
            logger.info(f"[StockSearch] HK 候选池加载完成: {len(self._catalog['HK'])}")
        except Exception as e:
            logger.warning(f"[StockSearch] HK 候选池加载失败: {e}")

    def _load_us_catalog(self) -> None:
        # 可选：akshare 新版本通常提供 stock_us_spot_em，旧版本可能没有
        try:
            import akshare as ak
            if not hasattr(ak, "stock_us_spot_em"):
                return
            df = ak.stock_us_spot_em()
            if df is None or df.empty:
                return
            rows = []
            for _, row in df.iterrows():
                code = str(row.get("代码", "")).strip().upper()
                name = str(row.get("名称", "")).strip()
                if not code:
                    continue
                rows.append({
                    "code": code,
                    "name": name or code,
                    "market": "US",
                })
            self._catalog["US"] = rows
            logger.info(f"[StockSearch] US 候选池加载完成: {len(self._catalog['US'])}")
        except Exception as e:
            logger.warning(f"[StockSearch] US 候选池加载失败: {e}")

    def _load_db_fallback(self) -> None:
        # 将已有 watchlist + 历史分析补入候选池，确保至少能搜到“系统里出现过”的标的
        existing = {(item["market"], item["code"]) for m in self._catalog.values() for item in m}
        try:
            watchlist = self.db.get_watchlist()
            for item in watchlist:
                d = item.to_dict()
                code = (d.get("code") or "").upper()
                market = (d.get("market") or "CN").upper()
                if not code:
                    continue
                key = (market, code)
                if key in existing:
                    continue
                self._catalog.setdefault(market, []).append({
                    "code": code,
                    "name": d.get("name") or code,
                    "market": market,
                })
                existing.add(key)
        except Exception as e:
            logger.debug(f"[StockSearch] watchlist fallback 加载失败: {e}")

        try:
            # 仅取近期历史，避免过大
            history = self.db.get_analysis_history(days=365, limit=2000)
            for rec in history:
                d = rec.to_dict()
                code = (d.get("code") or "").upper()
                name = (d.get("name") or "").strip()
                if not code:
                    continue
                market = self._infer_market(code)
                key = (market, code)
                if key in existing:
                    continue
                self._catalog.setdefault(market, []).append({
                    "code": code,
                    "name": name or code,
                    "market": market,
                })
                existing.add(key)
        except Exception as e:
            logger.debug(f"[StockSearch] history fallback 加载失败: {e}")

    def _load_builtin_map(self) -> None:
        """加载内置常见标的映射，保证离线时也有基本可用结果。"""
        try:
            from src.analyzer import STOCK_NAME_MAP
        except Exception as e:
            logger.debug(f"[StockSearch] 内置映射加载失败: {e}")
            return

        existing = {(item["market"], item["code"]) for m in self._catalog.values() for item in m}
        for raw_code, name in STOCK_NAME_MAP.items():
            code = (raw_code or "").strip().upper()
            if not code:
                continue

            if re.match(r'^\d{6}$', code):
                market = "CN"
                normalized_code = code
            elif re.match(r'^\d{5}$', code):
                market = "HK"
                normalized_code = f"HK{code}"
            elif re.match(r'^HK\d{5}$', code):
                market = "HK"
                normalized_code = code
            else:
                market = "US"
                normalized_code = code

            key = (market, normalized_code)
            if key in existing:
                continue
            self._catalog.setdefault(market, []).append({
                "code": normalized_code,
                "name": name or normalized_code,
                "market": market,
            })
            existing.add(key)

    @staticmethod
    def _normalize_df(df: pd.DataFrame, market: str) -> List[Dict[str, str]]:
        rows: List[Dict[str, str]] = []
        if df is None or df.empty:
            return rows
        code_col = "code" if "code" in df.columns else None
        name_col = "name" if "name" in df.columns else None
        if not code_col:
            return rows
        for _, row in df.iterrows():
            code = str(row.get(code_col, "")).strip().upper()
            name = str(row.get(name_col, "")).strip() if name_col else ""
            if not code:
                continue
            rows.append({
                "code": code,
                "name": name or code,
                "market": market,
            })
        return rows

    @staticmethod
    def _infer_market(code: str) -> str:
        if re.match(r'^\d{6}$', code):
            return "CN"
        if re.match(r'^HK\d{5}$', code):
            return "HK"
        return "US"

    @staticmethod
    def _rank_and_filter(candidates: List[Dict[str, str]], keyword: str) -> List[Dict[str, str]]:
        def score(item: Dict[str, str]) -> int:
            code = (item.get("code") or "").upper()
            name = (item.get("name") or "").upper()
            if code == keyword:
                return 100
            if code.startswith(keyword):
                return 80
            if keyword in code:
                return 60
            if name.startswith(keyword):
                return 50
            if keyword in name:
                return 40
            return 0

        matched = []
        for item in candidates:
            s = score(item)
            if s <= 0:
                continue
            matched.append((s, item))
        matched.sort(key=lambda x: (-x[0], x[1].get("code", "")))
        return [item for _, item in matched]


def get_stock_search_service() -> StockSearchService:
    """获取股票搜索服务单例"""
    return StockSearchService.get_instance()
