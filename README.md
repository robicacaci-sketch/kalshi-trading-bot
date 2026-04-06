# Kalshi Trading Bot

A Claude-powered toolkit for scanning and researching Kalshi prediction market opportunities.

## Structure

```
kalshi-trading-bot/
├── config.py              # Loads env vars, sets base URL
├── scripts/
│   ├── scanner.py         # Fetches markets, applies filters, ranks opportunities
│   └── researcher.py      # Sends flagged markets to Claude for probability analysis
├── skills/
│   ├── scanner/SKILL.md   # Detailed instructions for the scanner skill
│   └── researcher/SKILL.md
├── data/logs/             # Scan and research JSON outputs + log files
├── .env.example
└── requirements.txt
```

## Setup

```bash
cp .env.example .env
# Fill in .env with your credentials

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

### Scan for opportunities

```bash
python scripts/scanner.py
python scripts/scanner.py --category politics --min-volume 500 --max-days 14
```

Outputs a ranked markdown table and saves results to `data/logs/scan_YYYYMMDD_HHMMSS.json`.

### Research a specific market

```bash
python scripts/researcher.py --ticker INXD-24DEC31-T4500
```

### Research all markets from a scan

```bash
python scripts/researcher.py --file data/logs/scan_20241224_143200.json
```

## Configuration

| Variable | Description |
|---|---|
| `KALSHI_API_KEY_ID` | Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | Path to your RSA private key `.pem` file |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `KALSHI_ENV` | `demo` (default) or `live` |

## Scanner filters

1. **Status** — only `open` markets
2. **Volume** — `>= 200` contracts (configurable via `--min-volume`)
3. **Expiry** — `<= 30` days to close (configurable via `--max-days`)
4. **Price move** — `>= 10%` in 24 hours (configurable via `--price-move`)

Markets are ranked by a composite score weighting price move (50%), volume (30%), and urgency (20%).

## Researcher output

For each market, Claude produces:
- Resolution rules (exact criteria and adjudicator)
- Base rate from historical reference class
- Evidence update (each piece with direction and magnitude)
- Independent probability estimate with 90% CI
- Edge vs. market price, classified as marginal / moderate / strong
- Monitoring triggers (what to watch before resolution)
