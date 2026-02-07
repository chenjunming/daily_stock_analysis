# -*- coding: utf-8 -*-
"""Signal helpers for backtesting."""

from __future__ import annotations

from typing import Optional


def normalize_advice(advice: Optional[str]) -> str:
    text = str(advice or "").strip()
    if not text:
        return "hold"
    if any(k in text for k in ["强烈买入", "强买", "买入", "加仓"]):
        return "buy"
    if any(k in text for k in ["强烈卖出", "卖出", "减仓", "清仓"]):
        return "sell"
    return "hold"


def infer_market(code: str) -> str:
    c = (code or "").strip().upper()
    if len(c) == 6 and c.isdigit():
        return "CN"
    if c.startswith("HK") and len(c) == 7 and c[2:].isdigit():
        return "HK"
    if len(c) == 5 and c.isdigit():
        return "HK"
    return "US"

