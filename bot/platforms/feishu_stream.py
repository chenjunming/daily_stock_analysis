# -*- coding: utf-8 -*-
"""
===================================
飞书 Stream 模式适配器
===================================

使用飞书官方 lark-oapi SDK 的 WebSocket 长连接模式接入机器人，
无需公网 IP 和 Webhook 配置。

优势：
- 不需要公网 IP 或域名
- 不需要配置 Webhook URL
- 通过 WebSocket 长连接接收消息
- 更简单的接入方式
- 内置自动重连和心跳保活

依赖：
pip install lark-oapi

飞书长连接文档：
https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/handle-events
"""

import json
import logging
import threading
import time
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable, Any, Dict

logger = logging.getLogger(__name__)

# 尝试导入飞书 SDK
try:
    import lark_oapi as lark
    from lark_oapi import ws
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        UpdateMessageRequest,
        UpdateMessageRequestBody,
        PatchMessageRequest,
        PatchMessageRequestBody,
    )
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
        CallBackToast,
    )

    FEISHU_SDK_AVAILABLE = True
except ImportError:
    FEISHU_SDK_AVAILABLE = False
    logger.warning("[Feishu Stream] lark-oapi SDK 未安装，Stream 模式不可用")
    logger.warning("[Feishu Stream] 请运行: pip install lark-oapi")

from bot.models import BotMessage, BotResponse, ChatType
from src.formatters import format_feishu_markdown, chunk_feishu_content, build_feishu_card_from_markdown, infer_report_title
from src.config import get_config


