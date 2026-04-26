#!/usr/bin/env python3
"""
Kalshi Risk Engine
Pure-Python, deterministic position sizing and trade validation.

Kelly Criterion:  f* = (p*b - q) / b  where b = (1 - c) / c
Simplifies to:    f* = (p - c) / (1 - c)
Quarter Kelly:    0.25 * f*

All probabilities and prices are fractions in [0, 1].
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk parameters
# ---------------------------------------------------------------------------

KELLY_FRACTION = 0.25          # quarter Kelly
MIN_EDGE = 0.04                # 4% minimum edge (estimated_prob - market_price)
MAX_POSITION_PCT = 0.05        # max 5% of bankroll per position
MAX_DAILY_LOSS_PCT = 0.15      # block trading if day's loss exceeds 15% of bankroll
MAX_CONCURRENT_POSITIONS = 15  # hard cap on open positions
MAX_TOTAL_EXPOSURE_PCT = 0.50  # max 50% of bankroll across all open positions

RISK_LOG_PATH = config.LOG_DIR / "risk_log.json"


# ---------------------------------------------------------------------------
# Kelly math
# ---------------------------------------------------------------------------

def kelly_fraction(estimated_prob: float, market_price: float) -> float:
    """
    Compute full Kelly fraction for a YES bet.

    Args:
        estimated_prob: true win probability, 0-1
        market_price:   market-implied probability / cost per contract, 0-1

    Returns:
        Kelly fraction (may be negative if no edge)
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    # b = net odds on a $1 bet: pay market_price, win 1 → net = 1 - market_price
    # f* = (p - c) / (1 - c)  [derived from standard Kelly formula]
    return (estimated_prob - market_price) / (1.0 - market_price)


# ---------------------------------------------------------------------------
# Core validation
# ---------------------------------------------------------------------------

def validate_trade(
    ticker: str,
    estimated_probability: float,   # 0-1
    market_price: float,             # 0-1  (yes_ask_dollars)
    bankroll: float,                 # total account value in dollars
    current_positions: list[dict],   # each dict must have "size_dollars": float
    daily_pnl: float,                # today's realised P&L in dollars (negative = loss)
) -> dict:
    """
    Validate a potential trade and compute position size.

    Returns a dict with:
        approved            (bool)
        position_size_dollars
        position_size_contracts
        reason              (str)
        kelly_fraction      (float, the quarter-Kelly fraction used)
    """
    now = datetime.now(timezone.utc).isoformat()

    # Sanity guard
    if bankroll <= 0:
        result = _reject(ticker, 0.0, "Bankroll is zero or negative", now)
        _log(result)
        return result

    # --- Compute edge ---
    edge = estimated_probability - market_price

    # --- Risk check 1: minimum edge ---
    if edge <= MIN_EDGE:
        result = _reject(
            ticker,
            0.0,
            f"Edge {edge*100:.2f}% <= minimum {MIN_EDGE*100:.0f}%",
            now,
        )
        _log(result)
        return result

    # --- Risk check 2: daily loss limit ---
    if daily_pnl < 0 and abs(daily_pnl) >= MAX_DAILY_LOSS_PCT * bankroll:
        result = _reject(
            ticker,
            0.0,
            f"Daily loss limit hit: lost ${abs(daily_pnl):.2f} "
            f"({abs(daily_pnl)/bankroll*100:.1f}% of bankroll)",
            now,
        )
        _log(result)
        return result

    # --- Risk check 3: max concurrent positions ---
    if len(current_positions) >= MAX_CONCURRENT_POSITIONS:
        result = _reject(
            ticker,
            0.0,
            f"Max concurrent positions ({MAX_CONCURRENT_POSITIONS}) reached",
            now,
        )
        _log(result)
        return result

    # --- Risk check 4: max total exposure ---
    total_exposure = sum(p.get("size_dollars", 0) for p in current_positions)
    if total_exposure >= MAX_TOTAL_EXPOSURE_PCT * bankroll:
        result = _reject(
            ticker,
            0.0,
            f"Total exposure ${total_exposure:.2f} >= {MAX_TOTAL_EXPOSURE_PCT*100:.0f}% "
            f"of bankroll (${MAX_TOTAL_EXPOSURE_PCT * bankroll:.2f})",
            now,
        )
        _log(result)
        return result

    # --- Kelly sizing ---
    full_kelly = kelly_fraction(estimated_probability, market_price)
    qk = KELLY_FRACTION * full_kelly           # quarter Kelly fraction
    raw_size = qk * bankroll                   # dollars before caps

    # Cap at 5% of bankroll
    max_dollars = MAX_POSITION_PCT * bankroll
    position_dollars = min(raw_size, max_dollars)

    # Also cap so total exposure stays within limit after this trade
    remaining_exposure = MAX_TOTAL_EXPOSURE_PCT * bankroll - total_exposure
    position_dollars = min(position_dollars, remaining_exposure)

    # Must be positive to proceed
    if position_dollars <= 0:
        result = _reject(
            ticker,
            0.0,
            "Position size rounded to zero after caps",
            now,
        )
        _log(result)
        return result

    # --- Risk check 5: position size cap (sanity double-check) ---
    if position_dollars > MAX_POSITION_PCT * bankroll:
        result = _reject(
            ticker,
            position_dollars,
            f"Position ${position_dollars:.2f} exceeds 5% cap (${max_dollars:.2f})",
            now,
        )
        _log(result)
        return result

    # Contracts: each YES contract costs market_price dollars
    position_contracts = position_dollars / market_price

    result = {
        "approved": True,
        "ticker": ticker,
        "position_size_dollars": round(position_dollars, 2),
        "position_size_contracts": round(position_contracts, 2),
        "reason": "All risk checks passed",
        "kelly_fraction": round(qk, 6),
        # informational
        "edge_pct": round(edge * 100, 4),
        "estimated_probability": estimated_probability,
        "market_price": market_price,
        "validated_at": now,
    }
    _log(result)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reject(ticker: str, position_dollars: float, reason: str, now: str) -> dict:
    return {
        "approved": False,
        "ticker": ticker,
        "position_size_dollars": 0.0,
        "position_size_contracts": 0.0,
        "reason": reason,
        "kelly_fraction": 0.0,
        "validated_at": now,
    }


