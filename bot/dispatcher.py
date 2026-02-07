# -*- coding: utf-8 -*-
"""
===================================
命令分发器
===================================

负责解析命令、匹配处理器、分发执行。
"""

import logging
import json
import re
import time
import threading
from collections import defaultdict
from typing import Dict, List, Optional, Type, Callable, Any, Tuple

from bot.models import BotMessage, BotResponse
from bot.commands.base import BotCommand
from src.config import get_config

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    简单的频率限制器
    
    基于滑动窗口算法，限制每个用户的请求频率。
    """
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        """
        Args:
            max_requests: 窗口内最大请求数
            window_seconds: 窗口时间（秒）
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[str, List[float]] = defaultdict(list)
    
    def is_allowed(self, user_id: str) -> bool:
        """
        检查用户是否允许请求
        
        Args:
            user_id: 用户标识
            
        Returns:
            是否允许
        """
        now = time.time()
        window_start = now - self.window_seconds
        
        # 清理过期记录
        self._requests[user_id] = [
            t for t in self._requests[user_id] 
            if t > window_start
        ]
        
        # 检查是否超限
        if len(self._requests[user_id]) >= self.max_requests:
            return False
        
        # 记录本次请求
        self._requests[user_id].append(now)
        return True
    
    def get_remaining(self, user_id: str) -> int:
        """获取剩余可用请求数"""
        now = time.time()
        window_start = now - self.window_seconds
        
        # 清理过期记录
        self._requests[user_id] = [
            t for t in self._requests[user_id] 
            if t > window_start
        ]
        
        return max(0, self.max_requests - len(self._requests[user_id]))


