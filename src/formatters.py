# -*- coding: utf-8 -*-
"""
===================================
格式化工具模块
===================================

提供各种内容格式化工具函数，用于将通用格式转换为平台特定格式。
"""

import re
import time
from typing import List, Callable, Dict, Any


def format_feishu_markdown(content: str) -> str:
    """
    将通用 Markdown 做轻量清洗，尽量保留原始结构。
    注意：不要改写表格和标题，避免破坏用户可读性。
    
    Args:
        content: 原始 Markdown 内容
        
    Returns:
        转换后的飞书 Markdown 格式内容
        
    Example:
        >>> markdown = "# 标题\\n> 引用\\n| 列1 | 列2 |"
        >>> formatted = format_feishu_markdown(markdown)
        >>> print(formatted)
        **标题**
        💬 引用
        • 列1：值1 | 列2：值2
    """
    text = str(content or "").replace("\r\n", "\n").replace("\r", "\n")

    # 统一列表符号
    text = re.sub(r'(?m)^\s*•\s+', '- ', text)

    # 合并过多空行
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def build_feishu_card_from_markdown(content: str, title: str = "A股智能分析报告") -> Dict[str, Any]:
    """
    将 Markdown 内容转换为飞书交互卡片结构。
    使用 JSON 2.0 的 markdown 组件，保留标题/表格/代码块等语法。
    """
    text = format_feishu_markdown(content)
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title}
        },
        "body": {
            "elements": [{
                "tag": "markdown",
                "content": text or "（空内容）",
                "text_align": "left",
                "text_size": "normal",
            }]
        },
    }


def infer_report_title(content: str, default: str = "A股智能分析报告") -> str:
    """
    根据正文内容推断更准确的卡片标题。
    """
    s = (content or "").upper()
    if "今日操作模板" in s or "大盘复盘" in s:
        if "美股" in s or " US" in s or "| US" in s:
            return "美股智能分析报告"
        if "港股" in s or "| HK" in s:
            return "港股智能分析报告"
        if "A股" in s or "| CN" in s:
            return "A股智能分析报告"
        return "市场复盘报告"
    return default


def _chunk_by_lines(content: str, max_bytes: int, send_func: Callable[[str], bool]) -> bool:
    """
    强制按行分割发送（无法智能分割时的 fallback）
    
    Args:
        content: 完整消息内容
        max_bytes: 单条消息最大字节数
        send_func: 发送单条消息的函数
        
    Returns:
        是否全部发送成功
    """
    chunks = []
    current_chunk = ""
    
    # 按行分割，确保不会在多字节字符中间截断
    lines = content.split('\n')
    
    for line in lines:
        test_chunk = current_chunk + ('\n' if current_chunk else '') + line
        if len(test_chunk.encode('utf-8')) > max_bytes - 100:  # 预留空间给分页标记
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk = test_chunk
    
    if current_chunk:
        chunks.append(current_chunk)
    
    total_chunks = len(chunks)
    success_count = 0
    
    for i, chunk in enumerate(chunks):
        # 添加分页标记
        page_marker = f"\n\n📄 ({i+1}/{total_chunks})" if total_chunks > 1 else ""
        
        try:
            if send_func(chunk + page_marker):
                success_count += 1
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"飞书第 {i+1}/{total_chunks} 批发送异常: {e}")
        
        # 批次间隔，避免触发频率限制
        if i < total_chunks - 1:
            time.sleep(1)
    
    return success_count == total_chunks


def chunk_feishu_content(content: str, max_bytes: int, send_func: Callable[[str], bool]) -> bool:
    """
    将超长内容分段发送到飞书
    
    智能分割策略：
    1. 优先按 "---" 分隔（股票之间的分隔线）
    2. 其次按 "### " 标题分割（每只股票的标题）
    3. 最后按行强制分割
    
    Args:
        content: 完整消息内容
        max_bytes: 单条消息最大字节数
        send_func: 发送单条消息的函数，接收内容字符串，返回是否成功
        
    Returns:
        是否全部发送成功
    """
    def get_bytes(s: str) -> int:
        """获取字符串的 UTF-8 字节数"""
        return len(s.encode('utf-8'))
    
    # 智能分割：优先按 "---" 分隔（股票之间的分隔线）
    # 如果没有分隔线，按 "### " 标题分割（每只股票的标题）
    if "\n---\n" in content:
        sections = content.split("\n---\n")
        separator = "\n---\n"
    elif "\n### " in content:
        # 按 ### 分割，但保留 ### 前缀
        parts = content.split("\n### ")
        sections = [parts[0]] + [f"### {p}" for p in parts[1:]]
        separator = "\n"
    else:
        # 无法智能分割，按行强制分割
        return _chunk_by_lines(content, max_bytes, send_func)
    
    chunks = []
    current_chunk = []
    current_bytes = 0
    separator_bytes = get_bytes(separator)
    
    for section in sections:
        section_bytes = get_bytes(section) + separator_bytes
        
        # 如果单个 section 超长，回退到按行分片（不做截断）
        if section_bytes > max_bytes:
            return _chunk_by_lines(content, max_bytes, send_func)
        
        # 检查加入后是否超长
        if current_bytes + section_bytes > max_bytes:
            # 保存当前块，开始新块
            if current_chunk:
                chunks.append(separator.join(current_chunk))
            current_chunk = [section]
            current_bytes = section_bytes
        else:
            current_chunk.append(section)
            current_bytes += section_bytes
    
    # 添加最后一块
    if current_chunk:
        chunks.append(separator.join(current_chunk))
    
    # 分批发送
    total_chunks = len(chunks)
    success_count = 0
    
    for i, chunk in enumerate(chunks):
        # 添加分页标记
        if total_chunks > 1:
            page_marker = f"\n\n📄 ({i+1}/{total_chunks})"
            chunk_with_marker = chunk + page_marker
        else:
            chunk_with_marker = chunk
        
        try:
            if send_func(chunk_with_marker):
                success_count += 1
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"飞书第 {i+1}/{total_chunks} 批发送异常: {e}")
        
        # 批次间隔，避免触发频率限制
        if i < total_chunks - 1:
            time.sleep(1)
    
    return success_count == total_chunks