def _log(result: dict) -> None:
    """Append the validation result to the risk log (JSON Lines)."""
    try:
        RISK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RISK_LOG_PATH, "a") as f:
            f.write(json.dumps(result) + "\n")
    except Exception as exc:
        log.warning("Could not write to risk log: %s", exc)


# ---------------------------------------------------------------------------
# CLI for quick manual testing
# ---------------------------------------------------------------------------

def main():
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="Validate a hypothetical Kalshi trade.")
    parser.add_argument("ticker", help="Market ticker")
    parser.add_argument("estimated_prob", type=float, help="Your probability estimate (0-1)")
    parser.add_argument("market_price", type=float, help="Market ask price (0-1)")
    parser.add_argument("--bankroll", type=float, default=1000.0)
    parser.add_argument("--num-positions", type=int, default=0,
                        help="Number of currently open positions (default 0)")
    parser.add_argument("--exposure", type=float, default=0.0,
                        help="Total dollars currently at risk across open positions")
    parser.add_argument("--daily-pnl", type=float, default=0.0)
    args = parser.parse_args()

    # Fake current_positions list from --exposure
    positions = [{"size_dollars": args.exposure}] if args.exposure else []
    # Pad with zero-size entries for the count
    while len(positions) < args.num_positions:
        positions.append({"size_dollars": 0.0})

    result = validate_trade(
        ticker=args.ticker,
        estimated_probability=args.estimated_prob,
        market_price=args.market_price,
        bankroll=args.bankroll,
        current_positions=positions,
        daily_pnl=args.daily_pnl,
    )

    status = "APPROVED" if result["approved"] else "REJECTED"
    print(f"\n{'='*55}")
    print(f"  {result['ticker']}  →  {status}")
    print(f"{'='*55}")
    for k, v in result.items():
        if k not in ("approved", "ticker"):
            print(f"  {k:<30}: {v}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
