#!/usr/bin/env python3
"""
Kalshi Market Scanner
Connects to the Kalshi API, filters markets by volume/expiry/price-move,
and outputs a ranked shortlist of trading opportunities.
"""

import argparse
import base64
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_DIR / "scanner.log"),
    ],
)
log = logging.getLogger(__name__)


def load_private_key():
    key_path = config.KALSHI_PRIVATE_KEY_PATH
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def make_headers(method: str, path: str, private_key) -> dict:
    ts_ms = str(int(time.time() * 1000))
    message = (ts_ms + method.upper() + path).encode("utf-8")
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
    }


def get(path: str, params: dict, private_key, retries: int = 1):
    url = config.KALSHI_BASE_URL + path
    headers = make_headers("GET", path, private_key)
    for attempt in range(retries + 1):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.ok:
            return resp.json()
        log.error("GET %s → %d: %s", path, resp.status_code, resp.text[:200])
        if attempt < retries:
            log.info("Retrying in 5s...")
            time.sleep(5)
    return None


RELAXED_MARKET_LIMIT = 2000

CATEGORY_TICKERS = [
    "KXBTC", "KXETH", "KXINX", "KXSPY", "KXFED", "KXCPI",
    "KXGDP", "KXNHL", "KXNBA", "KXMLB",
]


def fetch_category_markets(private_key, series_ticker: str) -> list[dict]:
    """Fetch up to 200 open markets for a single series/category ticker."""
    params = {"status": "open", "limit": 200, "series_ticker": series_ticker}
    data = get("/markets", params, private_key)
    if not data:
        log.warning("No data returned for category %s", series_ticker)
        return []
    markets = data.get("markets", [])
    log.info("Category %s: %d markets", series_ticker, len(markets))
    return markets


def fetch_all_markets(private_key, category: str | None, limit: int | None = None) -> list[dict]:
    """Fetch markets across all known categories (or a single one if category is specified)."""
    tickers = [category] if category else CATEGORY_TICKERS
    seen = set()
    markets = []
    for ticker in tickers:
        batch = fetch_category_markets(private_key, ticker)
        for m in batch:
            key = m.get("ticker")
            if key and key not in seen:
                seen.add(key)
                markets.append(m)
        if limit and len(markets) >= limit:
            markets = markets[:limit]
            log.info("Hit market limit of %d — stopping fetch.", limit)
            break
    log.info("Total unique markets fetched across all categories: %d", len(markets))
    return markets


def get_price_24h_ago(ticker: str, series_ticker: str, private_key) -> float | None:
    path = f"/series/{series_ticker}/markets/{ticker}/candlesticks"
    params = {"period_interval": 1440, "limit": 2}
    data = get(path, params, private_key)
    if not data:
        return None
    candles = data.get("candlesticks", [])
    if len(candles) >= 2:
        return candles[-2].get("yes_ask", None)
    return None


def days_to_expiry(close_time_str: str) -> float:
    try:
        close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (close_dt - now).total_seconds() / 86400
    except Exception:
        return float("inf")


def percentile_rank(values: list[float], value: float) -> float:
    below = sum(1 for v in values if v < value)
    return below / max(len(values) - 1, 1)


def relaxed_score(m: dict) -> float:
    """Score for --relaxed mode: no candlestick calls.
    Weights: urgency (closer expiry) 60%, price in 20-80 cent sweet spot 40%.
    Bonus: spread quality (+0.2 weight) when bid/ask spread < 0.10.
    Uses yes_bid_dollars / yes_ask_dollars fields from the live API.
    """
    dte = days_to_expiry(m.get("close_time", ""))
    urgency = 1 / max(dte, 0.1)

    yes_bid = float(m.get("yes_bid_dollars") or 0)
    yes_ask = float(m.get("yes_ask_dollars") or 0)
    mid = yes_bid or yes_ask  # fall back to ask if no bid

    # Triangle function: 1.0 at 0.50, 0.0 at 0.20 or 0.80, 0.0 outside that range
    if 0.20 <= mid <= 0.80:
        price_score = 1.0 - abs(mid - 0.50) / 0.30
    else:
        price_score = 0.0

    # Spread quality bonus: tight markets are more tradeable
    if yes_bid > 0 and yes_ask > 0 and (yes_ask - yes_bid) < 0.10:
        spread_bonus = 0.2
    else:
        spread_bonus = 0.0

    return urgency * 0.6 + price_score * 0.4 + spread_bonus