class CommandDispatcher:
    """
    命令分发器
    
    职责：
    1. 注册和管理命令处理器
    2. 解析消息中的命令和参数
    3. 分发命令到对应处理器
    4. 处理未知命令和错误
    
    使用示例：
        dispatcher = CommandDispatcher()
        dispatcher.register(AnalyzeCommand())
        dispatcher.register(HelpCommand())
        
        response = dispatcher.dispatch(message)
    """
    
    def __init__(
        self, 
        command_prefix: str = "/",
        rate_limit_requests: int = 10,
        rate_limit_window: int = 60,
        admin_users: Optional[List[str]] = None
    ):
        """
        Args:
            command_prefix: 命令前缀，默认 "/"
            rate_limit_requests: 频率限制：窗口内最大请求数
            rate_limit_window: 频率限制：窗口时间（秒）
            admin_users: 管理员用户 ID 列表
        """
        self.command_prefix = command_prefix
        self.admin_users = set(admin_users or [])
        
        self._commands: Dict[str, BotCommand] = {}
        self._aliases: Dict[str, str] = {}
        self._rate_limiter = RateLimiter(rate_limit_requests, rate_limit_window)
        self._intent_analyzer = None
        self._intent_analyzer_ready = False
        self._analysis_chat_sessions: Dict[str, Dict[str, Any]] = {}
        self._analysis_chat_lock = threading.Lock()
        self._analysis_chat_ttl_seconds = 12 * 3600
        cfg = get_config()
        self._analysis_chat_max_turns = max(1, int(getattr(cfg, "bot_analysis_chat_max_turns", 3)))
        self._analysis_chat_quote_max_chars = max(80, int(getattr(cfg, "bot_analysis_chat_quote_max_chars", 220)))
        self._analysis_chat_base_context_max_chars = max(300, int(getattr(cfg, "bot_analysis_chat_base_context_max_chars", 800)))
        self._analysis_chat_history_q_max_chars = max(50, int(getattr(cfg, "bot_analysis_chat_history_q_max_chars", 120)))
        self._analysis_chat_history_a_max_chars = max(80, int(getattr(cfg, "bot_analysis_chat_history_a_max_chars", 220)))
        self._analysis_chat_prompt_max_chars = max(1200, int(getattr(cfg, "bot_analysis_chat_prompt_max_chars", 3200)))
        
        # 回调函数：获取帮助命令的命令列表
        self._help_command_getter: Optional[Callable] = None
    
    def register(self, command: BotCommand) -> None:
        """
        注册命令
        
        Args:
            command: 命令实例
        """
        name = command.name.lower()
        
        if name in self._commands:
            logger.warning(f"[Dispatcher] 命令 '{name}' 已存在，将被覆盖")
        
        self._commands[name] = command
        logger.debug(f"[Dispatcher] 注册命令: {name}")
        
        # 注册别名
        for alias in command.aliases:
            alias_lower = alias.lower()
            if alias_lower in self._aliases:
                logger.warning(f"[Dispatcher] 别名 '{alias_lower}' 已存在，将被覆盖")
            self._aliases[alias_lower] = name
            logger.debug(f"[Dispatcher] 注册别名: {alias_lower} -> {name}")
    
    def register_class(self, command_class: Type[BotCommand]) -> None:
        """
        注册命令类（自动实例化）
        
        Args:
            command_class: 命令类
        """
        self.register(command_class())
    
    def unregister(self, name: str) -> bool:
        """
        注销命令
        
        Args:
            name: 命令名称
            
        Returns:
            是否成功注销
        """
        name = name.lower()
        
        if name not in self._commands:
            return False
        
        command = self._commands.pop(name)
        
        # 移除别名
        for alias in command.aliases:
            self._aliases.pop(alias.lower(), None)
        
        logger.debug(f"[Dispatcher] 注销命令: {name}")
        return True
    
    def get_command(self, name: str) -> Optional[BotCommand]:
        """
        获取命令
        
        支持命令名和别名查询。
        
        Args:
            name: 命令名或别名
            
        Returns:
            命令实例，或 None
        """
        name = name.lower()
        
        # 先查命令名
        if name in self._commands:
            return self._commands[name]
        
        # 再查别名
        if name in self._aliases:
            return self._commands.get(self._aliases[name])
        
        return None
    
    def list_commands(self, include_hidden: bool = False) -> List[BotCommand]:
        """
        列出所有命令
        
        Args:
            include_hidden: 是否包含隐藏命令
            
        Returns:
            命令列表
        """
        commands = list(self._commands.values())
        
        if not include_hidden:
            commands = [c for c in commands if not c.hidden]
        
        return sorted(commands, key=lambda c: c.name)
    
    def is_admin(self, user_id: str) -> bool:
        """检查用户是否是管理员"""
        return user_id in self.admin_users
    
    def add_admin(self, user_id: str) -> None:
        """添加管理员"""
        self.admin_users.add(user_id)
    
    def remove_admin(self, user_id: str) -> None:
        """移除管理员"""
        self.admin_users.discard(user_id)
    
    def dispatch(self, message: BotMessage) -> BotResponse:
        """
        分发消息到对应命令
        
        Args:
            message: 消息对象
            
        Returns:
            响应对象
        """
        # 1. 检查频率限制
        if not self._rate_limiter.is_allowed(message.user_id):
            remaining_time = self._rate_limiter.window_seconds
            return BotResponse.error_response(
                f"请求过于频繁，请 {remaining_time} 秒后再试"
            )
        
        # 2. 解析命令和参数
        cmd_name, args = message.get_command_and_args(self.command_prefix)
        
        if cmd_name is None:
            chat_type = getattr(message.chat_type, "value", message.chat_type)
            is_private_chat = str(chat_type).lower() == "private"
            raw_event = (getattr(message, "raw_data", {}) or {}).get("event", {}) or {}
            is_feishu_reply = (
                str(getattr(message, "platform", "")).lower() == "feishu"
                and bool(raw_event.get("parent_id") or raw_event.get("root_id") or raw_event.get("thread_id"))
            )

            # 私聊/@机器人/飞书引用回复时，允许自然语言智能识别
            if message.mentioned or is_private_chat or is_feishu_reply:
                # 飞书引用回复：默认走对话，不走意图命令路由
                if is_feishu_reply:
                    contextual_reply = self._reply_with_analysis_context(message)
                    if contextual_reply is not None:
                        return contextual_reply
                    free_chat = self._reply_with_free_chat(message)
                    if free_chat is not None:
                        return free_chat
                    return BotResponse.text_response(
                        "这条引用我先按对话处理了，但暂时没生成答案。"
                        "你可以补充下更具体的问题。"
                    )
                smart = self._route_with_smart_intent(message, raw_cmd_name=None, raw_args=[])
                if smart is not None:
                    return smart
                contextual_reply = self._reply_with_analysis_context(message)
                if contextual_reply is not None:
                    return contextual_reply
                # 不是命令，检查是否 @了机器人
                return BotResponse.text_response(
                    "你好！我是股票分析助手。\n"
                    f"发送 `{self.command_prefix}help` 查看可用命令。"
                )
            # 非命令消息，不处理
            return BotResponse.text_response("")
        
        logger.info(f"[Dispatcher] 收到命令: {cmd_name}, 参数: {args}, 用户: {message.user_name}")
        
        # 3. 查找命令处理器
        command = self.get_command(cmd_name)
        
        if command is None:
            smart = self._route_with_smart_intent(message, raw_cmd_name=cmd_name, raw_args=args)
            if smart is not None:
                return smart
            return BotResponse.error_response(
                f"未知命令: {cmd_name}\n"
                f"发送 `{self.command_prefix}help` 查看可用命令。"
            )
        
        # 4. 检查权限
        if command.admin_only and not self.is_admin(message.user_id):
            return BotResponse.error_response("此命令需要管理员权限")
        
        # 5. 验证参数
        error_msg = command.validate_args(args)
        if error_msg:
            return BotResponse.error_response(
                f"{error_msg}\n用法: `{command.usage}`"
            )
        
        # 6. 执行命令
        try:
            response = command.execute(message, args)
            self._seed_analysis_session_from_command(message, cmd_name, args)
            logger.info(f"[Dispatcher] 命令 {cmd_name} 执行成功")
            return response
        except Exception as e:
            logger.error(f"[Dispatcher] 命令 {cmd_name} 执行失败: {e}")
            logger.exception(e)
            return BotResponse.error_response(f"命令执行失败: {str(e)[:100]}")

    def _seed_analysis_session_from_command(self, message: BotMessage, cmd_name: str, args: List[str]) -> None:
        """
        命令触发时给“分析续聊”预热会话，便于后续不写代码也能继续问。
        """
        name = (cmd_name or "").strip().lower()
        if name not in {"a", "analyze", "reanalyze", "分析", "查", "重新分析"}:
            return
        if not args:
            return
        probable = ""
        for t in args:
            tok = (t or "").strip().upper()
            if self._is_probable_stock_code(tok):
                probable = tok
                break
        if not probable:
            return
        context_text = self._load_latest_analysis_context_by_code(probable)
        key = self._analysis_session_key(message)
        with self._analysis_chat_lock:
            session = self._analysis_chat_sessions.get(key, {"turns": []})
            session["last_code"] = probable
            if context_text:
                session["base_context"] = context_text
            session["updated_at"] = time.time()
            self._analysis_chat_sessions[key] = session

    def _route_with_smart_intent(
        self,
        message: BotMessage,
        raw_cmd_name: Optional[str],
        raw_args: List[str],
    ) -> Optional[BotResponse]:
        """
        未知命令/自然语言兜底：AI 意图识别并转为已有命令执行。
        """
        config = get_config()
        # 没有可用 AI 时不启用
        if not (config.gemini_api_key or config.openai_api_key):
            return None

        text = (message.content or "").strip()
        if not text:
            return None

        # 0) 先走“能力清单 + 规划器”，让 AI 可组合多个功能
        plan = self._plan_intent_with_ai(text)
        if plan.get("steps"):
            planned = self._execute_planned_steps(plan, message)
            if planned is not None:
                return self._with_plan_summary(plan, planned)

        intent = self._infer_intent(text)
        if not intent or intent.get("action") in {"none", "", None}:
            return None

        result = self._execute_intent(intent, message, raw_cmd_name, raw_args)
        if result is None:
            return None
        return self._with_intent_summary(intent, result)

    @staticmethod
    def _is_probable_stock_code(token: str) -> bool:
        t = (token or "").strip().upper()
        if not t:
            return False
        if re.match(r'^\d{6}$', t):   # A股
            return True
        if re.match(r'^HK\d{5}$', t):  # 港股
            return True
        if re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', t):  # 美股/ETF
            return True
        return False

    def _analysis_session_key(self, message: BotMessage) -> str:
        raw_event = (getattr(message, "raw_data", {}) or {}).get("event", {}) or {}
        thread_id = (
            raw_event.get("root_id")
            or raw_event.get("thread_id")
            or raw_event.get("parent_id")
            or message.chat_id
        )
        platform = (message.platform or "").strip().lower()
        return f"{platform}:{message.chat_id}:{message.user_id}:{thread_id}"

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        s = (text or "").strip()
        if max_chars <= 0 or len(s) <= max_chars:
            return s
        return s[:max_chars].rstrip() + "..."

    @staticmethod
    def _format_analysis_context_from_row(row: Any) -> str:
        raw = {}
        try:
            raw = json.loads(getattr(row, "raw_result", "") or "{}")
        except Exception:
            raw = {}
        code = raw.get("code") or getattr(row, "code", "")
        name = raw.get("name") or getattr(row, "name", "") or code
        advice = raw.get("operation_advice") or getattr(row, "operation_advice", "") or "观望"
        trend = raw.get("trend_prediction") or getattr(row, "trend_prediction", "") or "震荡"
        summary = raw.get("analysis_summary") or getattr(row, "analysis_summary", "") or ""
        dashboard = raw.get("dashboard") or {}
        core = (dashboard.get("core_conclusion", {}) if isinstance(dashboard, dict) else {}) or {}
        battle = (dashboard.get("battle_plan", {}) if isinstance(dashboard, dict) else {}) or {}
        sniper = (battle.get("sniper_points", {}) if isinstance(battle, dict) else {}) or {}
        stop_loss = sniper.get("stop_loss") or "N/A"
        take_profit = sniper.get("take_profit") or "N/A"
        one_sentence = core.get("one_sentence") or ""
        return (
            f"标的: {name}({code})\n"
            f"结论: {advice} | 趋势: {trend}\n"
            f"一句话: {one_sentence}\n"
            f"摘要: {summary}\n"
            f"止损: {stop_loss} | 目标: {take_profit}"
        ).strip()

    def _load_latest_analysis_context_by_code(self, code: str) -> str:
        try:
            from src.storage import get_db
            db = get_db()
            rows = db.get_analysis_history(code=code, days=365, limit=1)
            if not rows:
                return ""
            return self._format_analysis_context_from_row(rows[0])
        except Exception as e:
            logger.debug(f"[Dispatcher] 读取分析上下文失败 code={code}: {e}")
            return ""

    def _load_latest_analysis_context_any(self) -> str:
        """
        兜底读取最近一次分析上下文（跨线程/重启后可恢复）。
        """
        try:
            from src.storage import get_db
            db = get_db()
            rows = db.get_analysis_history(days=14, limit=1)
            if not rows:
                return ""
            return self._format_analysis_context_from_row(rows[0])
        except Exception as e:
            logger.debug(f"[Dispatcher] 读取最近分析上下文失败: {e}")
            return ""

    def _reply_with_analysis_context(self, message: BotMessage) -> Optional[BotResponse]:
        """
        基于“引用分析 + 同线程记忆”进行 AI 连续对话。
        """
        text = (message.content or "").strip()
        if not text:
            return None
        raw_event = (getattr(message, "raw_data", {}) or {}).get("event", {}) or {}
        referenced_text = str(raw_event.get("referenced_text", "") or "").strip()
        referenced_message_id = str(raw_event.get("referenced_message_id", "") or "")
        raw_hint = self._extract_reply_quote_hint(message.raw_content or "")
        quote_context = self._clip_text(referenced_text or raw_hint, self._analysis_chat_quote_max_chars)
        has_quote_ref = bool(raw_event.get("parent_id") or raw_event.get("root_id") or raw_event.get("thread_id"))

        analyzer = self._get_intent_analyzer()
        if analyzer is None:
            return None

        key = self._analysis_session_key(message)
        now = time.time()
        with self._analysis_chat_lock:
            expired_keys = [
                k for k, v in self._analysis_chat_sessions.items()
                if now - float(v.get("updated_at", 0)) > self._analysis_chat_ttl_seconds
            ]
            for k in expired_keys:
                self._analysis_chat_sessions.pop(k, None)
            session = self._analysis_chat_sessions.get(key, {"turns": [], "base_context": "", "last_code": ""})
            # 若当前线程未命中（飞书引用常见），回退到同用户同会话最新一次分析上下文
            if not session.get("base_context"):
                prefix = f"{(message.platform or '').strip().lower()}:{message.chat_id}:{message.user_id}:"
                candidates = [
                    v for k, v in self._analysis_chat_sessions.items()
                    if k.startswith(prefix) and v.get("base_context")
                ]
                if candidates:
                    candidates.sort(key=lambda x: float(x.get("updated_at", 0)), reverse=True)
                    latest = candidates[0]
                    session = {
                        "turns": latest.get("turns", []),
                        "base_context": latest.get("base_context", ""),
                        "last_code": latest.get("last_code", ""),
                    }

        items = self._extract_stock_items_from_text(text)
        quote_codes = self._extract_codes_from_quote(quote_context)
        if referenced_text:
            items = items + self._extract_stock_items_from_text(referenced_text)
        probable_codes = [x.upper() for x in quote_codes if self._is_probable_stock_code(x)]
        probable_codes += [x.upper() for x in items if self._is_probable_stock_code(x)]
        if not probable_codes:
            # 兜底：从原始文本中再抓一次代码形态
            for token in re.findall(r"[A-Za-z0-9\.]+", message.raw_content or text):
                if self._is_probable_stock_code(token):
                    probable_codes.append(token.upper())
                    break
        if not probable_codes and referenced_text:
            for token in re.findall(r"[A-Za-z0-9\.]+", referenced_text):
                if self._is_probable_stock_code(token):
                    probable_codes.append(token.upper())
                    break

        selected_code = probable_codes[0] if probable_codes else session.get("last_code", "")
        base_context = session.get("base_context", "")
        if selected_code and (not base_context or selected_code != session.get("last_code")):
            loaded = self._load_latest_analysis_context_by_code(selected_code)
            if loaded:
                base_context = loaded

        # 非引用场景可回退“最近一次分析记录”；引用场景禁止全局回退，避免串标的
        if not base_context and not has_quote_ref:
            base_context = self._load_latest_analysis_context_any()

        # 再兜底：若没有历史分析上下文，但有引用正文，则把引用正文作为临时上下文
        if not base_context and quote_context:
            base_context = f"引用正文:\n{quote_context}"

        # 没有基础分析上下文时，不启用续聊
        if not base_context:
            return None

        base_context = self._clip_text(base_context, self._analysis_chat_base_context_max_chars)
        turns = session.get("turns", [])[-self._analysis_chat_max_turns:]
        history_lines = []
        for t in turns:
            q = self._clip_text(str(t.get("q", "")), self._analysis_chat_history_q_max_chars)
            a = self._clip_text(str(t.get("a", "")), self._analysis_chat_history_a_max_chars)
            if q or a:
                history_lines.append(f"Q: {q}\nA: {a}")
        history_text = "\n".join(history_lines) or "无"

        prompt = (
            "你是股票分析助手，基于既有报告做续聊答疑。\n"
            "要求：基于给定上下文回答；信息不足要明确缺口；输出简洁 Markdown；结尾加“风险提示：仅供参考，不构成投资建议”。\n\n"
            f"【引用正文】\n{quote_context or '无'}\n\n"
            f"【分析上下文】\n{base_context}\n\n"
            f"【同线程最近对话】\n{history_text}\n\n"
            f"【用户问题】\n{text}"
        )
        prompt = self._clip_text(prompt, self._analysis_chat_prompt_max_chars)
        try:
            answer = analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.3, "max_output_tokens": 900},
            )
            answer = (answer or "").strip()
            if not answer:
                return None

            new_turns = turns + [{
                "q": self._clip_text(text, self._analysis_chat_history_q_max_chars * 2),
                "a": self._clip_text(answer, self._analysis_chat_history_a_max_chars * 2),
            }]
            if len(new_turns) > self._analysis_chat_max_turns:
                new_turns = new_turns[-self._analysis_chat_max_turns:]

            with self._analysis_chat_lock:
                self._analysis_chat_sessions[key] = {
                    "base_context": base_context,
                    "last_code": selected_code,
                    "turns": new_turns,
                    "updated_at": time.time(),
                }
            if has_quote_ref:
                quote_preview = (quote_context or "").replace("\n", " ").strip()
                if len(quote_preview) > 220:
                    quote_preview = quote_preview[:220] + "..."
                debug_lines = [
                    "### 调试信息",
                    f"- 引用消息ID: `{referenced_message_id or 'N/A'}`",
                    f"- 引用正文长度: `{len(quote_context or '')}`",
                    f"- 引用正文片段: `{quote_preview or 'N/A'}`",
                    f"- 提取代码: `{', '.join(probable_codes[:8]) if probable_codes else 'N/A'}`",
                    f"- 选中代码: `{selected_code or 'N/A'}`",
                ]
                answer = "\n".join(debug_lines) + "\n\n" + answer
            return BotResponse.markdown_response(answer, at_user=True)
        except Exception as e:
            logger.debug(f"[Dispatcher] 分析续聊失败: {e}")
            return None

    @staticmethod
    def _extract_reply_quote_hint(raw_content: str) -> str:
        """
        从飞书原始文本中提取“回复 XXX: YYY”的引用提示。
        """
        s = (raw_content or "").strip()
        if not s:
            return ""
        m = re.search(r"回复\s*[^:：]{1,40}\s*[:：]\s*([^\n]{1,400})", s)
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _extract_codes_from_quote(text: str) -> List[str]:
        """
        从引用正文中优先提取显式代码（如 ANET、HK00700、600519）。
        对括号包裹代码优先，避免误提取普通英文词。
        """
        s = (text or "").upper()
        if not s:
            return []
        out: List[str] = []
        patterns = [
            r"\((HK\d{5}|\d{6}|[A-Z]{1,5}(?:\.[A-Z]{1,2})?)\)",
            r"`(HK\d{5}|\d{6}|[A-Z]{1,5}(?:\.[A-Z]{1,2})?)`",
            r"\b(HK\d{5}|\d{6}|[A-Z]{2,5}(?:\.[A-Z]{1,2})?)\b",
        ]
        for p in patterns:
            for c in re.findall(p, s):
                code = (c or "").strip().upper()
                if code in {"A", "H", "CN", "HK", "US"}:
                    continue
                if code not in out:
                    out.append(code)
        return out

    @staticmethod
    def _prefer_free_chat(text: str) -> bool:
        s = (text or "").strip()
        if not s:
            return False
        lowered = s.lower()
        cmd_like = ["/", "批量", "复盘", "watchlist", "position set", "position remove", "添加自选", "删除持仓"]
        if any(k in lowered for k in cmd_like):
            return False
        discuss_markers = ["建议", "怎么看", "为什么", "多少", "要不要", "是否", "风险", "仓位", "?", "？"]
        return any(k in s for k in discuss_markers)

    def _reply_with_free_chat(self, message: BotMessage) -> Optional[BotResponse]:
        """
        无明确命令意图时的 AI 直接问答（轻量）。
        """
        text = (message.content or "").strip()
        if not text:
            return None
        raw_event = (getattr(message, "raw_data", {}) or {}).get("event", {}) or {}
        referenced_text = str(raw_event.get("referenced_text", "") or "").strip()
        raw_hint = self._extract_reply_quote_hint(message.raw_content or "")
        quote_context = self._clip_text(referenced_text or raw_hint, self._analysis_chat_quote_max_chars)
        analyzer = self._get_intent_analyzer()
        if analyzer is None:
            return None
        prompt = (
            "你是股票分析助手。用户在追问上一条分析结果。"
            "请直接给出简洁、可执行的回答；若信息不足，明确说明缺哪些数据。"
            "输出 Markdown；结尾附一句：风险提示：仅供参考，不构成投资建议。\n\n"
            f"引用正文：{quote_context or '无'}\n"
            f"用户问题：{text}"
        )
        try:
            answer = analyzer._call_api_with_retry(
                prompt,
                generation_config={"temperature": 0.3, "max_output_tokens": 700},
            )
            answer = (answer or "").strip()
            if not answer:
                return None
            return BotResponse.markdown_response(answer, at_user=True)
        except Exception as e:
            logger.debug(f"[Dispatcher] 自由问答失败: {e}")
            return None

    def _get_intent_analyzer(self):
        """懒加载 AI 分析器用于意图识别。"""
        if self._intent_analyzer_ready:
            return self._intent_analyzer
        self._intent_analyzer_ready = True
        try:
            from src.analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer()
            if analyzer.is_available():
                self._intent_analyzer = analyzer
                return self._intent_analyzer
        except Exception as e:
            logger.debug(f"[Dispatcher] 初始化意图分析器失败: {e}")
        return None

    def _infer_intent(self, text: str) -> Dict[str, Any]:
        """
        识别用户自然语言意图。
        返回格式：
        {"action": "...", "items": [...], "market": "CN|HK|US|", "reply": "..."}
        """
        # 先用 AI，失败再走规则兜底
        ai_intent = self._infer_intent_with_ai(text)
        if ai_intent:
            return ai_intent
        return self._infer_intent_with_rules(text)

    def _infer_intent_with_ai(self, text: str) -> Dict[str, Any]:
        analyzer = self._get_intent_analyzer()
        if analyzer is None:
            return {}

        prompt = (
            "你是一个机器人命令路由器。请根据用户输入识别意图，"
            "仅输出 JSON，不要输出其它文本。\n"
            "允许 action: help,status,market,batch,history,watchlist_list,watchlist_add,analyze,reanalyze,position_list,position_set,position_remove,none。\n"
            "输出格式:\n"
            "{\"action\":\"...\",\"items\":[\"...\"],\"market\":\"CN|HK|US|\",\"avg_cost\":0,\"weight_pct\":0,\"reply\":\"\"}\n"
            "规则:\n"
            "1) 若用户说把某些股票加入自选，action=watchlist_add，items 填股票代码或名称列表。\n"
            "2) 若用户让你分析自选股（例如“分析今天A股和港股的自选股”），action=batch，不要误识别为单股 analyze。\n"
            "3) 若用户让你分析某只股票，action=analyze，items 仅放一个目标。\n"
            "4) 若用户明确要求重新分析/重跑/强制分析，action=reanalyze。\n"
            "5) 若用户查询持仓，action=position_list。\n"
            "6) 若用户设置持仓(包含成本+仓位)，action=position_set，items 放股票目标，avg_cost/weight_pct 填数值。\n"
            "7) 若用户删除持仓，action=position_remove，items 放股票目标。\n"
            "8) 若用户问历史报告/历史分析记录，action=history，items 放一个股票目标。\n"
            "9) 若用户问“今天A股/港股怎么操作”或“今天/今晚美股怎么操作”，action=market，market 填 CN/HK/US（多市场不确定时留空）。\n"
            "10) 若用户问帮助/状态/大盘/批量/看自选列表，对应 action。\n"
            "11) 无法判断时 action=none。\n\n"
            f"用户输入: {text}"
        )
        try:
            out = analyzer._call_api_with_retry(
                prompt,
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 512,
                }
            )
            json_text = self._extract_json_text(out)
            data = json.loads(json_text)
            action = str(data.get("action", "")).strip().lower()
            if action not in {"help", "status", "market", "batch", "history", "watchlist_list", "watchlist_add", "analyze", "reanalyze", "position_list", "position_set", "position_remove", "none"}:
                return {}
            items = data.get("items", [])
            if not isinstance(items, list):
                items = []
            items = [str(x).strip() for x in items if str(x).strip()]
            # 过滤明显的市场标记，避免 "A股" 被识别成股票 A
            items = [x for x in items if x.upper() not in {"A", "H", "CN", "HK", "US", "A股".upper(), "港股".upper(), "美股".upper()}]
            market = str(data.get("market", "")).strip().upper()
            if market not in {"CN", "HK", "US"}:
                market = ""
            avg_cost = data.get("avg_cost")
            weight_pct = data.get("weight_pct")
            return {"action": action, "items": items, "market": market, "avg_cost": avg_cost, "weight_pct": weight_pct}
        except Exception as e:
            logger.debug(f"[Dispatcher] AI 意图识别失败: {e}")
            return {}

    def _plan_intent_with_ai(self, text: str) -> Dict[str, Any]:
        """
        使用“能力清单 + 计划”模式识别用户意图。
        返回:
        {
          "intent": "...",
          "steps": [{"tool":"...", "args": {...}}],
          "reason": "..."
        }
        """
        analyzer = self._get_intent_analyzer()
        if analyzer is None:
            return {}

        catalog = self._tool_catalog()
        prompt = (
            "你是机器人调度规划器。你会收到用户输入与可用能力清单。"
            "请只输出 JSON，不要输出任何其它文字。\n"
            "目标：根据用户意图规划最少步骤，必要时组合多个能力。\n"
            "输出格式:\n"
            "{\"intent\":\"...\",\"steps\":[{\"tool\":\"...\",\"args\":{}}],\"reason\":\"...\"}\n"
            "约束:\n"
            "1) tool 必须来自清单，不能发明新工具。\n"
            "2) 股票目标优先放到 args.items（数组）或 args.target（单值）。\n"
            "3) 多市场批量时用 args.markets，如 [\"CN\",\"HK\"]。\n"
            "4) 对“分析+自选股”优先使用 batch，不要误判成单股 analyze。\n"
            "5) 对“今晚/今天怎么操作、操作建议”优先给出 batch，并设置 args.source=\"holdings\"（按用户持仓分析）。\n"
            "5) 不确定时返回 steps=[]。\n\n"
            f"能力清单: {json.dumps(catalog, ensure_ascii=False)}\n"
            f"用户输入: {text}"
        )

        try:
            out = analyzer._call_api_with_retry(
                prompt,
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 512,
                }
            )
            raw_json = self._extract_json_text(out)
            data = json.loads(raw_json) if raw_json else {}
            steps = self._normalize_plan_steps(data.get("steps", []))
            if not steps:
                return {}
            return {
                "intent": str(data.get("intent", "")).strip(),
                "steps": steps,
                "reason": str(data.get("reason", "")).strip(),
            }
        except Exception as e:
            logger.debug(f"[Dispatcher] AI 计划识别失败: {e}")
            return {}

    @staticmethod
    def _tool_catalog() -> List[Dict[str, Any]]:
        """提供给 AI 的能力清单。"""
        return [
            {
                "tool": "help",
                "description": "查看帮助与命令说明",
                "args_schema": {}
            },
            {
                "tool": "status",
                "description": "查看系统状态",
                "args_schema": {}
            },
            {
                "tool": "market",
                "description": "查看市场复盘",
                "args_schema": {}
            },
            {
                "tool": "analyze",
                "description": "分析单只股票",
                "args_schema": {"target": "股票代码或名称", "market": "CN|HK|US 可选"}
            },
            {
                "tool": "reanalyze",
                "description": "强制重新分析单只股票（忽略当日缓存）",
                "args_schema": {"target": "股票代码或名称", "market": "CN|HK|US 可选"}
            },
            {
                "tool": "batch",
                "description": "批量分析自选股，可按市场筛选",
                "args_schema": {"markets": ["CN|HK|US"], "source": "watchlist|holdings", "limit": "整数可选"}
            },
            {
                "tool": "history",
                "description": "查看个股历史分析报告",
                "args_schema": {"target": "股票代码或名称", "limit": "整数可选"}
            },
            {
                "tool": "watchlist_list",
                "description": "查看自选股",
                "args_schema": {}
            },
            {
                "tool": "watchlist_add",
                "description": "添加自选股",
                "args_schema": {"items": ["代码或名称"], "market": "CN|HK|US 可选"}
            },
            {
                "tool": "position_list",
                "description": "查看持仓列表",
                "args_schema": {}
            },
            {
                "tool": "position_set",
                "description": "设置持仓（成本与仓位）",
                "args_schema": {
                    "target": "代码或名称",
                    "avg_cost": "数字",
                    "weight_pct": "数字 0-100",
                    "market": "CN|HK|US 可选"
                }
            },
            {
                "tool": "position_remove",
                "description": "删除持仓",
                "args_schema": {"target": "代码或名称", "market": "CN|HK|US 可选"}
            },
        ]

    def _normalize_plan_steps(self, steps: Any) -> List[Dict[str, Any]]:
        """校验并标准化 AI 规划步骤。"""
        if not isinstance(steps, list):
            return []
        allowed_tools = {
            "help", "status", "market", "batch", "history", "watchlist_list", "watchlist_add",
            "reanalyze",
            "analyze", "position_list", "position_set", "position_remove"
        }
        normalized = []
        for step in steps[:4]:
            if not isinstance(step, dict):
                continue
            tool = str(step.get("tool", "")).strip().lower()
            if tool not in allowed_tools:
                continue
            args = step.get("args", {})
            if not isinstance(args, dict):
                args = {}
            normalized.append({"tool": tool, "args": args})
        return normalized

    def _execute_planned_steps(self, plan: Dict[str, Any], message: BotMessage) -> Optional[BotResponse]:
        """
        执行 AI 规划出的步骤（可多步组合）。
        """
        steps = plan.get("steps", []) or []
        if not steps:
            return None

        responses: List[Tuple[str, BotResponse]] = []
        for step in steps:
            tool = step.get("tool", "")
            args = step.get("args", {}) or {}
            step_resps = self._execute_one_planned_tool(tool, args, message)
            for r in step_resps:
                if r is not None:
                    responses.append((tool, r))

        if not responses:
            return None
        if len(responses) == 1:
            return responses[0][1]

        lines = []
        for idx, (tool, resp) in enumerate(responses, start=1):
            title = {
                "batch": "批量分析",
                "history": "历史报告",
                "analyze": "单股分析",
                "reanalyze": "重新分析",
                "watchlist_add": "添加自选股",
                "watchlist_list": "查看自选股",
                "position_set": "设置持仓",
                "position_remove": "删除持仓",
                "position_list": "查看持仓",
                "help": "帮助",
                "status": "系统状态",
                "market": "大盘复盘",
            }.get(tool, tool)
            lines.append(f"### 步骤 {idx}: {title}")
            lines.append((resp.text or "").strip())
            lines.append("")

        return BotResponse.markdown_response("\n".join(lines).strip(), at_user=True)

    def _execute_one_planned_tool(self, tool: str, args: Dict[str, Any], message: BotMessage) -> List[Optional[BotResponse]]:
        """
        执行单个规划工具步骤。
        返回列表是为了支持 batch 多市场扩展成多次执行。
        """
        market = str(args.get("market", "")).strip().upper()
        items = args.get("items", [])
        if not isinstance(items, list):
            items = []
        items = [str(x).strip() for x in items if str(x).strip()]
        target = str(args.get("target", "")).strip()
        if target and not items:
            items = [target]

        if tool == "batch":
            markets = args.get("markets", [])
            if not isinstance(markets, list):
                markets = []
            markets = [str(x).strip().upper() for x in markets if str(x).strip()]
            markets = [m for m in markets if m in {"CN", "HK", "US"}]
            source = str(args.get("source", "")).strip().lower()
            source_token = "holdings" if source in {"holding", "holdings", "position", "positions", "持仓"} else ""
            limit = args.get("limit")
            limit_token = None
            if isinstance(limit, (int, float)):
                limit_token = str(int(limit))
            elif isinstance(limit, str) and limit.strip().isdigit():
                limit_token = str(int(limit.strip()))

            if not markets:
                intent = {"action": "batch", "items": [], "market": ""}
                # batch 命令支持市场参数，通过 raw_args 传递更准确
                cmd = self.get_command("batch")
                if not cmd:
                    return [None]
                cmd_args = []
                if source_token:
                    cmd_args.append(source_token)
                if market in {"CN", "HK", "US"}:
                    cmd_args.append(market)
                if limit_token:
                    cmd_args.append(limit_token)
                return [cmd.execute(message, cmd_args)]

            cmd = self.get_command("batch")
            if not cmd:
                return [None]
            out = []
            for m in markets:
                cmd_args = []
                if source_token:
                    cmd_args.append(source_token)
                cmd_args.append(m)
                if limit_token:
                    cmd_args.append(limit_token)
                out.append(cmd.execute(message, cmd_args))
            return out

        intent = {"action": tool, "items": items, "market": market}
        if "avg_cost" in args:
            intent["avg_cost"] = args.get("avg_cost")
        if "weight_pct" in args:
            intent["weight_pct"] = args.get("weight_pct")
        return [self._execute_intent(intent, message, raw_cmd_name=None, raw_args=[])]

    @staticmethod
    def _with_plan_summary(plan: Dict[str, Any], response: BotResponse) -> BotResponse:
        intent = str(plan.get("intent", "")).strip() or "自动规划"
        reason = str(plan.get("reason", "")).strip()
        steps = plan.get("steps", []) or []
        step_desc = []
        for s in steps:
            tool = s.get("tool", "")
            args = s.get("args", {}) or {}
            step_desc.append(f"{tool}({json.dumps(args, ensure_ascii=False)})")
        detail = " -> ".join(step_desc[:6]) if step_desc else "无"
        prefix = (
            "🤖 **智能规划结果**\n"
            f"• 意图: {intent}\n"
            f"• 执行计划: {detail}\n"
        )
        if reason:
            prefix += f"• 依据: {reason}\n"
        prefix += "\n"

        return BotResponse(
            text=prefix + (response.text or ""),
            markdown=True,
            at_user=response.at_user,
            reply_to_message=response.reply_to_message,
            extra=response.extra,
        )

    @staticmethod
    def _extract_json_text(text: str) -> str:
        """从模型输出中提取 JSON 文本。"""
        raw = (text or "").strip()
        if raw.startswith("{") and raw.endswith("}"):
            return raw
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return m.group(0)
        return "{}"

    def _infer_intent_with_rules(self, text: str) -> Dict[str, Any]:
        """关键词兜底意图识别。"""
        s = (text or "").strip()
        lower = s.lower()
        if any(k in s for k in ["帮助", "help", "怎么用"]):
            return {"action": "help", "items": [], "market": ""}
        if any(k in s for k in ["状态", "status", "系统"]):
            return {"action": "status", "items": [], "market": ""}
        if any(k in s for k in ["大盘", "复盘", "market"]):
            return {"action": "market", "items": [], "market": ""}
        if any(k in s for k in ["怎么操作", "如何操作", "操作建议", "今晚"]) and any(k in s for k in ["A股", "港股", "美股", "沪深", "恒生", "纳指", "标普"]):
            return {"action": "market", "items": [], "market": ""}
        if any(k in s for k in ["批量", "全部分析", "batch"]):
            return {"action": "batch", "items": [], "market": ""}
        if any(k in s for k in ["历史", "history", "记录"]) and any(k in s for k in ["报告", "分析", "查看", "看下", "查下"]):
            items = self._extract_stock_items_from_text(s)
            if items:
                return {"action": "history", "items": [items[0]], "market": ""}
            return {"action": "history", "items": [], "market": ""}
        if any(k in s for k in ["重新分析", "重跑分析", "强制分析", "重跑", "重新跑"]) and any(k in s for k in ["分析", "analyze", "看下", "查下"]):
            items = self._extract_stock_items_from_text(s)
            if items:
                return {"action": "reanalyze", "items": [items[0]], "market": ""}
        # “分析 + 自选股”优先按 batch 处理，避免误识别成单股（例如把 A股 识别成 A）
        if "自选" in s and any(k in s for k in ["分析", "analyze", "看下", "查下"]):
            market = ""
            has_cn = ("A股" in s) or ("沪深" in s) or ("CN" in s.upper())
            has_hk = ("港股" in s) or ("HK" in s.upper())
            has_us = ("美股" in s) or ("US" in s.upper())
            # 同时提到多个市场时不传 market，交给 batch 全量/后续过滤逻辑处理
            if sum([1 if has_cn else 0, 1 if has_hk else 0, 1 if has_us else 0]) == 1:
                market = "CN" if has_cn else ("HK" if has_hk else "US")
            return {"action": "batch", "items": [], "market": market}
        if any(k in s for k in ["持仓", "仓位"]) and any(k in s for k in ["查看", "列表", "现在", "情况"]):
            return {"action": "position_list", "items": [], "market": ""}
        if "自选" in s and any(k in s for k in ["列表", "查看", "看看"]):
            return {"action": "watchlist_list", "items": [], "market": ""}
        if ("自选" in s and any(k in s for k in ["加", "添加", "加入"])) or "watchlist add" in lower:
            items = self._extract_stock_items_from_text(s)
            return {"action": "watchlist_add", "items": items, "market": ""}
        if any(k in s for k in ["删掉", "删除", "移除"]) and any(k in s for k in ["持仓", "仓位"]):
            target = self._extract_position_target_text(s)
            items = [target] if target else self._extract_stock_items_from_text(s)
            if items:
                return {"action": "position_remove", "items": [items[0]], "market": ""}
        if any(k in s for k in ["持仓", "仓位"]) and any(k in s for k in ["成本", "买入", "仓位"]):
            target = self._extract_position_target_text(s)
            items = [target] if target else self._extract_stock_items_from_text(s)
            avg_cost, weight_pct = self._extract_cost_and_weight(s)
            if items and avg_cost is not None and weight_pct is not None:
                return {
                    "action": "position_set",
                    "items": [items[0]],
                    "market": "",
                    "avg_cost": avg_cost,
                    "weight_pct": weight_pct,
                }
        if any(k in s for k in ["分析", "analyze", "看下", "查下"]):
            items = self._extract_stock_items_from_text(s)
            if items:
                return {"action": "analyze", "items": [items[0]], "market": ""}
        return {"action": "none", "items": [], "market": ""}

    @staticmethod
    def _extract_cost_and_weight(text: str) -> tuple[Optional[float], Optional[float]]:
        s = (text or "").strip()
        cost = None
        weight = None

        m_cost = re.search(r"(?:成本|买入价|均价)\s*[:：]?\s*(\d+(?:\.\d+)?)", s)
        if m_cost:
            try:
                cost = float(m_cost.group(1))
            except ValueError:
                cost = None

        m_weight = re.search(r"(?:仓位|持仓比例)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%?", s)
        if m_weight:
            try:
                weight = float(m_weight.group(1))
            except ValueError:
                weight = None

        # 兜底：提取所有数字，最后两个按“成本 仓位”处理
        if cost is None or weight is None:
            nums = re.findall(r"\d+(?:\.\d+)?", s)
            if len(nums) >= 2:
                try:
                    cost = cost if cost is not None else float(nums[-2])
                    weight = weight if weight is not None else float(nums[-1])
                except ValueError:
                    pass

        return cost, weight

    @staticmethod
    def _extract_position_target_text(text: str) -> Optional[str]:
        """
        针对持仓指令提取目标股票名称/代码。
        """
        s = (text or "").strip()
        # 删除类：删掉苹果持仓 / 删除 腾讯 持仓
        m_del = re.search(r"(?:删掉|删除|移除)\s*([^\s，,。；;]+?)\s*(?:持仓|仓位)?$", s)
        if m_del:
            target = m_del.group(1).strip()
            if target:
                return target

        # 设置类：我买了腾讯，成本320，仓位8%
        m_set = re.search(r"(?:买了|买入|持仓|仓位|设置)\s*([^\s，,。；;:：]+)", s)
        if m_set:
            target = m_set.group(1).strip()
            if target and target not in {"成本", "仓位"}:
                return target

        # 回退：取“成本/仓位”之前的最后一个词
        s2 = re.split(r"(?:成本|仓位|持仓比例)", s)[0]
        tokens = re.split(r"[\s，,。；;]+", s2)
        tokens = [t for t in tokens if t]
        if tokens:
            return tokens[-1]
        return None

    @staticmethod
    def _extract_stock_items_from_text(text: str) -> List[str]:
        """从自然语言中提取可能的股票代码/名称列表。"""
        # 先提取代码
        code_pattern = r"(HK\d{5}|\d{6}|[A-Z]{1,5}(?:\.[A-Z]{1,2})?)"
        raw_codes = re.findall(code_pattern, text.upper())
        codes = []
        for c in raw_codes:
            # 避免把“A股/H股”的市场字母识别成单股代码
            if c in {"A", "H"} and re.search(rf"{c}股", text, re.IGNORECASE):
                continue
            if c in {"CN", "HK", "US"}:
                continue
            codes.append(c)

        # 再提取逗号/空格分隔的中文股票名片段
        cleaned = re.sub(r"[，,;；\n\r\t]+", " ", text)
        tokens = [t.strip() for t in cleaned.split(" ") if t.strip()]
        stopwords = {
            "帮我", "把", "下面", "这些", "股票", "加入", "自选", "自选股",
            "请", "麻烦", "一下", "帮忙", "添加", "到", "里", "我", "想", "要",
            "/watchlist", "watchlist", "add", "/add"
        }
        names = []
        for t in tokens:
            t_norm = t.strip("：:()[]{}")
            if not t_norm or t_norm in stopwords:
                continue
            if re.match(code_pattern + r"$", t_norm.upper()):
                continue
            # 保守过滤：至少2个字符
            if len(t_norm) >= 2:
                names.append(t_norm)

        # 保序去重：代码优先
        merged = []
        seen = set()
        for x in codes + names:
            k = x.upper()
            if k in seen:
                continue
            if k in {"A", "H", "CN", "HK", "US"}:
                continue
            seen.add(k)
            merged.append(x)
        return merged

    def _execute_intent(
        self,
        intent: Dict[str, Any],
        message: BotMessage,
        raw_cmd_name: Optional[str],
        raw_args: List[str],
    ) -> Optional[BotResponse]:
        action = intent.get("action", "")
        items = intent.get("items", []) or []
        market = (intent.get("market", "") or "").upper()

        if action == "help":
            cmd = self.get_command("help")
            return cmd.execute(message, []) if cmd else None
        if action == "status":
            cmd = self.get_command("status")
            return cmd.execute(message, []) if cmd else None
        if action == "market":
            # “今晚/今天怎么操作”优先按持仓做批量分析建议，避免误走纯大盘复盘
            text = (message.content or "").strip()
            if any(k in text for k in ["怎么操作", "如何操作", "操作建议", "今晚", "今天"]) and "大盘复盘" not in text:
                cmd = self.get_command("batch")
                if cmd:
                    cmd_args = ["holdings"]
                    if market in {"CN", "HK", "US"}:
                        cmd_args.append(market)
                    return cmd.execute(message, cmd_args)
            cmd = self.get_command("market")
            return cmd.execute(message, []) if cmd else None
        if action == "batch":
            cmd = self.get_command("batch")
            return cmd.execute(message, []) if cmd else None
        if action == "watchlist_list":
            cmd = self.get_command("watchlist")
            return cmd.execute(message, []) if cmd else None
        if action == "position_list":
            cmd = self.get_command("position")
            return cmd.execute(message, []) if cmd else None
        if action == "history":
            cmd = self.get_command("history")
            if not cmd:
                return None
            if not items:
                return BotResponse.error_response("请告诉我要查看历史的股票代码或名称")
            return cmd.execute(message, [items[0]])
        if action == "analyze":
            if not items:
                return BotResponse.error_response("请告诉我要分析的股票代码或名称")
            cmd = self.get_command("analyze")
            if not cmd:
                return None
            analyze_args = [items[0]]
            if market:
                analyze_args.append(market)
            return cmd.execute(message, analyze_args)
        if action == "reanalyze":
            if not items:
                return BotResponse.error_response("请告诉我要重新分析的股票代码或名称")
            cmd = self.get_command("reanalyze")
            if not cmd:
                return None
            reanalyze_args = [items[0]]
            if market:
                reanalyze_args.append(market)
            return cmd.execute(message, reanalyze_args)
        if action == "watchlist_add":
            if not items:
                return BotResponse.error_response("没有识别到可添加的股票，请补充名称或代码")
            cmd = self.get_command("watchlist")
            if not cmd:
                return None
            success_lines = []
            fail_lines = []
            for item in items[:20]:
                add_args = ["add", item]
                if market:
                    add_args.append(market)
                resp = cmd.execute(message, add_args)
                if resp and resp.text and not resp.text.startswith("❌"):
                    success_lines.append(item)
                else:
                    err = resp.text if resp and resp.text else "添加失败"
                    fail_lines.append(f"{item}: {err}")

            lines = ["🤖 已按你的描述处理自选股：", ""]
            if success_lines:
                lines.append(f"✅ 添加成功（{len(success_lines)}）: {', '.join(success_lines)}")
            if fail_lines:
                lines.append(f"❌ 添加失败（{len(fail_lines)}）:")
                lines.extend([f"• {x}" for x in fail_lines[:8]])
            return BotResponse.markdown_response("\n".join(lines))
        if action == "position_set":
            if not items:
                return BotResponse.error_response("没有识别到持仓标的，请补充股票名称或代码")
            avg_cost = intent.get("avg_cost")
            weight_pct = intent.get("weight_pct")
            if avg_cost is None or weight_pct is None:
                return BotResponse.error_response("未识别到成本/仓位，请使用例如：成本1700 仓位15%")
            cmd = self.get_command("position")
            if not cmd:
                return None
            set_args = ["set", items[0], str(avg_cost), str(weight_pct)]
            if market:
                set_args.append(market)
            return cmd.execute(message, set_args)
        if action == "position_remove":
            if not items:
                return BotResponse.error_response("没有识别到要删除的持仓标的")
            cmd = self.get_command("position")
            if not cmd:
                return None
            rm_args = ["remove", items[0]]
            if market:
                rm_args.append(market)
            return cmd.execute(message, rm_args)

        # action=none 或无法处理
        if raw_cmd_name:
            return BotResponse.error_response(
                f"未知命令: {raw_cmd_name}\n"
                f"发送 `{self.command_prefix}help` 查看可用命令。"
            )
        return None

    @staticmethod
    def _with_intent_summary(intent: Dict[str, Any], response: BotResponse) -> BotResponse:
        """在智能兜底响应前附加“已识别意图”说明。"""
        action = str(intent.get("action", "")).strip().lower()
        items = intent.get("items", []) or []
        market = str(intent.get("market", "")).strip().upper()

        action_name_map = {
            "help": "查看帮助",
            "status": "查看系统状态",
            "market": "执行大盘复盘",
            "batch": "批量分析",
            "history": "查看历史报告",
            "watchlist_list": "查看自选股列表",
            "watchlist_add": "添加自选股",
            "analyze": "分析股票",
            "reanalyze": "重新分析股票",
            "position_list": "查看持仓",
            "position_set": "设置持仓",
            "position_remove": "删除持仓",
            "none": "未识别",
        }
        action_name = action_name_map.get(action, action or "未知")

        detail = []
        if items:
            detail.append(f"目标: {', '.join([str(x) for x in items[:8]])}")
        if market:
            detail.append(f"市场: {market}")
        if action == "position_set":
            if intent.get("avg_cost") is not None:
                detail.append(f"成本: {intent.get('avg_cost')}")
            if intent.get("weight_pct") is not None:
                detail.append(f"仓位: {intent.get('weight_pct')}%")
        detail_text = f"\n• {'; '.join(detail)}" if detail else ""

        prefix = (
            "🤖 **智能识别结果**\n"
            f"• 意图: {action_name}"
            f"{detail_text}\n\n"
        )

        return BotResponse(
            text=prefix + (response.text or ""),
            markdown=True,
            at_user=response.at_user,
            reply_to_message=response.reply_to_message,
            extra=response.extra,
        )
    
    def set_help_command_getter(self, getter: Callable) -> None:
        """
        设置帮助命令的命令列表获取器
        
        用于让 HelpCommand 获取命令列表。
        
        Args:
            getter: 回调函数，返回命令列表
        """
        self._help_command_getter = getter


# 全局分发器实例
_dispatcher: Optional[CommandDispatcher] = None


def get_dispatcher() -> CommandDispatcher:
    """
    获取全局分发器实例
    
    使用单例模式，首次调用时自动初始化并注册所有命令。
    """
    global _dispatcher
    
    if _dispatcher is None:
        from src.config import get_config
        
        config = get_config()
        
        # 创建分发器
        _dispatcher = CommandDispatcher(
            command_prefix=getattr(config, 'bot_command_prefix', '/'),
            rate_limit_requests=getattr(config, 'bot_rate_limit_requests', 10),
            rate_limit_window=getattr(config, 'bot_rate_limit_window', 60),
            admin_users=getattr(config, 'bot_admin_users', []),
        )
        
        # 自动注册所有命令
        from bot.commands import ALL_COMMANDS
        for command_class in ALL_COMMANDS:
            _dispatcher.register_class(command_class)
        
        logger.info(f"[Dispatcher] 初始化完成，已注册 {len(_dispatcher._commands)} 个命令")
    
    return _dispatcher


def reset_dispatcher() -> None:
    """重置全局分发器（主要用于测试）"""
    global _dispatcher
    _dispatcher = None
