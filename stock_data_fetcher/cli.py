from __future__ import annotations

import argparse
import json

from .core import ALLOWED_FEATURES, run_fetch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch stock and market data via DataFetcherManager with stable JSON output."
    )
    parser.add_argument(
        "--stocks",
        type=str,
        default="",
        help="Comma-separated stock codes. Example: 600519,000001,AAPL",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Daily candle lookback days for daily feature. Default: 30",
    )
    parser.add_argument(
        "--features",
        type=str,
        default=",".join(ALLOWED_FEATURES),
        help="Comma-separated feature set.",
    )
    parser.add_argument(
        "--sector-top-n",
        type=int,
        default=5,
        help="Top N sectors for sector_rankings. Default: 5",
    )
    parser.add_argument(
        "--format",
        type=str,
        choices=("json", "pretty"),
        default="json",
        help="Output format. Default: json",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any requested stock feature fails for any stock.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result, code = run_fetch(
            stocks_raw=args.stocks,
            features_raw=args.features,
            days=args.days,
            sector_top_n=args.sector_top_n,
            strict=args.strict,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    if args.format == "pretty":
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