class FeishuReplyClient:
    """
    飞书消息回复客户端
    
    使用飞书 API 发送回复消息。
    """

    def __init__(self, app_id: str, app_secret: str):
        """
        Args:
            app_id: 飞书应用 ID
            app_secret: 飞书应用密钥
        """
        if not FEISHU_SDK_AVAILABLE:
            raise ImportError("lark-oapi SDK 未安装")

        self._client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.WARNING) \
            .build()

        # 获取配置的最大字节数
        config = get_config()
        self._max_bytes = getattr(config, 'feishu_max_bytes', 20000)
        # 飞书交互卡片正文建议保持在较保守长度，避免 field validation failed
        self._card_md_max_chars = int(getattr(config, "feishu_card_md_max_chars", 12000) or 12000)
        # 本地消息正文缓存：用于“引用回复”场景快速恢复上下文（避免远端取不到卡片正文）
        self._message_text_cache: Dict[str, str] = {}
        self._message_text_cache_lock = threading.Lock()
        self._message_cache_file = Path(
            getattr(config, "feishu_message_cache_file", "data/feishu_message_cache.jsonl")
        )
        self._message_cache_max_entries = int(getattr(config, "feishu_message_cache_max_entries", 100) or 100)
        self._load_message_text_cache_from_disk()

    def _load_message_text_cache_from_disk(self) -> None:
        try:
            if not self._message_cache_file.exists():
                return
            loaded = 0
            with self._message_cache_file.open("r", encoding="utf-8") as f:
                lines = f.readlines()[-self._message_cache_max_entries:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    mid = str(obj.get("message_id", "") or "").strip()
                    txt = str(obj.get("text", "") or "").strip()
                    if mid and txt:
                        self._message_text_cache[mid] = txt
                        loaded += 1
                except Exception:
                    continue
            if loaded > 0:
                logger.info(f"[Feishu Stream] 已加载本地消息缓存: {loaded} 条")
            # 启动时顺带收敛落盘文件，确保只保留最近 N 条
            self._truncate_message_cache_file()
        except Exception as e:
            logger.debug(f"[Feishu Stream] 加载本地消息缓存失败: {e}")

    def _cache_message_text(self, message_id: Optional[str], content: Any) -> None:
        mid = (message_id or "").strip()
        if not mid:
            return
        text = self._content_to_plain_text(content)
        if not text:
            return
        if len(text) > 12000:
            text = text[:12000]
        with self._message_text_cache_lock:
            self._message_text_cache[mid] = text
            if len(self._message_text_cache) > self._message_cache_max_entries:
                # 按插入顺序淘汰最老条目
                oldest_key = next(iter(self._message_text_cache.keys()))
                if oldest_key != mid:
                    self._message_text_cache.pop(oldest_key, None)
        # 追加落盘，支持重启后引用恢复
        try:
            self._message_cache_file.parent.mkdir(parents=True, exist_ok=True)
            with self._message_cache_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "message_id": mid,
                    "text": text,
                    "ts": int(time.time())
                }, ensure_ascii=False) + "\n")
            self._truncate_message_cache_file()
        except Exception as e:
            logger.debug(f"[Feishu Stream] 写入本地消息缓存失败: {e}")

    def _truncate_message_cache_file(self) -> None:
        try:
            if not self._message_cache_file.exists():
                return
            with self._message_cache_file.open("r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= self._message_cache_max_entries:
                return
            keep = lines[-self._message_cache_max_entries:]
            with self._message_cache_file.open("w", encoding="utf-8") as f:
                f.writelines(keep)
        except Exception as e:
            logger.debug(f"[Feishu Stream] 截断本地消息缓存失败: {e}")

    @staticmethod
    def _content_to_plain_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return str(content).strip()
        if isinstance(content, dict):
            lines = []
            header = content.get("header")
            if isinstance(header, dict):
                title = header.get("title")
                if isinstance(title, dict):
                    t = str(title.get("content", "") or "").strip()
                    if t:
                        lines.append(t)
            elements = content.get("elements", [])
            if isinstance(elements, list):
                for el in elements:
                    if not isinstance(el, dict):
                        continue
                    text_obj = el.get("text")
                    if isinstance(text_obj, dict):
                        c = str(text_obj.get("content", "") or "").strip()
                        if c:
                            lines.append(c)
                    fields = el.get("fields")
                    if isinstance(fields, list):
                        for f in fields:
                            if isinstance(f, dict):
                                t = f.get("text")
                                if isinstance(t, dict):
                                    c = str(t.get("content", "") or "").strip()
                                    if c:
                                        lines.append(c)
            return "\n".join(lines).strip()
        return str(content).strip()

    def _send_interactive_card(self, content: str, message_id: Optional[str] = None,
                               chat_id: Optional[str] = None,
                               receive_id_type: str = "chat_id",
                               at_user: bool = False, user_id: Optional[str] = None) -> bool:
        """
        发送交互卡片消息（支持 Markdown 渲染）
        
        Args:
            content: Markdown 格式的内容
            message_id: 原消息 ID（回复时使用）
            chat_id: 会话 ID（主动发送时使用）
            receive_id_type: 接收者 ID 类型
            at_user: 是否 @用户
            user_id: 用户 open_id（at_user=True 时需要）
            
        Returns:
            是否发送成功
        """
        try:
            # 如果需要 @用户，在内容前添加 @ 标记
            content_json = self._build_card_content_json(content, at_user=at_user, user_id=user_id)

            if message_id:
                # 回复消息
                request = ReplyMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                    ReplyMessageRequestBody.builder()
                    .content(content_json)
                    .msg_type("interactive")
                    .build()
                ) \
                    .build()
                response = self._client.im.v1.message.reply(request)
            else:
                # 主动发送消息
                request = CreateMessageRequest.builder() \
                    .receive_id_type(receive_id_type) \
                    .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content_json)
                    .msg_type("interactive")
                    .build()
                ) \
                    .build()
                response = self._client.im.v1.message.create(request)

            if not response.success():
                logger.error(
                    f"[Feishu Stream] 发送交互卡片失败: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                return False

            msg_id = self._extract_message_id_from_response(response)
            self._cache_message_text(msg_id, content)
            logger.debug(f"[Feishu Stream] 发送交互卡片成功")
            return True

        except Exception as e:
            logger.error(f"[Feishu Stream] 发送交互卡片异常: {e}")
            return False

    def _build_card_content_json(self, content: Any, at_user: bool = False, user_id: Optional[str] = None) -> str:
        """
        支持两种输入：
        1) str: 自动包装为 lark_md 单段卡片
        2) dict: 直接作为完整卡片 JSON 发送
        """
        def _safe_md(text: Any) -> str:
            # 不做主动截断，由发送分片策略处理超长内容
            return str(text or "")

        def _get_elements_container(card_data: Dict[str, Any]) -> Optional[list]:
            # JSON 2.0: body.elements；JSON 1.0: elements
            body = card_data.get("body")
            if isinstance(body, dict) and isinstance(body.get("elements"), list):
                return body.get("elements")
            if isinstance(card_data.get("elements"), list):
                return card_data.get("elements")
            return None

        if isinstance(content, dict):
            card_data = dict(content)
            # 结构纠偏：actions 过多时按每组 5 个拆分，避免 field validation failed
            elements = _get_elements_container(card_data)
            if isinstance(elements, list):
                normalized_elements = []
                for el in elements:
                    if not isinstance(el, dict):
                        continue
                    if el.get("tag") == "action" and isinstance(el.get("actions"), list):
                        actions = el.get("actions") or []
                        if len(actions) <= 5:
                            normalized_elements.append(el)
                        else:
                            for i in range(0, len(actions), 5):
                                normalized_elements.append({
                                    "tag": "action",
                                    "actions": actions[i:i + 5],
                                })
                        continue
                    # 对 lark_md 内容做长度保护
                    # JSON 1.0: div.text.lark_md
                    text_obj = el.get("text")
                    if isinstance(text_obj, dict) and text_obj.get("tag") == "lark_md":
                        text_obj["content"] = _safe_md(text_obj.get("content"))
                    # JSON 2.0: markdown.content
                    if el.get("tag") == "markdown":
                        el["content"] = _safe_md(el.get("content"))
                    normalized_elements.append(el)
                if isinstance(card_data.get("body"), dict):
                    card_data["body"]["elements"] = normalized_elements
                else:
                    card_data["elements"] = normalized_elements
            if at_user and user_id:
                elements = _get_elements_container(card_data)
                mention_content = f"<at id={user_id}></at>"
                if isinstance(card_data.get("body"), dict):
                    mention_el = {
                        "tag": "markdown",
                        "content": mention_content,
                        "text_align": "left",
                        "text_size": "normal",
                    }
                else:
                    mention_el = {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"<at user_id=\"{user_id}\"></at>"
                        }
                    }
                if isinstance(elements, list):
                    elements.insert(0, mention_el)
                else:
                    if isinstance(card_data.get("body"), dict):
                        card_data["body"]["elements"] = [mention_el]
                    else:
                        card_data["elements"] = [mention_el]
            return json.dumps(card_data, ensure_ascii=False)

        final_content = _safe_md(str(content or ""))
        card_data = build_feishu_card_from_markdown(
            final_content,
            title=infer_report_title(final_content, default="A股智能分析报告")
        )
        if at_user and user_id:
            body = card_data.get("body")
            elements = body.get("elements") if isinstance(body, dict) else card_data.get("elements")
            mention_el = (
                {
                    "tag": "markdown",
                    "content": f"<at id={user_id}></at>",
                    "text_align": "left",
                    "text_size": "normal",
                }
                if isinstance(body, dict)
                else {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<at user_id=\"{user_id}\"></at>"
                    }
                }
            )
            if isinstance(elements, list):
                elements.insert(0, mention_el)
            else:
                if isinstance(body, dict):
                    body["elements"] = [mention_el]
                else:
                    card_data["elements"] = [mention_el]
        return json.dumps(card_data, ensure_ascii=False)

    @staticmethod
    def _extract_message_id_from_response(response) -> Optional[str]:
        """
        从飞书 SDK 响应对象中提取 message_id。
        兼容不同 SDK 版本的结构差异。
        """
        if response is None:
            return None
        # 常见路径：response.data.message_id
        data = getattr(response, "data", None)
        if data is not None:
            msg_id = getattr(data, "message_id", None)
            if msg_id:
                return msg_id
        # 兜底：直接查 response.message_id
        msg_id = getattr(response, "message_id", None)
        if msg_id:
            return msg_id
        return None

    def create_card_to_chat(
        self,
        chat_id: str,
        content: Any,
        receive_id_type: str = "chat_id",
        at_user: bool = False,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        在会话中创建一条交互卡片，返回 message_id。
        """
        try:
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .content(self._build_card_content_json(content, at_user=at_user, user_id=user_id))
                .msg_type("interactive")
                .build()
            ) \
                .build()
            response = self._client.im.v1.message.create(request)
            if not response.success():
                logger.error(
                    f"[Feishu Stream] 创建流式卡片失败: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
                )
                return None
            msg_id = self._extract_message_id_from_response(response)
            self._cache_message_text(msg_id, content)
            return msg_id
        except Exception as e:
            logger.error(f"[Feishu Stream] 创建流式卡片异常: {e}")
            return None

    def update_card(self, message_id: str, content: Any, use_patch: bool = False) -> bool:
        """
        更新已发送卡片内容。
        """
        try:
            content_json = self._build_card_content_json(content)
            payload_len = len(content_json.encode("utf-8"))

            def _do_patch() -> bool:
                patch_request = PatchMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                    PatchMessageRequestBody.builder()
                    .content(content_json)
                    .build()
                ) \
                    .build()
                patch_response = self._client.im.v1.message.patch(patch_request)
                if not patch_response.success():
                    logger.error(
                        f"[Feishu Stream] Patch 更新卡片失败: code={patch_response.code}, msg={patch_response.msg}, payload={payload_len}B, log_id={patch_response.get_log_id()}"
                    )
                    return False
                return True

            if use_patch:
                return _do_patch()

            # Update 接口不需要 msg_type，传入会触发 invalid msg_type
            request = UpdateMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                UpdateMessageRequestBody.builder()
                .content(content_json)
                .build()
            ) \
                .build()
            response = self._client.im.v1.message.update(request)

            if not response.success():
                log_msg = (
                    f"[Feishu Stream] Update 更新卡片失败，回退 Patch: "
                    f"code={response.code}, msg={response.msg}, payload={payload_len}B, log_id={response.get_log_id()}"
                )
                # 已知可回退错误（如字段校验失败）不按告警级别刷屏
                if str(response.code) == "99992402":
                    logger.info(log_msg)
                else:
                    logger.warning(log_msg)
                if _do_patch():
                    self._cache_message_text(message_id, content)
                    return True
                # 兜底：再尝试最简卡片，避免整次流式会话失效
                fallback_text = self._extract_fallback_text(content)
                fallback_json = self._build_card_content_json(fallback_text)
                fallback_len = len(fallback_json.encode("utf-8"))
                logger.warning(f"[Feishu Stream] 尝试最简卡片降级更新，payload={fallback_len}B")
                patch_request = PatchMessageRequest.builder() \
                    .message_id(message_id) \
                    .request_body(
                    PatchMessageRequestBody.builder()
                    .content(fallback_json)
                    .build()
                ) \
                    .build()
                patch_response = self._client.im.v1.message.patch(patch_request)
                if not patch_response.success():
                    logger.error(
                        f"[Feishu Stream] 最简卡片降级仍失败: code={patch_response.code}, msg={patch_response.msg}, log_id={patch_response.get_log_id()}"
                    )
                    return False
                self._cache_message_text(message_id, fallback_text)
                return True
            self._cache_message_text(message_id, content)
            return True
        except Exception as e:
            logger.error(f"[Feishu Stream] 更新卡片异常: {e}")
            return False

    @staticmethod
    def _extract_fallback_text(content: Any) -> str:
        """从复杂卡片提取简化文案，作为更新失败时兜底。"""
        if isinstance(content, str):
            return content[:1200]
        if isinstance(content, dict):
            lines = []
            header = content.get("header")
            if isinstance(header, dict):
                title = (header.get("title") or {}).get("content") if isinstance(header.get("title"), dict) else ""
                if title:
                    lines.append(f"## {title}")
            for el in content.get("elements", [])[:8]:
                if not isinstance(el, dict):
                    continue
                text_obj = el.get("text")
                if isinstance(text_obj, dict):
                    txt = text_obj.get("content")
                    if txt:
                        lines.append(str(txt))
            text = "\n\n".join(lines).strip()
            return text[:1200] if text else "卡片更新中..."
        return "卡片更新中..."

    def reply_card(self, message_id: str, content: Any, at_user: bool = False, user_id: Optional[str] = None) -> Optional[str]:
        """
        回复一条交互卡片并返回新消息的 message_id。
        """
        try:
            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(
                ReplyMessageRequestBody.builder()
                .content(self._build_card_content_json(content, at_user=at_user, user_id=user_id))
                .msg_type("interactive")
                .build()
            ) \
                .build()
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                logger.error(
                    f"[Feishu Stream] 回复流式卡片失败: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
                )
                return None
            msg_id = self._extract_message_id_from_response(response)
            self._cache_message_text(msg_id, content)
            return msg_id
        except Exception as e:
            logger.error(f"[Feishu Stream] 回复流式卡片异常: {e}")
            return None

    def reply_text(self, message_id: str, text: str, at_user: bool = False,
                   user_id: Optional[str] = None) -> bool:
        """
        回复文本消息（支持交互卡片和分段发送）
        
        Args:
            message_id: 原消息 ID
            text: 回复文本
            at_user: 是否 @用户
            user_id: 用户 open_id（at_user=True 时需要）
            
        Returns:
            是否发送成功
        """
        # 将文本转换为飞书 Markdown 格式
        formatted_text = format_feishu_markdown(text)

        # 检查是否需要分段发送
        content_bytes = len(formatted_text.encode('utf-8'))
        if content_bytes > self._max_bytes:
            logger.info(
                f"[Feishu Stream] 回复消息内容超长({content_bytes}字节)，将分批发送"
            )
            return chunk_feishu_content(
                formatted_text,
                self._max_bytes,
                lambda chunk: self._send_interactive_card(
                    chunk, message_id=message_id, at_user=at_user, user_id=user_id
                )
            )

        # 单条消息，使用交互卡片
        return self._send_interactive_card(
            formatted_text, message_id=message_id, at_user=at_user, user_id=user_id
        )

    def send_to_chat(self, chat_id: str, text: str,
                     receive_id_type: str = "chat_id") -> bool:
        """
        发送消息到指定会话（支持交互卡片和分段发送）
        
        Args:
            chat_id: 会话 ID
            text: 消息文本
            receive_id_type: 接收者 ID 类型，默认 chat_id
            
        Returns:
            是否发送成功
        """
        # 将文本转换为飞书 Markdown 格式
        formatted_text = format_feishu_markdown(text)

        # 检查是否需要分段发送
        content_bytes = len(formatted_text.encode('utf-8'))
        if content_bytes > self._max_bytes:
            logger.info(
                f"[Feishu Stream] 发送消息内容超长({content_bytes}字节)，将分批发送"
            )
            return chunk_feishu_content(
                formatted_text,
                self._max_bytes,
                lambda chunk: self._send_interactive_card(
                    chunk, chat_id=chat_id, receive_id_type=receive_id_type
                )
            )

        # 单条消息，使用交互卡片
        return self._send_interactive_card(
            formatted_text, chat_id=chat_id, receive_id_type=receive_id_type
        )

    def get_message_text(self, message_id: str) -> str:
        """
        拉取飞书消息正文（用于引用回复时补全上下文）。
        返回解析后的 text 字段，失败返回空字符串。
        """
        if not message_id:
            return ""
        with self._message_text_cache_lock:
            cached = (self._message_text_cache.get(message_id) or "").strip()
        if cached:
            logger.info(f"[Feishu Stream] 引用正文命中本地缓存: id={message_id}, len={len(cached)}")
            return cached
        # 跨实例兜底：发送端和接收端可能是不同客户端实例，先从落盘缓存重载再查一次
        self._load_message_text_cache_from_disk()
        with self._message_text_cache_lock:
            cached = (self._message_text_cache.get(message_id) or "").strip()
        if cached:
            logger.info(f"[Feishu Stream] 引用正文命中落盘缓存: id={message_id}, len={len(cached)}")
            return cached
        try:
            # 延迟导入，避免 SDK 版本差异在模块导入阶段直接失败
            from lark_oapi.api.im.v1 import GetMessageRequest
            req = GetMessageRequest.builder().message_id(message_id).build()
            resp = self._client.im.v1.message.get(req)
            if not resp.success():
                logger.debug(
                    f"[Feishu Stream] 获取引用消息失败: id={message_id}, code={resp.code}, msg={resp.msg}"
                )
                return ""
            data = getattr(resp, "data", None)
            item = getattr(data, "message", None) if data is not None else None
            content = getattr(item, "content", "") if item is not None else ""
            msg_type = str(getattr(item, "message_type", "") or getattr(item, "msg_type", "") or "").lower()
            if not content:
                return ""
            try:
                payload = json.loads(content)
                # 普通文本消息
                text = str(payload.get("text", "") or "").strip()
                if text:
                    return text
                # 交互卡片消息：提取 elements 中的 lark_md/plain_text 文本
                # 典型结构：{"elements":[{"tag":"div","text":{"tag":"lark_md","content":"..."}}, ...]}
                elements = payload.get("elements", [])
                if isinstance(elements, list):
                    lines = []
                    for el in elements:
                        if not isinstance(el, dict):
                            continue
                        text_obj = el.get("text")
                        if isinstance(text_obj, dict):
                            c = str(text_obj.get("content", "") or "").strip()
                            if c:
                                lines.append(c)
                        # 兼容 note/actions 内嵌文本
                        fields = el.get("fields")
                        if isinstance(fields, list):
                            for f in fields:
                                if isinstance(f, dict):
                                    t = f.get("text")
                                    if isinstance(t, dict):
                                        c = str(t.get("content", "") or "").strip()
                                        if c:
                                            lines.append(c)
                    if lines:
                        return "\n".join(lines)
                logger.info(
                    f"[Feishu Stream] 引用消息解析为空: id={message_id}, type={msg_type}, keys={list(payload.keys())[:8]}"
                )
                return ""
            except Exception:
                return str(content)
        except Exception as e:
            logger.debug(f"[Feishu Stream] 拉取引用消息异常: {e}")
            return ""


class FeishuCardStreamSession:
    """
    飞书单卡片流式会话：创建一次，后续持续 update 同一条消息。
    """

    def __init__(
        self,
        reply_client: FeishuReplyClient,
        chat_id: str,
        update_interval: float = 2.0,
        max_updates: int = 120,
        at_user: bool = False,
        user_id: Optional[str] = None,
    ):
        self.reply_client = reply_client
        self.chat_id = chat_id
        self.update_interval = max(0.2, float(update_interval))
        self.max_updates = max(1, int(max_updates))
        self.at_user = at_user
        self.user_id = user_id
        self.message_id: Optional[str] = None
        self.last_update_ts: float = 0.0
        self.version: int = 0

    def start(self, initial_content: Any) -> bool:
        message_id = self.reply_client.create_card_to_chat(
            chat_id=self.chat_id,
            content=initial_content,
            at_user=self.at_user,
            user_id=self.user_id,
        )
        if not message_id:
            return False
        self.message_id = message_id
        self.version = 1
        self.last_update_ts = time.time()
        return True

    def update(self, content: Any, force: bool = False) -> bool:
        if not self.message_id:
            return False
        if self.version >= self.max_updates:
            return False
        now = time.time()
        if not force and now - self.last_update_ts < self.update_interval:
            return False
        ok = self.reply_client.update_card(self.message_id, content)
        if ok:
            self.version += 1
            self.last_update_ts = now
        return ok

    def finish(self, final_content: Any) -> bool:
        # 终态更新强制刷新
        return self.update(final_content, force=True)


class FeishuStreamHandler:
    """
    飞书 Stream 模式消息处理器
    
    将 SDK 的事件转换为统一的 BotMessage 格式，
    并调用命令分发器处理。
    """

    def __init__(
            self,
            on_message: Callable[[BotMessage], BotResponse],
            reply_client: FeishuReplyClient
    ):
        """
        Args:
            on_message: 消息处理回调函数，接收 BotMessage 返回 BotResponse
            reply_client: 飞书回复客户端
        """
        self._on_message = on_message
        self._reply_client = reply_client
        self._logger = logger
        # 简单去重缓存：避免飞书重复投递或重连导致同一消息被处理多次
        self._processed_message_ids: dict[str, float] = {}
        self._processed_lock = threading.Lock()
        self._dedup_ttl_seconds = 180
        # 次级去重：同会话同用户同内容在短时间内重复投递时拦截
        self._processed_content_keys: dict[str, float] = {}
        self._content_dedup_ttl_seconds = 60
        # 拒绝历史积压消息（默认只处理最近 120 秒）
        config = get_config()
        self._max_message_age_seconds = int(getattr(config, "feishu_message_max_age_seconds", 120) or 120)

    @staticmethod
    def _truncate_log_content(text: str, max_len: int = 200) -> str:
        """截断日志内容"""
        cleaned = text.replace("\n", " ").strip()
        if len(cleaned) > max_len:
            return f"{cleaned[:max_len]}..."
        return cleaned

    def _log_incoming_message(self, message: BotMessage) -> None:
        """记录收到的消息日志"""
        content = message.raw_content or message.content or ""
        summary = self._truncate_log_content(content)
        self._logger.info(
            "[Feishu Stream] Incoming message: msg_id=%s user_id=%s "
            "chat_id=%s chat_type=%s content=%s",
            message.message_id,
            message.user_id,
            message.chat_id,
            getattr(message.chat_type, "value", message.chat_type),
            summary,
        )

    def handle_message(self, event: 'P2ImMessageReceiveV1') -> None:
        """
        处理接收到的消息事件
        
        Args:
            event: 飞书消息接收事件
        """
        try:
            # 解析消息
            bot_message = self._parse_event_message(event)

            if bot_message is None:
                return

            if self._is_stale_message(bot_message):
                self._logger.info(
                    "[Feishu Stream] 忽略历史消息: msg_id=%s age=%ss",
                    bot_message.message_id,
                    int((datetime.now() - bot_message.timestamp).total_seconds()) if bot_message.timestamp else -1,
                )
                return

            if self._is_duplicate_message(bot_message):
                self._logger.info(
                    "[Feishu Stream] 忽略重复消息: msg_id=%s",
                    bot_message.message_id
                )
                return

            self._log_incoming_message(bot_message)

            # 调用消息处理回调
            response = self._on_message(bot_message)

            # 发送回复
            if response and response.text:
                self._reply_client.reply_text(
                    message_id=bot_message.message_id,
                    text=response.text,
                    at_user=response.at_user,
                    user_id=bot_message.user_id if response.at_user else None
                )

        except Exception as e:
            self._logger.error(f"[Feishu Stream] 处理消息失败: {e}")
            self._logger.exception(e)

    def handle_card_action(self, event: 'P2CardActionTrigger') -> Dict[str, Any]:
        """
        处理交互卡片按钮点击事件。
        支持：批量分析完成卡片 -> 查看单股详情
        """
        try:
            self._logger.info("[Feishu Stream] 收到卡片交互回调")
            value = {}
            chat_id = ""
            action = getattr(getattr(event, "event", None), "action", None)
            if action is not None:
                value = getattr(action, "value", None) or {}
            context = getattr(getattr(event, "event", None), "context", None)
            if context is not None:
                chat_id = getattr(context, "open_chat_id", "") or ""

            action_type = str(value.get("action", "")).strip().lower()
            self._logger.info(
                "[Feishu Stream] 卡片回调 action=%s chat_id=%s value=%s",
                action_type,
                chat_id,
                value,
            )
            code = str(value.get("code", "")).strip().upper()
            # 放宽兼容：即使 action 字段为空，只要携带 code 也按查看详情处理
            if (action_type == "show_stock_detail" or (not action_type and code)) and code and chat_id:
                # 卡片回调需要尽快返回，避免飞书侧超时报错（如 code 200340）
                threading.Thread(
                    target=self._send_stock_detail_async,
                    args=(chat_id, code),
                    daemon=True,
                    name=f"feishu-card-detail-{code}",
                ).start()
                return {
                    "toast": {"type": "success", "content": f"已收到，正在发送 {code} 详细报告"}
                }
            if code and not chat_id:
                return {
                    "toast": {"type": "warning", "content": "缺少会话ID，无法发送详情"}
                }

            return {
                "toast": {"type": "info", "content": "已收到操作"}
            }
        except Exception as e:
            self._logger.error(f"[Feishu Stream] 处理卡片交互失败: {e}")
            self._logger.exception(e)
            # 始终返回可序列化结构，避免飞书侧 200340
            return {
                "toast": {"type": "error", "content": "处理交互失败"}
            }

    def _build_stock_detail_report(self, code: str) -> str:
        """
        从分析历史中取最新一条，生成单股详细报告文本。
        """
        try:
            from src.storage import get_db
            from src.analyzer import AnalysisResult
            from src.notification import NotificationService

            db = get_db()
            history = db.get_analysis_history(code=code, days=365, limit=1)
            if not history:
                return f"⚠️ 未找到 `{code}` 的历史分析报告，请先执行分析。"

            record = history[0]
            raw_result = {}
            try:
                raw_result = json.loads(record.raw_result or "{}")
            except Exception:
                raw_result = {}

            result = AnalysisResult(
                code=(raw_result.get("code") or record.code or code),
                name=(raw_result.get("name") or record.name or code),
                sentiment_score=int(raw_result.get("sentiment_score") or record.sentiment_score or 50),
                trend_prediction=(raw_result.get("trend_prediction") or record.trend_prediction or "震荡"),
                operation_advice=(raw_result.get("operation_advice") or record.operation_advice or "观望"),
                analysis_summary=(raw_result.get("analysis_summary") or record.analysis_summary or ""),
                dashboard=raw_result.get("dashboard"),
                decision_type=(raw_result.get("decision_type") or "hold"),
                confidence_level=(raw_result.get("confidence_level") or "中"),
            )
            notifier = NotificationService()
            return notifier.generate_dashboard_report([result])
        except Exception as e:
            self._logger.error(f"[Feishu Stream] 构建单股详情失败 code={code}: {e}")
            return f"❌ 获取 `{code}` 详细报告失败: {str(e)[:120]}"

    def _send_stock_detail_async(self, chat_id: str, code: str) -> None:
        """后台发送个股详情，避免阻塞卡片回调。"""
        try:
            detail = self._build_stock_detail_report(code)
            self._reply_client.send_to_chat(chat_id=chat_id, text=detail)
        except Exception as e:
            self._logger.error(f"[Feishu Stream] 异步发送个股详情失败 code={code}: {e}")

    def _is_duplicate_message(self, message: BotMessage) -> bool:
        """基于 message_id + 内容指纹的去重检查。"""
        message_id = message.message_id
        if not message_id and not (message.chat_id and message.user_id and message.content):
            return False
        now = time.time()
        with self._processed_lock:
            # 清理过期缓存
            expired = [
                msg_id for msg_id, ts in self._processed_message_ids.items()
                if now - ts > self._dedup_ttl_seconds
            ]
            for msg_id in expired:
                self._processed_message_ids.pop(msg_id, None)

            expired_content = [
                k for k, ts in self._processed_content_keys.items()
                if now - ts > self._content_dedup_ttl_seconds
            ]
            for k in expired_content:
                self._processed_content_keys.pop(k, None)

            if message_id and message_id in self._processed_message_ids:
                return True

            content_key = self._content_key(message)
            if content_key and content_key in self._processed_content_keys:
                return True

            if message_id:
                self._processed_message_ids[message_id] = now
            if content_key:
                self._processed_content_keys[content_key] = now
            return False

    @staticmethod
    def _content_key(message: BotMessage) -> str:
        """生成内容去重键。"""
        base = f"{message.chat_id}|{message.user_id}|{(message.content or '').strip()}"
        if not base.strip("|"):
            return ""
        return hashlib.md5(base.encode("utf-8")).hexdigest()

    def _is_stale_message(self, message: BotMessage) -> bool:
        """是否为历史积压消息。"""
        ts = message.timestamp
        if not ts:
            return False
        age = (datetime.now() - ts).total_seconds()
        if age < 0:
            return False
        return age > self._max_message_age_seconds

    def _parse_event_message(self, event: 'P2ImMessageReceiveV1') -> Optional[BotMessage]:
        """
        解析飞书事件消息为统一格式
        
        Args:
            event: P2ImMessageReceiveV1 事件对象
        """
        try:
            event_data = event.event
            if event_data is None:
                return None

            message_data = event_data.message
            sender_data = event_data.sender

            if message_data is None:
                return None

            # 只处理文本消息
            message_type = message_data.message_type or ""
            if message_type != "text":
                self._logger.debug(f"[Feishu Stream] 忽略非文本消息: {message_type}")
                return None

            # 解析消息内容
            content_str = message_data.content or "{}"
            try:
                content_json = json.loads(content_str)
                raw_content = content_json.get("text", "")
            except json.JSONDecodeError:
                raw_content = content_str

            # 提取命令（去除 @机器人）
            content = self._extract_command(raw_content, message_data.mentions)
            mentioned = "@" in raw_content or bool(message_data.mentions)

            # 获取发送者信息
            user_id = ""
            if sender_data and sender_data.sender_id:
                user_id = sender_data.sender_id.open_id or sender_data.sender_id.user_id or ""

            # 获取会话类型
            chat_type_str = message_data.chat_type or ""
            if chat_type_str == "group":
                chat_type = ChatType.GROUP
            elif chat_type_str == "p2p":
                chat_type = ChatType.PRIVATE
            else:
                chat_type = ChatType.UNKNOWN

            # 创建时间
            create_time = message_data.create_time
            try:
                if create_time:
                    timestamp = datetime.fromtimestamp(int(create_time) / 1000)
                else:
                    timestamp = datetime.now()
            except (ValueError, TypeError):
                timestamp = datetime.now()

            # 构建原始数据
            parent_id = getattr(message_data, "parent_id", "") or ""
            root_id = getattr(message_data, "root_id", "") or ""
            thread_id = getattr(message_data, "thread_id", "") or ""
            referenced_message_id = parent_id or root_id or ""
            referenced_text = ""
            if referenced_message_id:
                referenced_text = self._reply_client.get_message_text(referenced_message_id)
            if referenced_message_id:
                self._logger.info(
                    "[Feishu Stream] 引用上下文: parent/root=%s text_len=%s",
                    referenced_message_id,
                    len(referenced_text or ""),
                )
            raw_data = {
                "header": {
                    "event_id": event.header.event_id if event.header else "",
                    "event_type": event.header.event_type if event.header else "",
                    "create_time": event.header.create_time if event.header else "",
                    "token": event.header.token if event.header else "",
                    "app_id": event.header.app_id if event.header else "",
                },
                "event": {
                    "message_id": message_data.message_id,
                    "chat_id": message_data.chat_id,
                    "chat_type": message_data.chat_type,
                    "content": message_data.content,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                    "referenced_message_id": referenced_message_id,
                    "referenced_text": referenced_text,
                }
            }

            return BotMessage(
                platform="feishu",
                message_id=message_data.message_id or "",
                user_id=user_id,
                user_name=user_id,  # 飞书不直接返回用户名
                chat_id=message_data.chat_id or "",
                chat_type=chat_type,
                content=content,
                raw_content=raw_content,
                mentioned=mentioned,
                mentions=[m.key or "" for m in (message_data.mentions or [])],
                timestamp=timestamp,
                raw_data=raw_data,
            )

        except Exception as e:
            self._logger.error(f"[Feishu Stream] 解析消息失败: {e}")
            return None

    def _extract_command(self, text: str, mentions: list) -> str:
        """
        提取命令内容（去除 @机器人）
        
        飞书的 @用户 格式是：@_user_1, @_user_2 等
        
        Args:
            text: 原始消息文本
            mentions: @提及列表
        """
        import re

        # 方式1: 通过 mentions 列表移除（精确匹配）
        for mention in (mentions or []):
            key = getattr(mention, 'key', '') or ''
            if key:
                text = text.replace(key, '')

        # 方式2: 正则兜底，移除飞书 @用户 格式（@_user_N）
        # 当 mentions 为空或未正确传递时生效
        text = re.sub(r'@_user_\d+\s*', '', text)

        # 清理多余空格
        return ' '.join(text.split())


class FeishuStreamClient:
    """
    飞书 Stream 模式客户端
    
    封装 lark-oapi SDK 的 WebSocket 客户端，提供简单的启动接口。
    
    使用方式：
        client = FeishuStreamClient()
        client.start()  # 阻塞运行
        
        # 或者在后台运行
        client.start_background()
    """

    def __init__(
            self,
            app_id: Optional[str] = None,
            app_secret: Optional[str] = None
    ):
        """
        Args:
            app_id: 应用 ID（不传则从配置读取）
            app_secret: 应用密钥（不传则从配置读取）
        """
        if not FEISHU_SDK_AVAILABLE:
            raise ImportError(
                "lark-oapi SDK 未安装。\n"
                "请运行: pip install lark-oapi"
            )

        from src.config import get_config
        config = get_config()

        self._app_id = app_id or getattr(config, 'feishu_app_id', None)
        self._app_secret = app_secret or getattr(config, 'feishu_app_secret', None)

        if not self._app_id or not self._app_secret:
            raise ValueError(
                "飞书 Stream 模式需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET"
            )

        self._ws_client: Optional[ws.Client] = None
        self._reply_client: Optional[FeishuReplyClient] = None
        self._background_thread: Optional[threading.Thread] = None
        self._running = False

    def _create_message_handler(self) -> Callable[[BotMessage], BotResponse]:
        """创建消息处理函数"""

        def handle_message(message: BotMessage) -> BotResponse:
            from bot.dispatcher import get_dispatcher
            dispatcher = get_dispatcher()
            return dispatcher.dispatch(message)

        return handle_message

    def _create_event_handler(self) -> 'lark.EventDispatcherHandler':
        """创建事件分发处理器"""
        # 创建回复客户端
        self._reply_client = FeishuReplyClient(self._app_id, self._app_secret)

        # 创建消息处理器
        handler = FeishuStreamHandler(
            self._create_message_handler(),
            self._reply_client
        )

        # 创建并注册事件处理器
        # 注意：encrypt_key 和 verification_token 在长连接模式下不是必需的
        # 但 SDK 要求传入（可以为空字符串）
        from src.config import get_config
        config = get_config()

        encrypt_key = getattr(config, 'feishu_encrypt_key', '') or ''
        verification_token = getattr(config, 'feishu_verification_token', '') or ''

        event_handler = lark.EventDispatcherHandler.builder(
            encrypt_key=encrypt_key,
            verification_token=verification_token,
            level=lark.LogLevel.WARNING
        ).register_p2_im_message_receive_v1(
            handler.handle_message
        ).register_p2_card_action_trigger(
            handler.handle_card_action
        ).build()

        return event_handler

    def start(self) -> None:
        """
        启动 Stream 客户端（阻塞）
        
        此方法会阻塞当前线程，直到客户端停止。
        """
        logger.info("[Feishu Stream] 正在启动...")

        # 创建事件处理器
        event_handler = self._create_event_handler()

        # 创建 WebSocket 客户端
        self._ws_client = ws.Client(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.WARNING,
            auto_reconnect=True
        )

        self._running = True
        logger.info("[Feishu Stream] 客户端已启动，等待消息...")

        # 启动（阻塞）
        self._ws_client.start()

    def start_background(self) -> None:
        """
        在后台线程启动 Stream 客户端（非阻塞）
        
        适用于与其他服务（如 WebUI）同时运行的场景。
        """
        if self._background_thread and self._background_thread.is_alive():
            logger.warning("[Feishu Stream] 客户端已在运行")
            return

        self._running = True
        self._background_thread = threading.Thread(
            target=self._run_in_background,
            daemon=True,
            name="FeishuStreamClient"
        )
        self._background_thread.start()
        logger.info("[Feishu Stream] 后台客户端已启动")

    def _run_in_background(self) -> None:
        """后台运行（处理异常和重连）"""
        import time

        while self._running:
            try:
                self.start()
            except Exception as e:
                logger.error(f"[Feishu Stream] 运行异常: {e}")
                if self._running:
                    logger.info("[Feishu Stream] 5 秒后重连...")
                    time.sleep(5)

    def stop(self) -> None:
        """停止客户端"""
        self._running = False
        logger.info("[Feishu Stream] 客户端已停止")

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running


# 全局客户端实例
_stream_client: Optional[FeishuStreamClient] = None


def get_feishu_stream_client() -> Optional[FeishuStreamClient]:
    """获取全局 Stream 客户端实例"""
    global _stream_client

    if _stream_client is None and FEISHU_SDK_AVAILABLE:
        try:
            _stream_client = FeishuStreamClient()
        except (ImportError, ValueError) as e:
            logger.warning(f"[Feishu Stream] 无法创建客户端: {e}")
            return None

    return _stream_client


def start_feishu_stream_background() -> bool:
    """
    在后台启动飞书 Stream 客户端
    
    Returns:
        是否成功启动
    """
    client = get_feishu_stream_client()
    if client:
        client.start_background()
        return True
    return False
