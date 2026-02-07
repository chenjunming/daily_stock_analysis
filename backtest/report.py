# -*- coding: utf-8 -*-
"""Backtest reporting helpers."""

from __future__ import annotations

from typing import Dict, List

from backtest.engine import Trade


def format_pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def build_markdown_summary(result: Dict[str, object]) -> str:
    metrics = result.get("metrics", {}) or {}
    codes = result.get("codes", []) or []
    trades: List[Trade] = result.get("trades", []) or []
    cfg = result.get("config")
    bench = result.get("benchmark_curve")
    bench_code = "N/A"
    if bench is not None and len(bench) > 0 and "benchmark_code" in bench:
        bench_code = str(bench["benchmark_code"].iloc[0])

    lines = [
        "## 回测结果",
        "",
        f"- 标的数: **{len(codes)}**",
        f"- 基准: **{bench_code}**",
        f"- 交易次数: **{len(trades)}**",
        f"- 仓位框架: **剑宗{getattr(cfg, 'sword_pct', 0.3) * 100:.0f}% + 气宗{getattr(cfg, 'qi_pct', 0.7) * 100:.0f}%**",
        "",
        "### 核心指标",
        "",
    ]
    if metrics:
        lines.extend(
            [
                f"- 总收益: **{format_pct(metrics.get('total_return', 0.0))}**",
                f"- 年化收益: **{format_pct(metrics.get('annual_return', 0.0))}**",
                f"- 最大回撤: **{format_pct(metrics.get('max_drawdown', 0.0))}**",
                f"- 夏普比率: **{metrics.get('sharpe', 0.0):.2f}**",
                f"- Calmar: **{metrics.get('calmar', 0.0):.2f}**",
            ]
        )
    else:
        lines.append("- 指标不可用（曲线为空）")

    if trades:
        win = [t for t in trades if t.side == "sell" and (t.pnl or 0.0) > 0]
        loss = [t for t in trades if t.side == "sell" and (t.pnl or 0.0) <= 0]
        sword_trades = sum(1 for t in trades if t.sleeve == "sword")
        qi_trades = sum(1 for t in trades if t.sleeve == "qi")
        lines.extend(
            [
                "",
                "### 交易统计",
                "",
                f"- 平仓次数: **{len(win) + len(loss)}**",
                f"- 胜率: **{(len(win) / max(len(win) + len(loss), 1)) * 100:.2f}%**",
                f"- 盈利单: **{len(win)}** | 亏损单: **{len(loss)}**",
                f"- 剑宗交易: **{sword_trades}** | 气宗交易: **{qi_trades}**",
            ]
        )
    return "\n".join(lines)
