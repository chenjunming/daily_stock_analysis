# Trigger and Command Examples

## Chinese Prompt Examples

1. `帮我获取 600519 和 000001 最近30天日线+实时+筹码，并带上指数和板块排行`
2. `拉取 AAPL 的行情，返回 JSON，失败也要给出错误字段`
3. `给我市场统计和板块涨跌榜，不用个股`

## English Prompt Examples

1. `Fetch daily candles and realtime quote for 600519,000001 with market breadth.`
2. `Get AAPL daily/realtime/chip data in JSON and keep partial failures.`
3. `Fetch indices, market stats, and sector rankings only.`

## Command Examples

1. Full stock + market:
```bash
python scripts/fetch_market_data.py \
  --stocks 600519,000001 \
  --features daily,realtime,chip,name,indices,market_stats,sector_rankings \
  --days 30 \
  --sector-top-n 5 \
  --format json
```

2. Market only:
```bash
python scripts/fetch_market_data.py \
  --features indices,market_stats,sector_rankings \
  --sector-top-n 8 \
  --format pretty
```

3. Strict mode:
```bash
python scripts/fetch_market_data.py \
  --stocks 600519,INVALID \
  --features daily,realtime,name \
  --strict \
  --format json
```
