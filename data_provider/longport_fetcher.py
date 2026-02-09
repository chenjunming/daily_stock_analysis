# -*- coding: utf-8 -*-
"""
===================================
LongportFetcher - 长桥行情数据源
===================================

数据来源：Longbridge OpenAPI（longport Python SDK）
特点：港美股行情稳定，支持 A/HK/US 实时与名称补全，需配置 API 凭证

支持能力：
1. 日线历史数据（history_candlesticks_by_date）
2. 实时行情（quote）
3. 股票名称（static_info）
4. 主要指数（quote，作为主尝试）
"""

import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from src.config import get_config

logger = logging.getLogger(__name__)


class LongportFetcher(BaseFetcher):
    """
    Longport 数据源实现（港股/美股）
    """

    name = "LongportFetcher"
    priority = int(os.getenv("LONGPORT_PRIORITY", "-2"))
    _DEFAULT_MAIN_INDICES: List[Tuple[str, str]] = [
        ("000001.SH", "上证指数"),
        ("399001.SZ", "深证成指"),
        ("399006.SZ", "创业板指"),
        ("000688.SH", "科创50"),
        ("000016.SH", "上证50"),
        ("000300.SH", "沪深300"),
        ("HSI.HK", "恒生指数"),
        ("HSTECH.HK", "恒生科技"),
        ("SPY.US", "S&P 500 ETF"),
        ("QQQ.US", "纳斯达克100 ETF"),
    ]

    def __init__(self):
        self._config = get_config()
        self._app_key = self._config.longport_app_key or ""
        self._app_secret = self._config.longport_app_secret or ""
        self._access_token = self._config.longport_access_token or ""
        self._http_url = self._config.longport_http_url or None
        self._quote_ws_url = self._config.longport_quote_ws_url or None
        self._token_expires_at = self._parse_iso_datetime(self._config.longport_access_token_expires_at)
        self._auto_refresh = bool(self._config.longport_auto_refresh)
        self._persist_access_token = bool(self._config.longport_persist_access_token)
        self._refresh_lock = threading.Lock()

        self._api = None
        self._lp_config = None
        self._ctx = None
        self._enabled = False
        self._name_cache: Dict[str, str] = {}
        self._main_indices = self._load_main_indices_from_env()
        self._init_client()
        if not self._enabled:
            # 凭证未配置时降低优先级，避免影响默认链路
            self.priority = 98

    def is_available(self) -> bool:
        return self._enabled and self._ctx is not None and self._api is not None

    @staticmethod
    def _parse_iso_datetime(raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        text = raw.strip()
        if not text:
            return None
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _build_longport_config(self, access_token: str):
        from longport.openapi import Config as LongportConfig

        return LongportConfig(
            app_key=self._app_key,
            app_secret=self._app_secret,
            access_token=access_token,
            http_url=self._http_url,
            quote_ws_url=self._quote_ws_url,
        )

    def _should_refresh_soon(self) -> bool:
        if not self._token_expires_at:
            return False
        now = datetime.now(timezone.utc)
        return now + timedelta(hours=12) >= self._token_expires_at.astimezone(timezone.utc)

    def _persist_token_to_env(self, token: str) -> None:
        if not self._persist_access_token:
            return

        env_path = Path(__file__).resolve().parent.parent / ".env"
        try:
            if env_path.exists():
                lines = env_path.read_text(encoding="utf-8").splitlines()
            else:
                lines = []

            updated = False
            new_lines: List[str] = []
            for line in lines:
                if line.startswith("LONGPORT_ACCESS_TOKEN="):
                    new_lines.append(f"LONGPORT_ACCESS_TOKEN={token}")
                    updated = True
                else:
                    new_lines.append(line)
            if not updated:
                new_lines.append(f"LONGPORT_ACCESS_TOKEN={token}")

            env_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
            logger.info("[Longport] 已回写 LONGPORT_ACCESS_TOKEN 到 .env")
        except Exception as e:
            logger.warning(f"[Longport] 回写 .env 失败，将仅保存在内存: {e}")

    @staticmethod
    def _is_token_expired_error(exc: Exception) -> bool:
        code = getattr(exc, "code", None)
        msg = str(exc).lower()
        if code in {401003}:
            return True
        return ("token" in msg) and ("expired" in msg or "invalid" in msg)

    def _refresh_access_token(self, reason: str) -> bool:
        if not self._auto_refresh or not self._lp_config:
            return False

        with self._refresh_lock:
            try:
                # 进入锁后再检查一次，避免并发重复刷新
                if self._should_refresh_soon() is False and reason == "token_near_expiry":
                    return False

                logger.warning(f"[Longport] 尝试刷新 Access Token，原因: {reason}")
                if self._token_expires_at:
                    new_token = self._lp_config.refresh_access_token(self._token_expires_at)
                else:
                    new_token = self._lp_config.refresh_access_token()
                if not new_token:
                    logger.warning("[Longport] 刷新 Access Token 返回空值")
                    return False

                self._access_token = str(new_token)
                self._config.longport_access_token = self._access_token
                os.environ["LONGPORT_ACCESS_TOKEN"] = self._access_token

                # 刷新后重建上下文，确保后续请求使用新 token
                self._lp_config = self._build_longport_config(self._access_token)
                from longport.openapi import QuoteContext

                self._ctx = QuoteContext(self._lp_config)
                self._persist_token_to_env(self._access_token)
                logger.info("[Longport] Access Token 刷新成功")
                return True
            except Exception as e:
                logger.warning(f"[Longport] 刷新 Access Token 失败: {e}")
                return False

    def _call_with_refresh(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if self._is_token_expired_error(e):
                refreshed = self._refresh_access_token("token_expired")
                if refreshed:
                    return func(*args, **kwargs)
            raise

    def _init_client(self) -> None:
        if not (self._app_key and self._app_secret and self._access_token):
            logger.info("[Longport] 凭证未配置，跳过初始化")
            return

        try:
            from longport.openapi import QuoteContext
            import longport.openapi as longport_api

            self._lp_config = self._build_longport_config(self._access_token)
            self._ctx = QuoteContext(self._lp_config)
            self._api = longport_api
            self._enabled = True
            if self._auto_refresh and self._should_refresh_soon():
                self._refresh_access_token("token_near_expiry")
            logger.info("[Longport] QuoteContext 初始化成功")
        except Exception as e:
            logger.warning(f"[Longport] 初始化失败，将降级到其他数据源: {e}")
            self._enabled = False

    @staticmethod
    def _is_hk_code(stock_code: str) -> bool:
        code = (stock_code or "").strip().upper()
        if code.endswith(".HK"):
            code = code[:-3]
        if code.startswith("HK"):
            code = code[2:]
        return bool(re.match(r"^\d{1,5}$", code))

    @staticmethod
    def _is_us_code(stock_code: str) -> bool:
        code = (stock_code or "").strip().upper()
        if code.endswith(".US"):
            code = code[:-3]
        return bool(re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", code))

    @staticmethod
    def _is_cn_code(stock_code: str) -> bool:
        code = (stock_code or "").strip().upper()
        if code.endswith(".SH") or code.endswith(".SZ"):
            code = code[:-3]
        return bool(re.match(r"^\d{6}$", code))

    def _to_longport_symbol(self, stock_code: str) -> Optional[str]:
        code = (stock_code or "").strip().upper()
        if not code:
            return None

        # 已经是 Longport 标准格式
        if re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?\.US$", code):
            return code
        if re.match(r"^\d{1,5}\.HK$", code):
            num = str(int(code[:-3]))
            return f"{num}.HK"

        # 美股：AAPL / BRK.B
        if self._is_us_code(code):
            base = code[:-3] if code.endswith(".US") else code
            return f"{base}.US"

        # A股：600519 / 000001 / 600519.SH / 000001.SZ
        if self._is_cn_code(code):
            if code.endswith(".SH") or code.endswith(".SZ"):
                return code
            # 经验规则：6/5/9 开头一般为上交所，其余按深交所
            exchange = "SH" if code.startswith(("5", "6", "9")) else "SZ"
            return f"{code}.{exchange}"

        # 港股：HK00700 / 00700 / 700.HK
        if self._is_hk_code(code):
            if code.endswith(".HK"):
                code = code[:-3]
            if code.startswith("HK"):
                code = code[2:]
            num = str(int(code))
            return f"{num}.HK"

        return None

    @classmethod
    def _load_main_indices_from_env(cls) -> List[Tuple[str, str]]:
        """
        支持通过 LONGPORT_MAIN_INDICES 覆盖指数映射。

        格式: SYMBOL:名称,SYMBOL:名称
        示例: 000001.SH:上证指数,399001.SZ:深证成指,SPY.US:S&P 500 ETF
        """
        raw = (os.getenv("LONGPORT_MAIN_INDICES", "") or "").strip()
        if not raw:
            return list(cls._DEFAULT_MAIN_INDICES)

        parsed: List[Tuple[str, str]] = []
        for chunk in raw.split(","):
            text = chunk.strip()
            if not text:
                continue
            if ":" in text:
                symbol, name = text.split(":", 1)
            else:
                symbol, name = text, text
            symbol = symbol.strip().upper()
            name = name.strip()
            if symbol:
                parsed.append((symbol, name or symbol))

        return parsed or list(cls._DEFAULT_MAIN_INDICES)

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        if not self.is_available():
            raise DataFetchError("Longport 未启用或初始化失败")

        symbol = self._to_longport_symbol(stock_code)
        if not symbol:
            raise DataFetchError(f"Longport 仅支持港股/美股，当前代码不支持: {stock_code}")

        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        try:
            candles = self._call_with_refresh(
                self._ctx.history_candlesticks_by_date,
                symbol,
                self._api.Period.Day,
                self._api.AdjustType.ForwardAdjust,
                start,
                end,
            )
        except Exception as e:
            raise DataFetchError(f"Longport 获取历史数据失败: {e}") from e

        rows: List[Dict[str, Any]] = []
        for item in candles or []:
            ts = getattr(item, "timestamp", None)
            rows.append(
                {
                    "code": stock_code,
                    "date": ts,
                    "open": safe_float(getattr(item, "open", None)),
                    "high": safe_float(getattr(item, "high", None)),
                    "low": safe_float(getattr(item, "low", None)),
                    "close": safe_float(getattr(item, "close", None)),
                    "volume": safe_int(getattr(item, "volume", None), 0),
                    "amount": safe_float(getattr(item, "turnover", None), 0.0),
                }
            )

        if not rows:
            raise DataFetchError(f"Longport 未查询到 {stock_code} 的历史数据")

        return pd.DataFrame(rows)

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        out = df.copy()
        if out.empty:
            return out

        if "code" not in out.columns:
            out["code"] = stock_code
        if "amount" not in out.columns:
            out["amount"] = 0.0

        out["pct_chg"] = out["close"].pct_change() * 100
        out["pct_chg"] = out["pct_chg"].fillna(0).round(2)

        keep_cols = ["code"] + STANDARD_COLUMNS
        existing_cols = [c for c in keep_cols if c in out.columns]
        return out[existing_cols]

    def _resolve_name(self, symbol: str) -> str:
        if symbol in self._name_cache:
            return self._name_cache[symbol]

        name = ""
        try:
            info_list = self._call_with_refresh(self._ctx.static_info, [symbol])
            if info_list:
                info = info_list[0]
                name = (
                    (getattr(info, "name_cn", "") or "").strip()
                    or (getattr(info, "name_hk", "") or "").strip()
                    or (getattr(info, "name_en", "") or "").strip()
                )
        except Exception:
            name = ""

        self._name_cache[symbol] = name
        return name

    @staticmethod
    def _index_code_from_symbol(symbol: str) -> str:
        raw = (symbol or "").strip().upper()
        if "." in raw:
            return raw.split(".", 1)[0]
        return raw

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """
        获取股票名称（Longport static_info）。
        """
        code = (stock_code or "").strip().upper()
        if not code or not self.is_available():
            return None

        if code in self._name_cache and self._name_cache[code]:
            return self._name_cache[code]

        symbol = self._to_longport_symbol(code)
        if not symbol:
            return None

        name = self._resolve_name(symbol)
        if name:
            self._name_cache[code] = name
            return name
        return None

    def get_main_indices(self) -> Optional[List[Dict[str, Any]]]:
        """
        获取主要指数行情（Longport 优先）。
        任何单个指数查询失败时跳过，整体失败返回 None，让管理器回退其他数据源。
        """
        if not self.is_available():
            return None

        results: List[Dict[str, Any]] = []
        for symbol, display_name in self._main_indices:
            try:
                quotes = self._call_with_refresh(self._ctx.quote, [symbol])
                if not quotes:
                    continue
                q = quotes[0]
                current = safe_float(getattr(q, "last_done", None))
                if current is None or current <= 0:
                    continue

                prev_close = safe_float(getattr(q, "prev_close", None), 0.0) or 0.0
                change = safe_float(getattr(q, "change_value", None))
                change_pct = safe_float(getattr(q, "change_rate", None))
                if change is None and prev_close > 0:
                    change = current - prev_close
                if change_pct is None and prev_close > 0:
                    change_pct = ((current - prev_close) / prev_close) * 100

                high = safe_float(getattr(q, "high", None), current) or current
                low = safe_float(getattr(q, "low", None), current) or current
                amplitude = 0.0
                if prev_close > 0:
                    amplitude = ((high - low) / prev_close) * 100

                results.append(
                    {
                        "code": self._index_code_from_symbol(symbol),
                        "name": display_name,
                        "current": current,
                        "change": change or 0.0,
                        "change_pct": change_pct or 0.0,
                        "open": safe_float(getattr(q, "open", None), current) or current,
                        "high": high,
                        "low": low,
                        "prev_close": prev_close,
                        "volume": safe_float(getattr(q, "volume", None), 0.0) or 0.0,
                        "amount": safe_float(getattr(q, "turnover", None), 0.0) or 0.0,
                        "amplitude": amplitude,
                    }
                )
            except Exception as e:
                logger.debug(f"[Longport] 指数行情查询失败 {symbol}: {e}")
                continue

        return results or None

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        if not self.is_available():
            return None

        symbol = self._to_longport_symbol(stock_code)
        if not symbol:
            return None

        try:
            quotes = self._call_with_refresh(self._ctx.quote, [symbol])
        except Exception as e:
            logger.warning(f"[Longport] 获取实时行情失败 {stock_code}: {e}")
            return None

        if not quotes:
            return None

        q = quotes[0]

        # 美股时段价格选择：
        # 1) 若 trade_session 可识别，优先使用当前会话对应价格（夜盘时段优先夜盘）
        # 2) 否则回退到“时间最新，时间相同按 夜盘 > 盘后 > 盘前 > 常规”
        session_label = "regular"
        session_ts = getattr(q, "timestamp", None)
        session_price = safe_float(getattr(q, "last_done", None))
        session_pre_close = safe_float(getattr(q, "prev_close", None))
        session_high = safe_float(getattr(q, "high", None))
        session_low = safe_float(getattr(q, "low", None))
        session_volume = safe_int(getattr(q, "volume", None), None)
        session_amount = safe_float(getattr(q, "turnover", None), None)

        if str(symbol).endswith(".US"):
            # 候选时段：(label, object, priority)
            # priority 仅用于 fallback；越大优先级越高
            raw_candidates = [
                ("regular", q, 0),
                ("pre", getattr(q, "pre_market_quote", None), 1),
                ("post", getattr(q, "post_market_quote", None), 2),
                ("overnight", getattr(q, "overnight_quote", None), 3),
            ]
            candidates: List[tuple[str, Any, int, Any]] = []
            for label, obj, pri in raw_candidates:
                if obj is None:
                    continue
                p = safe_float(getattr(obj, "last_done", None))
                if p is None or p <= 0:
                    continue
                candidates.append((label, obj, pri, getattr(obj, "timestamp", None)))

            def _label_from_trade_session(raw_session: Any) -> Optional[str]:
                text = str(raw_session or "").strip().lower()
                if text.endswith(".overnight") or text == "overnight":
                    return "overnight"
                if text.endswith(".post") or text == "post":
                    return "post"
                if text.endswith(".pre") or text == "pre":
                    return "pre"
                if text.endswith(".intraday") or text == "intraday":
                    return "regular"
                return None

            best = None
            active_label = _label_from_trade_session(getattr(q, "trade_session", None))
            if active_label:
                for label, obj, pri, ts in candidates:
                    if label == active_label:
                        best = (label, obj, pri, ts)
                        break

            # fallback: 时间最新优先；时间戳不可比较时按优先级（夜盘>盘后>盘前>常规）
            if best is None:
                for label, obj, pri, ts in candidates:
                    if best is None:
                        best = (label, obj, pri, ts)
                        continue
                    _, _, best_pri, best_ts = best
                    if ts is not None and best_ts is not None:
                        if ts > best_ts or (ts == best_ts and pri > best_pri):
                            best = (label, obj, pri, ts)
                    else:
                        if pri > best_pri:
                            best = (label, obj, pri, ts)

            if best is not None:
                session_label, session_obj, _, session_ts = best
                session_price = safe_float(getattr(session_obj, "last_done", None))
                session_pre_close = safe_float(getattr(session_obj, "prev_close", None))
                session_high = safe_float(getattr(session_obj, "high", None))
                session_low = safe_float(getattr(session_obj, "low", None))
                session_volume = safe_int(getattr(session_obj, "volume", None), None)
                session_amount = safe_float(getattr(session_obj, "turnover", None), None)

        price = session_price
        pre_close = session_pre_close
        open_price = safe_float(getattr(q, "open", None))
        high = session_high
        low = session_low
        volume = session_volume
        amount = session_amount
        name = self._resolve_name(symbol)
        turnover_rate = None
        volume_ratio = None
        pe_ratio = None
        pb_ratio = None
        total_mv = None

        # calc_indexes 为增强信息，失败不影响主报价。
        try:
            calc_data = self._call_with_refresh(
                self._ctx.calc_indexes,
                [symbol],
                [
                    self._api.CalcIndex.TurnoverRate,
                    self._api.CalcIndex.VolumeRatio,
                    self._api.CalcIndex.PeTtmRatio,
                    self._api.CalcIndex.PbRatio,
                    self._api.CalcIndex.TotalMarketValue,
                ],
            )
            if calc_data:
                calc = calc_data[0]
                turnover_rate = safe_float(getattr(calc, "turnover_rate", None))
                volume_ratio = safe_float(getattr(calc, "volume_ratio", None))
                pe_ratio = safe_float(getattr(calc, "pe_ttm_ratio", None))
                pb_ratio = safe_float(getattr(calc, "pb_ratio", None))
                total_mv = safe_float(getattr(calc, "total_market_value", None))
        except Exception as e:
            logger.debug(f"[Longport] calc_indexes 获取失败 {stock_code}: {e}")

        change_amount = None
        change_pct = None
        if price is not None and pre_close and pre_close > 0:
            change_amount = price - pre_close
            change_pct = (change_amount / pre_close) * 100

        return UnifiedRealtimeQuote(
            code=stock_code,
            name=name,
            source=RealtimeSource.LONGPORT,
            price=price,
            change_pct=change_pct,
            change_amount=change_amount,
            volume=volume,
            amount=amount,
            volume_ratio=volume_ratio,
            turnover_rate=turnover_rate,
            open_price=open_price,
            high=high,
            low=low,
            pre_close=pre_close,
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            total_mv=total_mv,
            trade_session=str(getattr(q, "trade_session", "") or "").strip() or None,
            price_session=session_label,
            price_timestamp=session_ts.isoformat() if hasattr(session_ts, "isoformat") else None,
        )
