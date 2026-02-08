# Stock Data Fetcher Package Integration

This project now exposes the data fetch capability in reusable forms:

1. Python package API
2. CLI entrypoint
3. MCP server entrypoint

## Install

```bash
pip install -e .
```

For MCP support:

```bash
pip install -e '.[mcp]'
```

## Python API

```python
from stock_data_fetcher import run_fetch

result, exit_code = run_fetch(
    stocks_raw="600519,000001",
    features_raw="daily,realtime,name,indices",
    days=30,
    sector_top_n=5,
    strict=False,
)
```

## CLI

```bash
fetch-market-data --stocks 600519,000001 --features daily,realtime,name --format json
```

Backward-compatible repo script:

```bash
python scripts/fetch_market_data.py --stocks 600519 --features daily,realtime --format json
```

## MCP Server

Start server:

```bash
stock-data-fetcher-mcp
```

Exposed tool: `fetch_market_data`

Arguments:
- `stocks`: comma-separated stock codes
- `features`: subset of `daily,realtime,chip,name,indices,market_stats,sector_rankings`
- `days`: integer, default `30`
- `sector_top_n`: integer, default `5`
- `strict`: boolean

Tool output:
- `ok`: boolean
- `exit_code`: integer
- `data`: same JSON contract as the CLI output
