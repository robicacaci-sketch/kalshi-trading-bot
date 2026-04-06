# Skill: Kalshi Market Scanner

## Purpose

Scan all active Kalshi prediction markets, apply filters to surface the most tradeable opportunities, and output a ranked shortlist for further research.

---

## Inputs

- Kalshi REST API access (base URL and auth headers from `config.py`)
- Optional: a category slug to restrict the scan (e.g. `"politics"`, `"economics"`)
- Optional: override thresholds for volume, expiry, or price-move cutoffs

## Filters (apply in order)

### 1. Status filter
Discard any market where `status != "open"`. Only actively trading markets are relevant.

### 2. Volume filter
Keep markets where `volume >= 200` (open contracts, not dollar volume). This ensures there is enough liquidity to enter and exit a position without excessive slippage.

### 3. Expiry filter
Keep markets where the number of calendar days between today and `close_time` is `<= 30`. Markets resolving within 30 days have the most actionable near-term signals; longer-dated markets are harder to price and tie up capital.

### 4. Price-move filter
For each surviving market, compare `last_price` to `previous_price` (the price 24 hours ago, or the earliest available if less than 24 hours old).

Compute:
```
price_move_pct = abs(last_price - previous_price) / previous_price * 100
```

Flag the market if `price_move_pct >= 10`. A double-digit intraday move signals new information has entered the market and a mispricing may exist while the crowd catches up.

---

## API Calls

### List markets
```
GET {KALSHI_BASE_URL}/markets
```
Query parameters:
- `status=open`
- `limit=200` (page through with `cursor` if more exist)
- `series_ticker` (optional — pass to restrict by category)

### Get a single market (for price history)
```
GET {KALSHI_BASE_URL}/markets/{ticker}
```

### Get candlestick / price history
```
GET {KALSHI_BASE_URL}/series/{series_ticker}/markets/{ticker}/candlesticks
```
Use `period_interval=1440` (1440 minutes = 1 day) and limit to the last 2 candles to get yesterday's close and today's current price.

---

## Authentication

Kalshi's API uses RSA-signed JWT authentication. For every request:
1. Build the message: `timestamp + method + path` (no query string in the path).
2. Sign with the RSA private key at `KALSHI_PRIVATE_KEY_PATH` using PKCS#1 v1.5 + SHA-256.
3. Set headers:
   ```
   KALSHI-ACCESS-KEY: {KALSHI_API_KEY_ID}
   KALSHI-ACCESS-TIMESTAMP: {unix_ms_timestamp}
   KALSHI-ACCESS-SIGNATURE: {base64_signature}
   ```

---

## Ranking

After filtering, rank surviving markets by a composite score:

```
score = (price_move_pct * 0.5) + (volume_rank * 0.3) + (urgency_rank * 0.2)
```

Where:
- `volume_rank` = percentile rank of `volume` within the filtered set (higher = better)
- `urgency_rank` = percentile rank of `(1 / days_to_expiry)` (closer expiry = higher rank)

Sort descending by `score`. Present the top 10.

---

## Output Format

Emit a markdown table followed by a JSON array saved to `data/logs/scan_{YYYYMMDD_HHMMSS}.json`.

### Markdown table (stdout)
```
| Rank | Ticker | Title | Yes Price | Price Move | Volume | Days Left | Score |
|------|--------|-------|-----------|------------|--------|-----------|-------|
|  1   | ...    | ...   |  0.63     |  +14.2 %   |  8420  |    7      | 0.87  |
```

### JSON record per market
```json
{
  "ticker": "INXD-24DEC31-T4500",
  "title": "Will S&P 500 close above 4500 on Dec 31?",
  "yes_price": 0.63,
  "no_price": 0.37,
  "volume": 8420,
  "days_to_expiry": 7,
  "price_move_pct": 14.2,
  "direction": "up",
  "score": 0.87,
  "close_time": "2024-12-31T21:00:00Z",
  "scanned_at": "2024-12-24T14:32:00Z"
}
```

---

## Error Handling

- If the API returns a non-2xx response, log the status code and response body to `data/logs/errors.log` and retry once after 5 seconds.
- If fewer than 3 markets survive all filters, widen the price-move threshold to 5% and note the relaxation in output.
- Never crash silently — always surface filter counts so the operator can tune thresholds.

---

## Example invocation

```bash
python scripts/scanner.py
python scripts/scanner.py --category politics --min-volume 500
```
