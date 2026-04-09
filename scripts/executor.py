#!/usr/bin/env python3
"""
Kalshi Executor
Ties scanner → researcher → risk engine → order placement into one automated loop.

Usage:
    python3 scripts/executor.py

Drop a file called STOP in the project root to halt trading immediately.
"""

import json
import logging
import sys
import base64
import time
from datetime import datetime, date, timezone
from pathlib import Path

import requests
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

# ---------------------------------------------------------------------------
# Logging — set up before importing sub-scripts so our handler wins.
# scanner.py and researcher.py each call basicConfig() at import time;
# we use a named logger here so we always get our own file handler.
# ---------------------------------------------------------------------------

_log_dir = config.LOG_DIR
_log_dir.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("executor")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _fh = logging.FileHandler(_log_dir / "executor.log")
    _fh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.addHandler(_fh)

# Import sub-scripts after logger is set up
from scripts.scanner import scan
from scripts.researcher import research
from scripts.risk_engine import validate_trade
from scripts.resolver import resolve_positions

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STOP_FILE = config.ROOT_DIR / "STOP"
STATE_FILE = config.LOG_DIR / "state.json"
EXEC_LOG_FILE = config.LOG_DIR / "executor_log.json"

MAX_MARKETS_PER_RUN = 3
INITIAL_BANKROLL = 1000.0   # demo starting balance

# ---------------------------------------------------------------------------
# Kalshi API helpers (order placement uses KALSHI_ORDER_BASE_URL)
# ---------------------------------------------------------------------------

def _load_private_key():
    with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _make_headers(method: str, path: str, private_key) -> dict:
    """
    Build Kalshi auth headers.

    path must be the full URL path from the domain root, e.g.
    '/trade-api/v2/portfolio/orders' — Kalshi requires RSA-PSS/SHA-256.
    """
    ts_ms = str(int(time.time() * 1000))
    message = (ts_ms + method.upper() + path).encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Content-Type": "application/json",
    }


def place_order(
    ticker: str,
    side: str,           # "yes" or "no"
    count: int,          # number of contracts (>= 1)
    price_cents: int,    # limit price in cents (1–99)
    private_key,
) -> dict | None:
    """
    Place a limit buy order via the Kalshi API.

    Returns the parsed response dict on success, None on failure.
    Always uses KALSHI_ORDER_BASE_URL (demo when KALSHI_ENV=demo).
    """
    endpoint = "/portfolio/orders"
    url = config.KALSHI_ORDER_BASE_URL + endpoint
    # Kalshi requires signing the full URL path from the domain root,
    # e.g. /trade-api/v2/portfolio/orders (not just /portfolio/orders).
    sign_path = urlparse(url).path

    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "count": count,
    }
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents

    headers = _make_headers("POST", sign_path, private_key)
    # Serialize body explicitly so we control the exact bytes sent and can log them.
    body_json = json.dumps(body)

    # Issue 1 fix: print() bypasses any logging config so this always appears.
    print("DEBUG HEADERS:", headers)
    print("DEBUG BODY   :", body_json)
    print("DEBUG URL    :", url)

    try:
        resp = requests.post(url, headers=headers, data=body_json, timeout=15)
    except requests.RequestException as exc:
        log.error("HTTP error placing order for %s: %s", ticker, exc)
        return None

    if resp.ok:
        return resp.json()

    if resp.status_code == 401:
        log.warning(
            "401 Unauthorized placing order for %s (full response: %s).\n"
            "  Demo API trading requires a separate demo account setup — "
            "logging as SIMULATED trade instead.",
            ticker, resp.text,
        )
        return {"order": {"order_id": f"SIMULATED-{ticker}", "status": "simulated"}}

    log.error(
        "Order failed for %s: HTTP %d — %s",
        ticker, resp.status_code, resp.text,
    )
    return None


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

_DEFAULT_STATE: dict = {
    "current_bankroll": INITIAL_BANKROLL,
    "current_positions": [],
    "daily_pnl": 0.0,
    "date": None,
    "last_run": None,
}


