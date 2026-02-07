# -*- coding: utf-8 -*-
"""
===================================
时间工具（统一 UTC+8）
===================================
"""

from datetime import datetime, timedelta, timezone, date
from typing import Optional

UTC8 = timezone(timedelta(hours=8))


def utc8_now() -> datetime:
    """返回当前 UTC+8 时间（timezone-aware）。"""
    return datetime.now(UTC8)


def utc8_today() -> date:
    """返回当前 UTC+8 日期。"""
    return utc8_now().date()


def as_utc8(dt: Optional[datetime]) -> Optional[datetime]:
    """
    将任意 datetime 解释/转换为 UTC+8。
    约定：naive datetime 按 UTC 时间解释（容器默认常见场景）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UTC8)

