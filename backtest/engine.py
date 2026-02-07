# -*- coding: utf-8 -*-
"""Signal backtest engine (v2) with Sword/Qi allocation framework."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import and_, select

from src.storage import AnalysisHistory, StockDaily, get_db
from backtest.signals import infer_market, normalize_advice


@dataclass
class Trade:
    code: str
    sleeve: str
    side: str
    dt: date
    price: float
    shares: float
    fee: float
    pnl: Optional[float] = None


@dataclass
class BacktestConfig:
    start_date: date
    end_date: date
    market: str = ""
    initial_capital: float = 1_000_000.0
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0003
    benchmark_code: str = ""
    sword_pct: float = 0.30
    qi_pct: float = 0.70
    qi_drawdown_stop: float = 0.25
    qi_extreme_rally_trigger: float = 0.20
    qi_extreme_reduce_fraction: float = 1.0 / 3.0


class SignalBacktestEngine:
    def __init__(self, cfg: BacktestConfig):
        self.cfg = cfg
        self.db = get_db()

    def run(self, codes: Optional[List[str]] = None) -> Dict[str, object]:
        picked = codes or self._pick_codes_from_history()
        if not picked:
            raise ValueError("未找到可回测标的，请先确保有分析历史和日线数据")

        prices = self._load_prices(picked)
        if prices.empty:
            raise ValueError("未加载到价格数据，请检查 stock_daily 数据范围")

        signals = self._load_daily_signals(picked)
        cap_per_code = float(self.cfg.initial_capital) / float(len(picked))

        code_curves: Dict[str, pd.DataFrame] = {}
        trades: List[Trade] = []
        for code in picked:
            df_code = prices[prices["code"] == code].copy()
            if df_code.empty:
                continue
            sig_map = signals.get(code, {})
            curve, code_trades = self._simulate_one_code(code, df_code, sig_map, cap_per_code)
            code_curves[code] = curve
            trades.extend(code_trades)

        if not code_curves:
            raise ValueError("可回测标的无有效行情曲线")

        portfolio = self._merge_curves(code_curves)
        benchmark = self._build_benchmark(portfolio["date"])
        metrics = self._calc_metrics(portfolio["equity"])

        out = {
            "codes": list(code_curves.keys()),
            "portfolio_curve": portfolio,
            "benchmark_curve": benchmark,
            "metrics": metrics,
            "trades": trades,
            "config": self.cfg,
        }
        return out

    def _pick_codes_from_history(self) -> List[str]:
        start_dt = datetime.combine(self.cfg.start_date, datetime.min.time())
        end_dt = datetime.combine(self.cfg.end_date + timedelta(days=1), datetime.min.time())
        with self.db.get_session() as session:
            rows = session.execute(
                select(AnalysisHistory.code)
                .where(
                    and_(
                        AnalysisHistory.created_at >= start_dt,
                        AnalysisHistory.created_at < end_dt,
                    )
                )
            ).all()
        codes = sorted({str(r[0]).upper() for r in rows if r and r[0]})
        if self.cfg.market:
            m = self.cfg.market.upper()
            codes = [c for c in codes if infer_market(c) == m]
        return codes

    def _load_prices(self, codes: List[str]) -> pd.DataFrame:
        with self.db.get_session() as session:
            rows = session.execute(
                select(
                    StockDaily.code,
                    StockDaily.date,
                    StockDaily.open,
                    StockDaily.close,
                    StockDaily.ma5,
                )
                .where(
                    and_(
                        StockDaily.code.in_(codes),
                        StockDaily.date >= self.cfg.start_date,
                        StockDaily.date <= self.cfg.end_date,
                    )
                )
            ).all()
        df = pd.DataFrame(rows, columns=["code", "date", "open", "close", "ma5"])
        if df.empty:
            return df
        df["code"] = df["code"].astype(str).str.upper()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values(["code", "date"]).reset_index(drop=True)
        return df

    def _load_daily_signals(self, codes: List[str]) -> Dict[str, Dict[date, str]]:
        start_dt = datetime.combine(self.cfg.start_date - timedelta(days=5), datetime.min.time())
        end_dt = datetime.combine(self.cfg.end_date + timedelta(days=1), datetime.min.time())
        with self.db.get_session() as session:
            rows = session.execute(
                select(
                    AnalysisHistory.code,
                    AnalysisHistory.created_at,
                    AnalysisHistory.operation_advice,
                    AnalysisHistory.id,
                )
                .where(
                    and_(
                        AnalysisHistory.code.in_(codes),
                        AnalysisHistory.created_at >= start_dt,
                        AnalysisHistory.created_at < end_dt,
                    )
                )
            ).all()
        if not rows:
            return {}
        df = pd.DataFrame(rows, columns=["code", "created_at", "advice", "id"])
        df["code"] = df["code"].astype(str).str.upper()
        df["signal_date"] = pd.to_datetime(df["created_at"]).dt.date
        df = df.sort_values(["code", "signal_date", "created_at", "id"]).drop_duplicates(
            ["code", "signal_date"], keep="last"
        )
        df["signal"] = df["advice"].map(normalize_advice)
        signal_map: Dict[str, Dict[date, str]] = {}
        for _, r in df.iterrows():
            signal_map.setdefault(r["code"], {})[r["signal_date"]] = r["signal"]
        return signal_map

    def _simulate_one_code(
        self,
        code: str,
        df_code: pd.DataFrame,
        signal_map: Dict[date, str],
        capital: float,
    ) -> Tuple[pd.DataFrame, List[Trade]]:
        # Sword/Qi sleeves
        sword_cash = float(capital) * float(self.cfg.sword_pct)
        qi_cash = float(capital) * float(self.cfg.qi_pct)
        sword_shares = 0.0
        qi_shares = 0.0
        sword_entry = 0.0
        qi_entry = 0.0
        qi_peak = 0.0
        qi_reduced_once = False

        pending = "hold"
        pending_sword_exit = False
        pending_qi_exit = False
        pending_qi_reduce_frac = 0.0
        trades: List[Trade] = []
        rows = []

        for _, r in df_code.iterrows():
            dt = r["date"]
            open_px = float(r["open"] or 0.0)
            close_px = float(r["close"] or 0.0)
            ma5 = float(r["ma5"] or 0.0)
            exec_px = open_px if open_px > 0 else close_px

            # Execute order from previous day signal at current open.
            if exec_px > 0:
                # 1) Forced exits/reductions first (risk control precedence)
                if pending_sword_exit and sword_shares > 0:
                    fill = exec_px * (1.0 - self.cfg.slippage_rate)
                    notional = sword_shares * fill
                    fee = notional * self.cfg.fee_rate
                    sword_cash += notional - fee
                    pnl = (fill - sword_entry) * sword_shares - fee
                    trades.append(
                        Trade(code=code, sleeve="sword", side="sell", dt=dt, price=fill, shares=sword_shares, fee=fee, pnl=pnl)
                    )
                    sword_shares = 0.0
                    sword_entry = 0.0
                    pending_sword_exit = False

                if pending_qi_exit and qi_shares > 0:
                    fill = exec_px * (1.0 - self.cfg.slippage_rate)
                    notional = qi_shares * fill
                    fee = notional * self.cfg.fee_rate
                    qi_cash += notional - fee
                    pnl = (fill - qi_entry) * qi_shares - fee
                    trades.append(
                        Trade(code=code, sleeve="qi", side="sell", dt=dt, price=fill, shares=qi_shares, fee=fee, pnl=pnl)
                    )
                    qi_shares = 0.0
                    qi_entry = 0.0
                    qi_peak = 0.0
                    qi_reduced_once = False
                    pending_qi_exit = False

                if pending_qi_reduce_frac > 0 and qi_shares > 0:
                    reduce_shares = qi_shares * pending_qi_reduce_frac
                    if reduce_shares > 0:
                        fill = exec_px * (1.0 - self.cfg.slippage_rate)
                        notional = reduce_shares * fill
                        fee = notional * self.cfg.fee_rate
                        qi_cash += notional - fee
                        pnl = (fill - qi_entry) * reduce_shares - fee
                        trades.append(
                            Trade(
                                code=code,
                                sleeve="qi",
                                side="reduce",
                                dt=dt,
                                price=fill,
                                shares=reduce_shares,
                                fee=fee,
                                pnl=pnl,
                            )
                        )
                        qi_shares -= reduce_shares
                    pending_qi_reduce_frac = 0.0

                # 2) Signal orders
                if pending == "sell":
                    if sword_shares > 0:
                        fill = exec_px * (1.0 - self.cfg.slippage_rate)
                        notional = sword_shares * fill
                        fee = notional * self.cfg.fee_rate
                        sword_cash += notional - fee
                        pnl = (fill - sword_entry) * sword_shares - fee
                        trades.append(
                            Trade(code=code, sleeve="sword", side="sell", dt=dt, price=fill, shares=sword_shares, fee=fee, pnl=pnl)
                        )
                        sword_shares = 0.0
                        sword_entry = 0.0
                    if qi_shares > 0:
                        fill = exec_px * (1.0 - self.cfg.slippage_rate)
                        notional = qi_shares * fill
                        fee = notional * self.cfg.fee_rate
                        qi_cash += notional - fee
                        pnl = (fill - qi_entry) * qi_shares - fee
                        trades.append(
                            Trade(code=code, sleeve="qi", side="sell", dt=dt, price=fill, shares=qi_shares, fee=fee, pnl=pnl)
                        )
                        qi_shares = 0.0
                        qi_entry = 0.0
                        qi_peak = 0.0
                        qi_reduced_once = False
                elif pending == "buy":
                    # Sword sleeve enter
                    if sword_shares <= 0 and sword_cash > 0:
                        fill = exec_px * (1.0 + self.cfg.slippage_rate)
                        qty = sword_cash / (fill * (1.0 + self.cfg.fee_rate)) if fill > 0 else 0.0
                        if qty > 0:
                            notional = qty * fill
                            fee = notional * self.cfg.fee_rate
                            sword_cash -= notional + fee
                            sword_shares += qty
                            sword_entry = fill
                            trades.append(Trade(code=code, sleeve="sword", side="buy", dt=dt, price=fill, shares=qty, fee=fee))
                    # Qi sleeve enter
                    if qi_shares <= 0 and qi_cash > 0:
                        fill = exec_px * (1.0 + self.cfg.slippage_rate)
                        qty = qi_cash / (fill * (1.0 + self.cfg.fee_rate)) if fill > 0 else 0.0
                        if qty > 0:
                            notional = qty * fill
                            fee = notional * self.cfg.fee_rate
                            qi_cash -= notional + fee
                            qi_shares += qty
                            qi_entry = fill
                            qi_peak = fill
                            qi_reduced_once = False
                            trades.append(Trade(code=code, sleeve="qi", side="buy", dt=dt, price=fill, shares=qty, fee=fee))

            mark_px = close_px if close_px > 0 else exec_px
            sword_equity = sword_cash + sword_shares * mark_px
            qi_equity = qi_cash + qi_shares * mark_px
            equity = sword_equity + qi_equity
            rows.append({"date": dt, "equity": equity, "sword_equity": sword_equity, "qi_equity": qi_equity})

            # Sleeve risk rules evaluated on close, executed next open.
            if sword_shares > 0 and ma5 > 0 and close_px > 0 and close_px < ma5:
                pending_sword_exit = True

            if qi_shares > 0 and close_px > 0:
                qi_peak = max(qi_peak, close_px) if qi_peak > 0 else close_px
                # Qi sleeve: tolerate 20-30% drawdown (default 25%)
                if qi_peak > 0 and close_px <= qi_peak * (1.0 - float(self.cfg.qi_drawdown_stop)):
                    pending_qi_exit = True
                # Qi sleeve: extreme short rally -> reduce
                if (
                    not qi_reduced_once
                    and qi_entry > 0
                    and close_px >= qi_entry * (1.0 + float(self.cfg.qi_extreme_rally_trigger))
                ):
                    pending_qi_reduce_frac = float(self.cfg.qi_extreme_reduce_fraction)
                    qi_reduced_once = True

            # Set signal generated today, executed next day.
            pending = signal_map.get(dt, "hold")

        curve = pd.DataFrame(rows)
        curve["code"] = code
        return curve, trades

    @staticmethod
    def _merge_curves(code_curves: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        all_dates = sorted(
            {d for df in code_curves.values() for d in pd.to_datetime(df["date"]).dt.date.tolist()}
        )
        merged = pd.DataFrame({"date": all_dates})
        merged["equity"] = 0.0
        for _, df in code_curves.items():
            tmp = df[["date", "equity"]].copy()
            tmp = merged[["date"]].merge(tmp, on="date", how="left")
            tmp["equity"] = tmp["equity"].ffill().bfill()
            merged["equity"] += tmp["equity"]
        merged["ret"] = merged["equity"].pct_change().fillna(0.0)
        return merged

    def _build_benchmark(self, dates: pd.Series) -> pd.DataFrame:
        code = (self.cfg.benchmark_code or "").strip().upper()
        if not code:
            default = {"US": "QQQ", "CN": "510300", "HK": "HK2800"}
            code = default.get((self.cfg.market or "").upper(), "QQQ")

        with self.db.get_session() as session:
            rows = session.execute(
                select(StockDaily.date, StockDaily.close)
                .where(
                    and_(
                        StockDaily.code == code,
                        StockDaily.date >= self.cfg.start_date,
                        StockDaily.date <= self.cfg.end_date,
                    )
                )
            ).all()
        df = pd.DataFrame(rows, columns=["date", "close"])
        if df.empty:
            return pd.DataFrame({"date": dates, "benchmark_equity": [None] * len(dates), "benchmark_code": code})

        df["date"] = pd.to_datetime(df["date"]).dt.date
        bench = pd.DataFrame({"date": pd.to_datetime(dates).dt.date})
        bench = bench.merge(df, on="date", how="left")
        bench["close"] = bench["close"].ffill().bfill()
        first = float(bench["close"].iloc[0]) if len(bench) and bench["close"].iloc[0] else 0.0
        if first > 0:
            bench["benchmark_equity"] = self.cfg.initial_capital * (bench["close"] / first)
        else:
            bench["benchmark_equity"] = None
        bench["benchmark_code"] = code
        return bench[["date", "benchmark_equity", "benchmark_code"]]

    @staticmethod
    def _calc_metrics(equity: pd.Series) -> Dict[str, float]:
        eq = equity.astype(float)
        if eq.empty or eq.iloc[0] <= 0:
            return {}
        total_return = eq.iloc[-1] / eq.iloc[0] - 1.0
        days = max(len(eq), 1)
        annual_return = (1.0 + total_return) ** (252.0 / days) - 1.0
        rets = eq.pct_change().dropna()
        vol = rets.std() * (252 ** 0.5) if not rets.empty else 0.0
        sharpe = (rets.mean() * 252 / vol) if vol > 1e-12 else 0.0
        running_max = eq.cummax()
        drawdown = eq / running_max - 1.0
        max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
        calmar = annual_return / abs(max_drawdown) if abs(max_drawdown) > 1e-12 else 0.0
        return {
            "total_return": float(total_return),
            "annual_return": float(annual_return),
            "max_drawdown": float(max_drawdown),
            "sharpe": float(sharpe),
            "calmar": float(calmar),
        }