def scan(category: str | None, min_volume: int, max_days: int, price_move_pct: float, relaxed: bool = False):
    private_key = load_private_key()

    if relaxed:
        min_volume = 0
        max_days = 90
        log.info("--relaxed mode: volume=any, max_days=90, limit=%d, no candlestick calls", RELAXED_MARKET_LIMIT)

    log.info("Fetching markets from %s...", config.KALSHI_BASE_URL)
    fetch_limit = RELAXED_MARKET_LIMIT if relaxed else None
    all_markets = fetch_all_markets(private_key, category, limit=fetch_limit)
    log.info("Total open markets fetched: %d", len(all_markets))

    if all_markets:
        log.info("First market raw fields:\n%s", json.dumps(all_markets[0], indent=2, default=str))

    # --- Filter 1: volume (skipped in relaxed mode) ---
    if min_volume > 0:
        after_volume = [m for m in all_markets if (m.get("volume") or 0) >= min_volume]
        log.info("After volume >= %d: %d markets", min_volume, len(after_volume))
    else:
        after_volume = all_markets
        log.info("Volume filter skipped (min_volume=0)")

    # --- Filter 2: expiry ---
    after_expiry = [
        m for m in after_volume
        if days_to_expiry(m.get("close_time", "")) <= max_days
    ]
    log.info("After days_to_expiry <= %d: %d markets", max_days, len(after_expiry))

    if relaxed:
        # --- Relaxed pre-filters (applied before scoring, no API calls) ---

        # Drop multivariate/parlay markets
        after_expiry = [m for m in after_expiry if "MVE" not in (m.get("ticker") or "")]
        log.info("After dropping MVE tickers: %d markets", len(after_expiry))

        # Drop markets where yes_ask is missing, <= 0.01, or at ceiling (1.00 = dead market)
        def has_pricing(m: dict) -> bool:
            ask = float(m.get("yes_ask_dollars") or 0)
            return 0.01 < ask < 1.0

        after_expiry = [m for m in after_expiry if has_pricing(m)]
        log.info("After dropping zero-priced markets: %d markets", len(after_expiry))

        # --- Relaxed ranking: score purely on urgency + price attractiveness, no API calls ---
        pool = after_expiry
        for m in pool:
            yes_bid = float(m.get("yes_bid_dollars") or m.get("yes_ask_dollars") or 0)
            m["yes_price"] = yes_bid
            m["price_move_pct"] = None
            m["direction"] = "n/a"
            m["days_to_expiry"] = round(days_to_expiry(m.get("close_time", "")), 1)
            m["score"] = round(relaxed_score(m), 4)
        pool.sort(key=lambda m: m["score"], reverse=True)
        top10 = pool[:10]
        mode_label = f"RELAXED — top 10 of {len(pool)}"
    else:
        # --- Normal mode: filter 3 — price move via candlestick API ---
        flagged = []
        effective_move_pct = price_move_pct
        for m in after_expiry:
            ticker = m.get("ticker", "")
            series_ticker = m.get("series_ticker", "")
            yes_price = m.get("yes_ask") or m.get("last_price") or 0
            prev_price = get_price_24h_ago(ticker, series_ticker, private_key)
            if prev_price is None or prev_price == 0:
                continue
            move = abs(yes_price - prev_price) / prev_price * 100
            if move >= effective_move_pct:
                m["yes_price"] = yes_price
                m["prev_price"] = prev_price
                m["price_move_pct"] = round(move, 2)
                m["direction"] = "up" if yes_price > prev_price else "down"
                flagged.append(m)

        log.info("After price move >= %.1f%%: %d markets", effective_move_pct, len(flagged))

        if len(flagged) < 3:
            relaxed_threshold = price_move_pct / 2
            log.warning(
                "Fewer than 3 markets passed filters. Relaxing price-move threshold to %.1f%%.",
                relaxed_threshold,
            )
            for m in after_expiry:
                if m in flagged:
                    continue
                ticker = m.get("ticker", "")
                series_ticker = m.get("series_ticker", "")
                yes_price = m.get("yes_ask") or m.get("last_price") or 0
                prev_price = get_price_24h_ago(ticker, series_ticker, private_key)
                if prev_price is None or prev_price == 0:
                    continue
                move = abs(yes_price - prev_price) / prev_price * 100
                if move >= relaxed_threshold:
                    m["yes_price"] = yes_price
                    m["prev_price"] = prev_price
                    m["price_move_pct"] = round(move, 2)
                    m["direction"] = "up" if yes_price > prev_price else "down"
                    flagged.append(m)
            effective_move_pct = relaxed_threshold

        pool = flagged
        volumes = [m.get("volume", 0) for m in pool]
        days_list = [days_to_expiry(m.get("close_time", "")) for m in pool]

        for m in pool:
            vol_rank = percentile_rank(volumes, m.get("volume", 0))
            urgency_rank = percentile_rank(
                [1 / max(d, 0.1) for d in days_list],
                1 / max(days_to_expiry(m.get("close_time", "")), 0.1),
            )
            m["score"] = round(
                m["price_move_pct"] / 100 * 0.5 + vol_rank * 0.3 + urgency_rank * 0.2, 4
            )
            m["days_to_expiry"] = round(days_to_expiry(m.get("close_time", "")), 1)

        pool.sort(key=lambda m: m["score"], reverse=True)
        top10 = pool[:10]
        mode_label = f"{len(flagged)} markets flagged"

    # --- Output ---
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = config.LOG_DIR / f"scan_{now_str}.json"
    records = []
    for m in top10:
        yes_price = m.get("yes_price") or 0
        records.append({
            "ticker": m.get("ticker"),
            "title": m.get("title"),
            "yes_price": yes_price,
            "no_price": round(1 - yes_price, 4),
            "volume": m.get("volume"),
            "days_to_expiry": m.get("days_to_expiry"),
            "price_move_pct": m.get("price_move_pct"),
            "direction": m.get("direction"),
            "score": m.get("score"),
            "close_time": m.get("close_time"),
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        })

    with open(out_path, "w") as f:
        json.dump(records, f, indent=2)
    log.info("Results saved to %s", out_path)

    # --- Print markdown table ---
    header = f"{'Rank':<5} {'Ticker':<30} {'Yes $':<7} {'Move':>8} {'Volume':>8} {'Days':>5} {'Score':>6}"
    print("\n" + "=" * len(header))
    print(f"Kalshi Scanner Results  |  {now_str}  |  {mode_label}")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for i, r in enumerate(records, 1):
        direction = r.get("direction", "n/a")
        move_pct = r.get("price_move_pct")
        if move_pct is None:
            move_str = "n/a"
        elif direction == "up":
            move_str = f"+{move_pct:.1f}%"
        elif direction == "down":
            move_str = f"-{move_pct:.1f}%"
        else:
            move_str = f"{move_pct:.1f}%"
        yes_price = r.get("yes_price") or 0
        volume = r.get("volume") or 0
        print(
            f"{i:<5} {r['ticker']:<30} {yes_price:<7.2f} {move_str:>8} "
            f"{volume:>8} {r['days_to_expiry']:>5} {r['score']:>6.3f}"
        )
    print("=" * len(header) + "\n")

    return records


def main():
    parser = argparse.ArgumentParser(description="Scan Kalshi markets for opportunities.")
    parser.add_argument("--category", default=None, help="Series ticker to restrict scan")
    parser.add_argument("--min-volume", type=int, default=config.SCANNER_MIN_VOLUME)
    parser.add_argument("--max-days", type=int, default=config.SCANNER_MAX_DAYS_TO_EXPIRY)
    parser.add_argument("--price-move", type=float, default=config.SCANNER_PRICE_MOVE_PCT)
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Demo-friendly mode: skip volume filter, extend expiry to 90 days, "
             "lower price-move to 1%%, and show top 10 by score regardless of filters.",
    )
    args = parser.parse_args()

    scan(
        category=args.category,
        min_volume=args.min_volume,
        max_days=args.max_days,
        price_move_pct=args.price_move,
        relaxed=args.relaxed,
    )


if __name__ == "__main__":
    main()
