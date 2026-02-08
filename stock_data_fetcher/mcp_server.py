from __future__ import annotations

from typing import Any, Dict

from .core import ALLOWED_FEATURES, run_fetch


def _feature_help() -> str:
    return ",".join(ALLOWED_FEATURES)


def _run_fetch_tool(
    stocks: str = "",
    features: str = _feature_help(),
    days: int = 30,
    sector_top_n: int = 5,
    strict: bool = False,
) -> Dict[str, Any]:
    try:
        result, exit_code = run_fetch(
            stocks_raw=stocks,
            features_raw=features,
            days=days,
            sector_top_n=sector_top_n,
            strict=strict,
        )
        return {"ok": exit_code == 0, "exit_code": exit_code, "data": result}
    except ValueError as exc:
        return {"ok": False, "exit_code": 2, "error": str(exc)}


def main() -> int:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print(
            "Missing dependency: install with `pip install '.[mcp]'` or `pip install mcp`.",
            flush=True,
        )
        return 1

    app = FastMCP("stock-data-fetcher")

    @app.tool(
        name="fetch_market_data",
        description=(
            "Fetch stock and market data. "
            "Features: daily,realtime,chip,name,indices,market_stats,sector_rankings."
        ),
    )
    def fetch_market_data(
        stocks: str = "",
        features: str = _feature_help(),
        days: int = 30,
        sector_top_n: int = 5,
        strict: bool = False,
    ) -> Dict[str, Any]:
        return _run_fetch_tool(stocks, features, days, sector_top_n, strict)

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

