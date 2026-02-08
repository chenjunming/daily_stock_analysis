from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Ensure repo root is importable when package is used from source checkout.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data_provider import DataFetcherManager


ALLOWED_FEATURES = (
    "daily",
    "realtime",
    "chip",
    "name",
    "indices",
    "market_stats",
    "sector_rankings",
)
STOCK_FEATURES = {"daily", "realtime", "chip", "name"}
MARKET_FEATURES = {"indices", "market_stats", "sector_rankings"}


def parse_csv(raw: str) -> List[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return to_jsonable(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def call_manager(func: Any, *args: Any, **kwargs: Any) -> Any:
    """
    Keep stdout clean for JSON output.
    Some upstream providers print diagnostics to stdout directly.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = func(*args, **kwargs)
    noisy_stdout = buf.getvalue().strip()
    if noisy_stdout:
        print(noisy_stdout, file=sys.stderr)
    return result


def validate_inputs(
    stocks_raw: str,
    features_raw: str,
    days: int,
    sector_top_n: int,
) -> Tuple[List[str], List[str]]:
    stocks = parse_csv(stocks_raw)
    features = parse_csv(features_raw)

    if not features:
        raise ValueError("features cannot be empty")
    unknown = [f for f in features if f not in ALLOWED_FEATURES]
    if unknown:
        raise ValueError(f"invalid features: {','.join(unknown)}")
    if days <= 0:
        raise ValueError("days must be > 0")
    if sector_top_n <= 0:
        raise ValueError("sector-top-n must be > 0")
    if any(f in STOCK_FEATURES for f in features) and not stocks:
        raise ValueError("stocks is required when using stock-level features")
    return stocks, features


def run_fetch(
    *,
    stocks_raw: str = "",
    features_raw: str = ",".join(ALLOWED_FEATURES),
    days: int = 30,
    sector_top_n: int = 5,
    strict: bool = False,
) -> Tuple[Dict[str, Any], int]:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    start = time.time()

    stocks, features = validate_inputs(stocks_raw, features_raw, days, sector_top_n)
    manager = DataFetcherManager()

    result: Dict[str, Any] = {
        "meta": {
            "started_at": started_at,
            "duration_ms": 0,
            "stocks": stocks,
            "days": days,
            "features": features,
            "sector_top_n": sector_top_n,
            "strict": bool(strict),
            "partial_failure": False,
            "all_failed": False,
        },
        "stocks": {},
        "market": {},
    }

    requested_stock_features = [f for f in features if f in STOCK_FEATURES]
    requested_market_features = [f for f in features if f in MARKET_FEATURES]

    total_attempts = 0
    total_success = 0
    strict_has_failure = False

    for code in stocks:
        item: Dict[str, Any] = {"errors": []}
        for feature in requested_stock_features:
            item[feature] = None
        result["stocks"][code] = item

        for feature in requested_stock_features:
            total_attempts += 1
            try:
                if feature == "daily":
                    df, source_name = call_manager(manager.get_daily_data, code, days=days)
                    if df is None or df.empty:
                        raise RuntimeError("empty daily data")
                    records = [to_jsonable(x) for x in df.to_dict(orient="records")]
                    item["daily"] = {
                        "source": source_name,
                        "rows": len(records),
                        "data": records,
                    }
                    total_success += 1
                elif feature == "realtime":
                    quote = call_manager(manager.get_realtime_quote, code)
                    if quote is None:
                        raise RuntimeError("realtime quote unavailable")
                    item["realtime"] = to_jsonable(quote)
                    total_success += 1
                elif feature == "chip":
                    chip = call_manager(manager.get_chip_distribution, code)
                    if chip is None:
                        raise RuntimeError("chip distribution unavailable")
                    item["chip"] = to_jsonable(chip)
                    total_success += 1
                elif feature == "name":
                    name = call_manager(manager.get_stock_name, code)
                    if not name:
                        raise RuntimeError("stock name unavailable")
                    item["name"] = name
                    total_success += 1
            except Exception as exc:
                strict_has_failure = True
                item["errors"].append({"feature": feature, "message": str(exc)})

    if "indices" in requested_market_features:
        total_attempts += 1
        try:
            result["market"]["indices"] = to_jsonable(call_manager(manager.get_main_indices))
            total_success += 1
        except Exception as exc:
            result["market"]["indices"] = None
            result["market"].setdefault("errors", []).append(
                {"feature": "indices", "message": str(exc)}
            )

    if "market_stats" in requested_market_features:
        total_attempts += 1
        try:
            result["market"]["market_stats"] = to_jsonable(call_manager(manager.get_market_stats))
            total_success += 1
        except Exception as exc:
            result["market"]["market_stats"] = None
            result["market"].setdefault("errors", []).append(
                {"feature": "market_stats", "message": str(exc)}
            )

    if "sector_rankings" in requested_market_features:
        total_attempts += 1
        try:
            top, bottom = call_manager(manager.get_sector_rankings, sector_top_n)
            result["market"]["sector_rankings"] = {
                "top": to_jsonable(top),
                "bottom": to_jsonable(bottom),
                "n": sector_top_n,
            }
            total_success += 1
        except Exception as exc:
            result["market"]["sector_rankings"] = None
            result["market"].setdefault("errors", []).append(
                {"feature": "sector_rankings", "message": str(exc)}
            )

    result["meta"]["duration_ms"] = int((time.time() - start) * 1000)
    result["meta"]["partial_failure"] = total_success < total_attempts
    result["meta"]["all_failed"] = total_attempts > 0 and total_success == 0

    if strict and strict_has_failure:
        return result, 3
    if result["meta"]["all_failed"]:
        return result, 3
    return result, 0

