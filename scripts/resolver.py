#!/usr/bin/env python3
"""
Kalshi Position Resolver
Checks open positions in state.json against the live Kalshi API, calculates
P&L for resolved markets, updates the bankroll, and removes closed positions.

Called automatically at the start of each executor run via resolve_positions().
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from scripts.performance import (
    append_history_snapshot,
    build_metrics,
    load_placed_trades,
    load_resolved_trades,
    load_state as perf_load_state,
)

log = logging.getLogger("resolver")
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _fh = logging.FileHandler(config.LOG_DIR / "resolver.log")
    _fh.setFormatter(_fmt)
    log.addHandler(_sh)
    log.addHandler(_fh)
    log.setLevel(logging.INFO)

STATE_FILE = config.LOG_DIR / "state.json"
# Market results are read from the live (non-demo) API — no auth required for
# public market data endpoints.
LIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _fetch_order(order_id: str) -> dict | None:
    """Fetch a single order from the live API (no auth needed for public order status)."""
    # Order status requires auth; use the authenticated base URL if available,
    # otherwise skip silently — this is advisory only.
    url = f"{LIVE_BASE_URL}/portfolio/orders/{order_id}"
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException as exc:
        log.debug("HTTP error fetching order %s: %s", order_id, exc)
        return None
    if resp.ok:
        return resp.json().get("order")
    # 401/403 expected for unauthenticated calls — not an error worth logging
    return None


def _fetch_market(ticker: str) -> dict | None:
    url = f"{LIVE_BASE_URL}/markets/{ticker}"
    try:
        resp = requests.get(url, timeout=10)
    except requests.RequestException as exc:
        log.error("HTTP error fetching market %s: %s", ticker, exc)
        return None
    if resp.ok:
        return resp.json().get("market")
    log.error("Failed to fetch market %s: HTTP %d — %s", ticker, resp.status_code, resp.text[:200])
    return None


def _calc_pnl(position: dict, result: str) -> float:
    """
    Return net P&L in dollars for a resolved position.

    position keys used: side, count, price (fractional dollars, e.g. 0.62)
    result: "yes" or "no"

    Win:  receive $1.00 per contract, cost was price → profit = count * (1 - price)
    Loss: contract expires worthless → loss = count * price
    """
    side: str = position["side"]
    count: int = int(position["count"])
    price: float = float(position["price"])  # already in dollars (0–1)

    won = (side == result)
    if won:
        return round(count * (1.0 - price), 4)
    else:
        return round(-(count * price), 4)


def resolve_positions() -> None:
    """
    Load state.json, check each open position against the live API, settle any
    resolved markets, and save the updated state back to disk.
    """
    if not STATE_FILE.exists():
        log.info("No state.json found — nothing to resolve")
        return

    with open(STATE_FILE) as f:
        state = json.load(f)

    positions: list[dict] = state.get("current_positions", [])
    if not positions:
        log.info("No open positions to resolve")
        return

    log.info("Checking %d open position(s) for resolution...", len(positions))

    bankroll: float = float(state.get("current_bankroll", 0.0))
    daily_pnl: float = float(state.get("daily_pnl", 0.0))
    remaining: list[dict] = []
    resolved_count = 0

    for pos in positions:
        ticker: str = pos.get("ticker", "")
        market = _fetch_market(ticker)
        if market is None:
            # API error — keep position, retry next run
            remaining.append(pos)
            continue

        result: str = market.get("result", "")
        if result not in ("yes", "no"):
            # Still open — check if the order is stale (0 fills after 24 h)
            order_id: str = pos.get("order_id", "")
            placed_at_str: str = pos.get("placed_at", "")
            if order_id and not order_id.startswith("SIMULATED-") and placed_at_str:
                try:
                    placed_at = datetime.fromisoformat(placed_at_str.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - placed_at).total_seconds() / 3600
                    if age_hours >= 24:
                        order = _fetch_order(order_id)
                        if order is not None:
                            filled = int(order.get("filled_count") or order.get("fill_count") or 0)
                            if filled == 0:
                                log.warning(
                                    "Order %s for %s has 0 fills after 24h — may be stale",
                                    order_id, ticker,
                                )
                except Exception as exc:
                    log.debug("Stale-order check failed for %s: %s", order_id, exc)
            remaining.append(pos)
            continue

        # Market has resolved
        pnl = _calc_pnl(pos, result)
        bankroll = round(bankroll + pnl, 4)
        daily_pnl = round(daily_pnl + pnl, 4)
        resolved_count += 1

        log.info(
            "RESOLVED %s | side=%s result=%s | P&L=$%.2f | new bankroll=$%.2f",
            ticker, pos.get("side"), result, pnl, bankroll,
        )

    if resolved_count == 0:
        log.info("No positions resolved this run")
        return

    state["current_positions"] = remaining
    state["current_bankroll"] = bankroll
    state["daily_pnl"] = daily_pnl
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    log.info(
        "Resolved %d position(s) | %d still open | bankroll=$%.2f",
        resolved_count, len(remaining), bankroll,
    )

    # Snapshot current performance metrics to history after each resolution
    try:
        metrics = build_metrics(
            load_placed_trades(),
            load_resolved_trades(),
            perf_load_state(),
        )
        append_history_snapshot(metrics)
        log.info("Performance snapshot appended to history")
    except Exception as exc:
        log.error("Failed to append performance snapshot (non-fatal): %s", exc)


if __name__ == "__main__":
    resolve_positions()
