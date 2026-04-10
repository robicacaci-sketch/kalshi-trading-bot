#!/usr/bin/env python3
"""
Kalshi Market Researcher
Fetches a market by ticker, searches the web for current context,
and uses Claude to estimate the true probability vs the market price.

Usage:
    python3 scripts/researcher.py KXBTC-26APR0517-B67375
"""

import argparse
import base64
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import anthropic
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
        logging.FileHandler(config.LOG_DIR / "researcher.log"),
    ],
)
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert prediction market analyst. You will be given details about a Kalshi \
prediction market and web search results containing current real-world information.

Your job is to estimate the true probability that the market resolves YES.

Follow this process:
1. Read the market question and resolution criteria carefully.
2. Review the web search results for relevant current data.
3. Reason step by step about the probability of YES resolution.
4. Compare your estimated probability to the current market price.
5. Calculate edge = your_probability - market_price (both as percentages, e.g. 63.0).

Classify the edge as:
- STRONG if abs(edge) > 8%
- MODERATE if abs(edge) is 4–8%
- MARGINAL if abs(edge) is 1–4%
- NO_EDGE if abs(edge) < 1%

Output ONLY a single valid JSON object — no markdown fences, no extra text — \
matching this exact schema:
{
  "ticker": "<string>",
  "question": "<string>",
  "market_price": <float, percent 0-100>,
  "estimated_probability": <float, percent 0-100>,
  "edge": <float, your_probability minus market_price>,
  "edge_classification": "STRONG" | "MODERATE" | "MARGINAL" | "NO_EDGE",
  "reasoning": "<string, 3-6 sentences explaining your estimate>",
  "key_factors": ["<string>", ...],
  "risks": ["<string>", ...]
}
"""


# ---------------------------------------------------------------------------
# Kalshi API helpers (same pattern as scanner.py)
# ---------------------------------------------------------------------------

def load_private_key():
    with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
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


def fetch_market(ticker: str, private_key) -> dict:
    path = f"/markets/{ticker}"
    headers = make_headers("GET", path, private_key)
    resp = requests.get(config.KALSHI_BASE_URL + path, headers=headers, timeout=15)
    if not resp.ok:
        log.error("Failed to fetch market %s: %d %s", ticker, resp.status_code, resp.text[:300])
        sys.exit(1)
    market = resp.json().get("market") or resp.json()
    log.info("Fetched market: %s", market.get("title") or ticker)
    return market


# ---------------------------------------------------------------------------
# Search query builder
# ---------------------------------------------------------------------------

def build_search_query(market: dict) -> str:
    """Derive a useful web search query from the market title and ticker."""
    title = market.get("title") or ""
    ticker = market.get("ticker") or ""

    # Map known series prefixes to useful search context
    prefix_hints = {
        "KXBTC":   "Bitcoin BTC price today",
        "KXETH":   "Ethereum ETH price today",
        "KXINX":   "S&P 500 index today",
        "KXSPY":   "SPY ETF price today",
        "KXFED":   "Federal Reserve interest rate decision",
        "KXCPI":   "US CPI inflation report",
        "KXGDP":   "US GDP report",
        "KXNHL":   "NHL game result today",
        "KXNBA":   "NBA game result today",
        "KXMLB":   "MLB game result today",
    }

    series = ticker.split("-")[0] if "-" in ticker else ticker
    hint = prefix_hints.get(series, "")

    if hint:
        return f"{hint} {title}"
    return title


# ---------------------------------------------------------------------------
# RSS headline fetcher
# ---------------------------------------------------------------------------

_RSS_FEEDS: dict[str, list[str]] = {
    "KXCPI": [
        "https://www.bls.gov/feed/bls_latest.rss",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ],
    "KXFED": [
        "https://www.bls.gov/feed/bls_latest.rss",
        "https://www.federalreserve.gov/feeds/press_all.xml",
    ],
    "KXGDP": [
        "https://apps.bea.gov/rss/rss.xml",
        "https://www.bls.gov/feed/bls_latest.rss",
    ],
    "KXINX": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.aspx",
    ],
    "KXSPY": [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.aspx",
    ],
    "KXBTC": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ],
    "KXETH": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
    ],
}

_DEFAULT_FEEDS = [
    "https://www.bls.gov/feed/bls_latest.rss",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.aspx",
]


def _fetch_one_feed(url: str, max_items: int = 5) -> list[str]:
    """Fetch a single RSS/Atom feed and return up to max_items headline strings."""
    resp = requests.get(url, timeout=5, headers={"User-Agent": "kalshi-bot/1.0"})
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    # Strip XML namespaces from tags for uniform handling
    def detag(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    headlines = []
    # RSS <item> elements
    for item in root.iter():
        if detag(item.tag) != "item":
            continue
        children = {detag(c.tag): (c.text or "").strip() for c in item}
        title = children.get("title", "")
        desc  = children.get("description", "")
        if title:
            text = title if not desc else f"{title} — {desc[:120]}"
            headlines.append(text)
        if len(headlines) >= max_items:
            break

    # Atom <entry> elements (e.g. Fed Reserve feed)
    if not headlines:
        for entry in root.iter():
            if detag(entry.tag) != "entry":
                continue
            children = {detag(c.tag): (c.text or "").strip() for c in entry}
            title   = children.get("title", "")
            summary = children.get("summary", "")
            if title:
                text = title if not summary else f"{title} — {summary[:120]}"
                headlines.append(text)
            if len(headlines) >= max_items:
                break

    return headlines


def fetch_rss_headlines(ticker: str, title: str) -> str:
    """
    Fetch recent headlines from RSS feeds relevant to the market's series.
    Returns a formatted string ready to prepend to the Claude prompt.
    Falls back silently on any individual feed error.
    Total wall-clock budget: 10 seconds.
    """
    series = ticker.split("-")[0] if "-" in ticker else ticker
    urls = _RSS_FEEDS.get(series, _DEFAULT_FEEDS)

    all_headlines: list[str] = []

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {pool.submit(_fetch_one_feed, url): url for url in urls}
        for future in as_completed(futures, timeout=10):
            url = futures[future]
            try:
                items = future.result()
                all_headlines.extend(items)
            except Exception as exc:
                log.debug("RSS feed %s failed (skipping): %s", url, exc)

    if not all_headlines:
        return ""

    lines = "\n".join(f"- {h}" for h in all_headlines[:10])
    return f"Recent news headlines:\n{lines}"


# ---------------------------------------------------------------------------
# Core research function
# ---------------------------------------------------------------------------

def research(ticker: str) -> dict:
    private_key = load_private_key()
    market = fetch_market(ticker, private_key)

    # Pull the yes ask price as the market-implied probability (0–100 scale)
    yes_ask = float(market.get("yes_ask_dollars") or market.get("yes_ask") or 0)
    market_price_pct = round(yes_ask * 100, 2)

    title = market.get("title") or ticker
    subtitle = market.get("subtitle") or ""
    rules = market.get("rules_primary") or market.get("rules") or ""
    close_time = market.get("close_time") or ""

    search_query = build_search_query(market)
    log.info("Web search query: %s", search_query)

    # Fetch RSS headlines for additional real-time context
    rss_context = fetch_rss_headlines(ticker, title)
    headline_count = rss_context.count("\n-") if rss_context else 0
    log.info("Fetched %d RSS headlines for %s", headline_count, ticker)

    # Build the user message with market context
    market_context = f"""\
