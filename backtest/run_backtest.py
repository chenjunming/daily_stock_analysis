# -*- coding: utf-8 -*-
"""Run signal backtest from CLI.

Usage example:
python -m backtest.run_backtest --market US --start 2024-01-01 --end 2026-02-06
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd

from backtest.engine import BacktestConfig, SignalBacktestEngine
from backtest.report import build_markdown_summary


def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_codes(codes: str) -> List[str]:
    return [x.strip().upper() for x in (codes or "").split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="Signal backtest based on analysis history")
    parser.add_argument("--market", type=str, default="", help="CN/HK/US")
    parser.add_argument("--codes", type=str, default="", help="Comma-separated codes (optional)")
    parser.add_argument("--start", type=str, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=1_000_000.0, help="Initial capital")
    parser.add_argument("--fee", type=float, default=0.0005, help="One-way fee rate")
    parser.add_argument("--slippage", type=float, default=0.0003, help="One-way slippage rate")
    parser.add_argument("--benchmark", type=str, default="", help="Benchmark code")
    parser.add_argument("--sword-pct", type=float, default=0.30, help="Sword sleeve capital ratio")
    parser.add_argument("--qi-pct", type=float, default=0.70, help="Qi sleeve capital ratio")
    parser.add_argument("--qi-drawdown-stop", type=float, default=0.25, help="Qi sleeve max drawdown tolerance")
    parser.add_argument("--qi-extreme-rally-trigger", type=float, default=0.20, help="Qi reduce trigger from entry")
    parser.add_argument("--qi-extreme-reduce-fraction", type=float, default=0.3333, help="Qi reduce fraction")
    parser.add_argument("--outdir", type=str, default="reports/backtest", help="Output directory")
    args = parser.parse_args()

    total_ratio = float(args.sword_pct) + float(args.qi_pct)
    if total_ratio <= 0:
        raise ValueError("sword_pct + qi_pct 必须大于 0")
    sword_pct = float(args.sword_pct) / total_ratio
    qi_pct = float(args.qi_pct) / total_ratio

    cfg = BacktestConfig(
        start_date=_parse_date(args.start),
        end_date=_parse_date(args.end),
        market=(args.market or "").strip().upper(),
        initial_capital=float(args.capital),
        fee_rate=float(args.fee),
        slippage_rate=float(args.slippage),
        benchmark_code=(args.benchmark or "").strip().upper(),
        sword_pct=sword_pct,
        qi_pct=qi_pct,
        qi_drawdown_stop=float(args.qi_drawdown_stop),
        qi_extreme_rally_trigger=float(args.qi_extreme_rally_trigger),
        qi_extreme_reduce_fraction=float(args.qi_extreme_reduce_fraction),
    )

    engine = SignalBacktestEngine(cfg)
    result = engine.run(codes=_parse_codes(args.codes))
    summary = build_markdown_summary(result)
    print(summary)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    suffix = f"{cfg.market or 'ALL'}_{cfg.start_date}_{cfg.end_date}".replace(":", "-")

    portfolio: pd.DataFrame = result["portfolio_curve"]  # type: ignore
    benchmark: pd.DataFrame = result["benchmark_curve"]  # type: ignore
    trades = result.get("trades", []) or []

    portfolio.to_csv(outdir / f"portfolio_curve_{suffix}.csv", index=False)
    benchmark.to_csv(outdir / f"benchmark_curve_{suffix}.csv", index=False)
    if trades:
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(outdir / f"trades_{suffix}.csv", index=False)
    with (outdir / f"summary_{suffix}.md").open("w", encoding="utf-8") as f:
        f.write(summary + "\n")

    print(f"\nSaved to: {outdir}")


if __name__ == "__main__":
    main()
