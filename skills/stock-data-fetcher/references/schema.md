# JSON Schema Contract

## Request CLI

Command:

```bash
python scripts/fetch_market_data.py \
  --stocks <comma-separated-codes> \
  --days <int> \
  --features <comma-separated-features> \
  --sector-top-n <int> \
  --format json|pretty \
  [--strict]
```

Supported features:
- `daily`
- `realtime`
- `chip`
- `name`
- `indices`
- `market_stats`
- `sector_rankings`

Exit codes:
- `0`: success (partial success allowed unless `--strict`)
- `2`: argument validation error
- `3`: all attempts failed, or strict mode with any stock-feature failure

## Response Shape

```json
{
  "meta": {
    "started_at": "2026-02-08T12:00:00.000000Z",
    "duration_ms": 1234,
    "stocks": ["600519", "AAPL"],
    "days": 30,
    "features": ["daily", "realtime", "indices"],
    "sector_top_n": 5,
    "strict": false,
    "partial_failure": false,
    "all_failed": false
  },
  "stocks": {
    "600519": {
      "daily": {
        "source": "AkshareFetcher",
        "rows": 30,
        "data": []
      },
      "realtime": {},
      "chip": {},
      "name": "贵州茅台",
      "errors": []
    }
  },
  "market": {
    "indices": [],
    "market_stats": {},
    "sector_rankings": {
      "top": [],
      "bottom": [],
      "n": 5
    },
    "errors": []
  }
}
```

## Compatibility Rules

1. Keep top-level keys stable: `meta`, `stocks`, `market`.
2. New fields may be appended but existing fields should not be renamed.
3. `stocks[code].errors` and `market.errors` are append-only error channels.
4. Consumers must tolerate `null` or missing data for failed features.