Market ticker : {ticker}
Question      : {title}
Subtitle      : {subtitle}
Resolution    : {rules}
Closes        : {close_time}
Market price  : {market_price_pct}% (yes ask ${yes_ask:.4f})
Today's date  : {datetime.now(timezone.utc).strftime("%Y-%m-%d")}
"""

    # Call Claude with web_search tool enabled
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    log.info("Sending to Claude with web search...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Here are the market details:\n\n{market_context}\n\n"
                    + (f"{rss_context}\n\n" if rss_context else "")
                    + f"Please search the web for: {search_query}\n\n"
                    "Then analyze the market and return your JSON research brief."
                ),
            }
        ],
    )

    # Collect all text from the response into one string
    full_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    # Check for closed/expired market before attempting JSON parse
    expired_signals = [
        "already closed", "already resolved", "already expired",
        "market has closed", "market has resolved", "market has expired",
        "no longer active", "this market is closed",
    ]
    if any(signal in full_text.lower() for signal in expired_signals):
        log.warning("Claude indicates this market is closed or expired: %s", ticker)
        log.warning("Claude response: %s", full_text[:300])
        sys.exit(0)

    # Step 1: prefer the last text block that is pure JSON
    raw_json = None
    for block in reversed(response.content):
        if hasattr(block, "text") and block.text.strip().startswith("{"):
            raw_json = block.text.strip()
            break

    # Step 2: regex fallback — find outermost { ... } anywhere in the full text
    if not raw_json:
        match = re.search(r"\{.*\}", full_text, re.DOTALL)
        if match:
            raw_json = match.group(0)

    if not raw_json:
        log.error("Could not find JSON in Claude response:\n%s", full_text[:800])
        sys.exit(1)

    try:
        brief = json.loads(raw_json)
    except json.JSONDecodeError:
        # Last resort: trim any trailing text after the final closing brace
        last_brace = raw_json.rfind("}")
        if last_brace != -1:
            try:
                brief = json.loads(raw_json[: last_brace + 1])
            except json.JSONDecodeError as e:
                log.error("Claude returned invalid JSON: %s\nRaw output:\n%s", e, raw_json[:800])
                sys.exit(1)
        else:
            log.error("Claude returned invalid JSON (no closing brace):\n%s", raw_json[:800])
            sys.exit(1)

    # Ensure numeric fields are present and consistent
    brief.setdefault("ticker", ticker)
    brief.setdefault("market_price", market_price_pct)

    # ---------------------------------------------------------------------------
    # Save to file
    # ---------------------------------------------------------------------------
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_ticker = ticker.replace("/", "-")
    out_path = config.LOG_DIR / f"research_{safe_ticker}_{now_str}.json"
    with open(out_path, "w") as f:
        json.dump(brief, f, indent=2)
    log.info("Brief saved to %s", out_path)

    # ---------------------------------------------------------------------------
    # Print formatted summary
    # ---------------------------------------------------------------------------
    edge = brief.get("edge", 0)
    edge_cls = brief.get("edge_classification", "UNKNOWN")
    est_prob = brief.get("estimated_probability", 0)

    edge_colors = {
        "STRONG":   "\033[92m",   # green
        "MODERATE": "\033[93m",   # yellow
        "MARGINAL": "\033[94m",   # blue
        "NO_EDGE":  "\033[90m",   # grey
    }
    reset = "\033[0m"
    color = edge_colors.get(edge_cls, "")

    sep = "=" * 68
    print(f"\n{sep}")
    print(f"  {ticker}")
    print(f"  {title}")
    print(sep)
    print(f"  Market price      : {market_price_pct:.1f}%")
    print(f"  Estimated prob    : {est_prob:.1f}%")
    print(f"  Edge              : {color}{edge:+.1f}%  [{edge_cls}]{reset}")
    print()
    print(f"  Reasoning:")
    for line in brief.get("reasoning", "").split(". "):
        line = line.strip()
        if line:
            print(f"    • {line.rstrip('.')}.")
    print()
    print(f"  Key factors:")
    for factor in brief.get("key_factors", []):
        print(f"    + {factor}")
    print()
    print(f"  Risks:")
    for risk in brief.get("risks", []):
        print(f"    - {risk}")
    print(f"{sep}\n")

    return brief


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Research a Kalshi market and estimate true probability via Claude."
    )
    parser.add_argument("ticker", help="Kalshi market ticker, e.g. KXBTC-26APR0517-B67375")
    args = parser.parse_args()

    research(args.ticker.upper())


if __name__ == "__main__":
    main()