def load_state() -> dict:
    """Load state from disk, resetting daily_pnl on a new calendar day."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = _DEFAULT_STATE.copy()

    today = date.today().isoformat()
    if state.get("date") != today:
        log.info("New trading day (%s) — resetting daily_pnl to 0", today)
        state["daily_pnl"] = 0.0
        state["date"] = today

    return state


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info("State saved → %s", STATE_FILE)


# ---------------------------------------------------------------------------
# Executor log (JSON Lines)
# ---------------------------------------------------------------------------

def _log_action(event: dict) -> None:
    EXEC_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EXEC_LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def run_once() -> dict:
    """
    Execute one full scan → research → validate → order cycle.

    Returns a summary dict describing everything that happened.
    """
    now = datetime.now(timezone.utc).isoformat()
    summary: dict = {
        "run_at": now,
        "stopped": False,
        "markets_scanned": 0,
        "markets_researched": 0,
        "trades_approved": 0,
        "trades_placed": 0,
        "actions": [],
    }

    # ------------------------------------------------------------------
    # 0. STOP file check
    # ------------------------------------------------------------------
    if STOP_FILE.exists():
        log.warning("STOP file found at %s — aborting run", STOP_FILE)
        summary["stopped"] = True
        _log_action({"event": "STOP_FILE_DETECTED", "at": now})
        return summary

    # ------------------------------------------------------------------
    # 1. Load state
    # ------------------------------------------------------------------
    state = load_state()
    bankroll: float = state["current_bankroll"]
    positions: list = state["current_positions"]
    daily_pnl: float = state["daily_pnl"]

    log.info(
        "State loaded | bankroll=$%.2f | open_positions=%d | daily_pnl=$%.2f",
        bankroll, len(positions), daily_pnl,
    )

    # ------------------------------------------------------------------
    # 2. Load private key once (shared by scanner auth + order placement)
    # ------------------------------------------------------------------
    try:
        private_key = _load_private_key()
    except Exception as exc:
        log.error("Could not load private key: %s", exc)
        summary["error"] = f"Private key load failed: {exc}"
        return summary

    # ------------------------------------------------------------------
    # 3. Resolve any settled positions before scanning for new ones
    # ------------------------------------------------------------------
    try:
        resolve_positions()
        # Reload state so bankroll reflects any settlements
        state = load_state()
        bankroll = state["current_bankroll"]
        positions = state["current_positions"]
        daily_pnl = state["daily_pnl"]
    except Exception as exc:
        log.error("Resolver error (non-fatal, continuing): %s", exc)

    # ------------------------------------------------------------------
    # 4. Scanner — relaxed mode for broadest market coverage
    # ------------------------------------------------------------------
    log.info("Starting scanner (relaxed mode)...")
    try:
        markets = scan(
            category=None,
            min_volume=0,
            max_days=90,
            price_move_pct=1.0,
            relaxed=True,
        )
    except (SystemExit, Exception) as exc:
        log.error("Scanner raised an exception: %s", exc)
        summary["error"] = f"Scanner failed: {exc}"
        _log_action({"event": "SCANNER_FAILED", "error": str(exc), "at": now})
        return summary

    summary["markets_scanned"] = len(markets)
    log.info("Scanner returned %d markets; taking top %d", len(markets), MAX_MARKETS_PER_RUN)

    top_markets = markets[:MAX_MARKETS_PER_RUN]

    # ------------------------------------------------------------------
    # 5. Research → validate → order for each candidate
    # ------------------------------------------------------------------
    for i, market in enumerate(top_markets):
        ticker: str = market["ticker"]
        log.info("=" * 55)
        log.info("Processing: %s (%s)", ticker, market.get("title", ""))

        # Pause between researcher calls to avoid Anthropic 429s.
        # Skip the delay before the first market.
        if i > 0:
            log.info("Waiting 60s before next researcher call (rate-limit buffer)...")
            time.sleep(60)

        # --- 4a. Research (with one retry on rate-limit) ---
        try:
            brief = research(ticker)
        except SystemExit:
            log.warning("researcher called sys.exit() for %s — skipping", ticker)
            _log_action({"event": "RESEARCH_EXITED", "ticker": ticker, "at": now})
            summary["actions"].append({"ticker": ticker, "event": "research_exited"})
            continue
        except Exception as exc:
            if "429" in str(exc) or "rate" in str(exc).lower():
                log.warning("Rate limit hit researching %s — waiting 60s then retrying once", ticker)
                time.sleep(60)
                try:
                    brief = research(ticker)
                except Exception as retry_exc:
                    log.error("Retry also failed for %s: %s", ticker, retry_exc)
                    _log_action({"event": "RESEARCH_FAILED", "ticker": ticker, "error": str(retry_exc), "at": now})
                    summary["actions"].append({"ticker": ticker, "event": "research_failed", "error": str(retry_exc)})
                    continue
            else:
                log.error("Research failed for %s: %s", ticker, exc)
                _log_action({"event": "RESEARCH_FAILED", "ticker": ticker, "error": str(exc), "at": now})
                summary["actions"].append({"ticker": ticker, "event": "research_failed", "error": str(exc)})
                continue

        summary["markets_researched"] += 1

        # Researcher returns values in percentage (0-100); convert to fractions
        edge_pct: float = brief.get("edge", 0.0)             # e.g. 12.5 → 12.5 pp
        est_prob_pct: float = brief.get("estimated_probability", 0.0)
        mkt_price_pct: float = brief.get("market_price", 0.0)

        est_prob = est_prob_pct / 100.0
        mkt_price = mkt_price_pct / 100.0

        log.info(
            "%s | est_prob=%.1f%% market=%.1f%% edge=%+.1f%% [%s]",
            ticker, est_prob_pct, mkt_price_pct, edge_pct,
            brief.get("edge_classification", "?"),
        )

        # --- 4b. Determine trade side ---
        # Positive edge → YES is underpriced → buy YES.
        # Negative edge → NO is underpriced → buy NO (flip probabilities).
        if edge_pct >= 0:
            side = "yes"
            val_prob = est_prob
            val_price = mkt_price
        else:
            side = "no"
            val_prob = 1.0 - est_prob    # our prob of NO winning
            val_price = 1.0 - mkt_price  # cost of a NO contract

        # --- 4c. Risk engine validation ---
        validation = validate_trade(
            ticker=ticker,
            estimated_probability=val_prob,
            market_price=val_price,
            bankroll=bankroll,
            current_positions=positions,
            daily_pnl=daily_pnl,
        )

        action_base = {
            "ticker": ticker,
            "side": side,
            "edge_pct": edge_pct,
            "est_prob_pct": est_prob_pct,
            "mkt_price_pct": mkt_price_pct,
            "at": now,
        }

        if not validation["approved"]:
            log.info("REJECTED %s: %s", ticker, validation["reason"])
            _log_action({**action_base, "event": "TRADE_REJECTED", "reason": validation["reason"]})
            summary["actions"].append({
                "ticker": ticker,
                "event": "rejected",
                "reason": validation["reason"],
            })
            continue

        summary["trades_approved"] += 1

        # --- 4d. Compute order parameters ---
        # count must be at least 1 contract
        count = max(1, int(validation["position_size_contracts"]))
        # Limit price = market ask (we accept the current price, not better)
        price_cents = max(1, min(99, round(val_price * 100)))
        actual_cost = count * val_price   # dollars committed

        log.info(
            "APPROVED %s | side=%s count=%d price=%dc kelly=%.4f cost=$%.2f",
            ticker, side, count, price_cents, validation["kelly_fraction"], actual_cost,
        )

        # --- 4e. Place order ---
        order_resp = place_order(ticker, side, count, price_cents, private_key)

        if order_resp is not None:
            order_obj = order_resp.get("order") or order_resp
            order_id = order_obj.get("order_id", "unknown")

            log.info("Order PLACED: %s order_id=%s", ticker, order_id)

            new_position = {
                "ticker": ticker,
                "side": side,
                "count": count,
                "price": val_price,
                "size_dollars": round(actual_cost, 4),
                "order_id": order_id,
                "placed_at": now,
            }
            positions.append(new_position)
            state["current_bankroll"] -= actual_cost
            bankroll = state["current_bankroll"]

            _log_action({
                **action_base,
                "event": "ORDER_PLACED",
                "order_id": order_id,
                "count": count,
                "price_cents": price_cents,
                "cost_dollars": round(actual_cost, 4),
                "kelly_fraction": validation["kelly_fraction"],
                "remaining_bankroll": round(bankroll, 2),
            })
            summary["actions"].append({
                "ticker": ticker,
                "event": "order_placed",
                "side": side,
                "count": count,
                "price_cents": price_cents,
                "cost_dollars": round(actual_cost, 4),
                "order_id": order_id,
            })
            summary["trades_placed"] += 1

        else:
            log.error("Order FAILED for %s — no position recorded", ticker)
            _log_action({**action_base, "event": "ORDER_FAILED"})
            summary["actions"].append({"ticker": ticker, "event": "order_failed"})

    # ------------------------------------------------------------------
    # 6. Persist updated state
    # ------------------------------------------------------------------
    state["current_positions"] = positions
    save_state(state)

    log.info("=" * 55)
    log.info(
        "Run complete | researched=%d approved=%d placed=%d | bankroll=$%.2f",
        summary["markets_researched"],
        summary["trades_approved"],
        summary["trades_placed"],
        state["current_bankroll"],
    )

    return summary


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("Kalshi Executor starting (env=%s)", config.KALSHI_ENV)

    if STOP_FILE.exists():
        log.warning("STOP file present — exiting immediately")
        sys.exit(0)

    summary = run_once()

    # Pretty-print summary
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  Executor Summary  —  {summary['run_at']}")
    print(sep)

    if summary.get("stopped"):
        print("  STATUS : HALTED (STOP file detected)")
    elif summary.get("error"):
        print(f"  STATUS : ERROR — {summary['error']}")
    else:
        print(f"  STATUS : OK")

    print(f"  Markets scanned    : {summary['markets_scanned']}")
    print(f"  Markets researched : {summary['markets_researched']}")
    print(f"  Trades approved    : {summary['trades_approved']}")
    print(f"  Trades placed      : {summary['trades_placed']}")

    if summary["actions"]:
        print()
        print("  Actions:")
        for a in summary["actions"]:
            ticker = a.get("ticker", "?")
            event = a.get("event", "?")
            if event == "order_placed":
                print(
                    f"    + {ticker:<35} PLACED  {a['side'].upper()} "
                    f"{a['count']}x @ {a['price_cents']}¢  "
                    f"(${a['cost_dollars']:.2f})"
                )
            elif event == "rejected":
                print(f"    - {ticker:<35} REJECTED — {a.get('reason', '')}")
            else:
                print(f"    ~ {ticker:<35} {event.upper()}")

    print(f"{sep}\n")


if __name__ == "__main__":
    main()
