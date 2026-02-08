---
name: stock-data-fetcher
description: Fetch stock and market data using a unified pipeline for AI-triggered workflows. Use for requests such as 获取行情、拉取日线、实时价格、筹码分布、市场统计、板块排行, or fetch market data, daily candles, realtime quote, chip distribution, market breadth, sector rankings.
---

# Stock Data Fetcher

Use this skill to fetch stock-level and market-level data through one stable command and return machine-readable JSON.

## Execute

1. Build the request:
- Collect `stocks` (comma-separated stock codes, example: `600519,000001,AAPL`) when stock features are requested.
- Select `features` from:
  `daily,realtime,chip,name,indices,market_stats,sector_rankings`
- Optional tuning:
  `--days` (default `30`), `--sector-top-n` (default `5`), `--strict`, `--format json|pretty`.

2. Validate before running:
- If any stock feature (`daily,realtime,chip,name`) is requested, `stocks` must be non-empty.
- Reject unknown features.
- Require `days > 0` and `sector-top-n > 0`.

3. Run:
```bash
python scripts/fetch_market_data.py \
  --stocks 600519,000001 \
  --features daily,realtime,chip,name,indices,market_stats,sector_rankings \
  --days 30 \
  --sector-top-n 5 \
  --format json
```

4. Parse output:
- Read `meta` first to determine `partial_failure` or `all_failed`.
- Read per-stock results in `stocks[code]`.
- Read market-wide results in `market`.
- When `errors` exist, continue with available fields as degraded output.

## Output Contract

See `references/schema.md` for JSON schema and compatibility rules.
See `references/examples.md` for prompt and command examples.
