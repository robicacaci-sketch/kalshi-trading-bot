#!/usr/bin/env python3
"""
Kalshi Performance Tracker
Reads executor_log.json and resolver.log, calculates trading metrics, prints a
summary report, and saves results to data/logs/performance.json.

Usage:
    python3 scripts/performance.py
"""

import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

EXECUTOR_LOG  = config.LOG_DIR / "executor_log.json"
RESOLVER_LOG  = config.LOG_DIR / "resolver.log"
STATE_FILE    = config.LOG_DIR / "state.json"
PERF_OUT      = config.LOG_DIR / "performance.json"
PERF_HISTORY  = config.LOG_DIR / "performance_history.json"

# Regex for resolver.log RESOLVED lines:
# 2026-04-10 09:53:48,948 [INFO] RESOLVED KXCPI-26MAR-T0.8 | side=no result=yes | P&L=$-49.68 | new bankroll=$778.74
_RESOLVED_RE = re.compile(
    r"(?P<log_date>\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}.*?"
    r"RESOLVED (?P<ticker>\S+) \| side=(?P<side>\w+) result=(?P<result>\w+) "
    r"\| P&L=\$(?P<pnl>-?[\d.]+) \| new bankroll=\$(?P<bankroll>[\d.]+)"
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_placed_trades() -> list[dict]:
    """Return all ORDER_PLACED events from executor_log.json (JSON Lines)."""
    if not EXECUTOR_LOG.exists():
        return []
    trades = []
    with open(EXECUTOR_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == "ORDER_PLACED":
                trades.append(record)
    return trades


def load_resolved_trades() -> list[dict]:
    """Parse RESOLVED lines from resolver.log into structured dicts."""
    if not RESOLVER_LOG.exists():
        return []
    resolved = []
    with open(RESOLVER_LOG) as f:
        for line in f:
            m = _RESOLVED_RE.search(line)
            if not m:
                continue
            resolved.append({
                "ticker":     m.group("ticker"),
                "side":       m.group("side"),
                "result":     m.group("result"),
                "pnl":        float(m.group("pnl")),
                "bankroll":   float(m.group("bankroll")),
                "resolved_date": m.group("log_date"),   # YYYY-MM-DD, used for Sharpe
            })
    return resolved


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

_SHOCK_KEYWORDS = {
    "war", "ceasefire", "surprise", "unexpected", "shock", "crisis",
    "collapse", "emergency", "invasion", "sanctions", "tariff", "escalat",
}
_TIMING_KEYWORDS = {
    "before resolution", "moved against", "reversed", "prior to", "ahead of",
    "before expiry", "timing", "too early",
}


def _load_research_brief(ticker: str) -> dict:
    """Return the most recent research brief JSON for ticker, or {} if not found."""
    safe = ticker.replace("/", "-")
    # Glob for all briefs for this ticker, take the most recent by filename sort
    matches = sorted(config.LOG_DIR.glob(f"research_{safe}_*.json"))
    if not matches:
        return {}
    try:
        with open(matches[-1]) as f:
            return json.load(f)
    except Exception:
        return {}


def classify_failure(trade: dict) -> str:
    """
    Classify a losing trade into one of four categories:
      LOW_EDGE       — claimed edge < 10% (borderline trade, shouldn't have taken it)
      EXTERNAL_SHOCK — reasoning mentions surprise/shock keywords
      BAD_TIMING     — reasoning mentions timing-related language
      BAD_PREDICTION — default (Claude was wrong, no obvious external cause)
    """
    edge_pct = trade.get("edge_pct")
    if edge_pct is not None and abs(edge_pct) < 10:
        return "LOW_EDGE"

    brief = _load_research_brief(trade["ticker"])
    reasoning = (brief.get("reasoning") or "").lower()
    risks_text = " ".join(brief.get("risks") or []).lower()
    combined = reasoning + " " + risks_text

    if any(kw in combined for kw in _SHOCK_KEYWORDS):
        return "EXTERNAL_SHOCK"
    if any(kw in combined for kw in _TIMING_KEYWORDS):
        return "BAD_TIMING"
    return "BAD_PREDICTION"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def build_metrics(placed: list[dict], resolved: list[dict], state: dict) -> dict:
    """Join placed and resolved records; compute all performance metrics."""

    # Index placed trades by ticker for O(1) lookup
    placed_by_ticker: dict[str, dict] = {}
    for t in placed:
        # Later placements of the same ticker overwrite earlier ones; that's fine
        # because we only ever hold one position per ticker.
        placed_by_ticker[t["ticker"]] = t

    open_tickers = {p["ticker"] for p in state.get("current_positions", [])}

    # Enrich resolved records with placed-trade metadata where available
    enriched: list[dict] = []
    for r in resolved:
        placed_rec = placed_by_ticker.get(r["ticker"], {})
        won = r["pnl"] > 0

        # Our estimated probability for the side we actually bet.
        # est_prob_pct is the YES probability; if we bet NO, flip it.
        est_prob_pct = placed_rec.get("est_prob_pct")
        if est_prob_pct is not None:
            if r["side"] == "yes":
                predicted_prob = est_prob_pct / 100.0
            else:
                predicted_prob = 1.0 - (est_prob_pct / 100.0)
        else:
            predicted_prob = None

        enriched.append({
            "ticker":         r["ticker"],
            "side":           r["side"],
            "result":         r["result"],
            "pnl":            r["pnl"],
            "won":            won,
            "edge_pct":       placed_rec.get("edge_pct"),   # raw signed edge
            "est_prob_pct":   est_prob_pct,
            "mkt_price_pct":  placed_rec.get("mkt_price_pct"),
            "count":          placed_rec.get("count"),
            "price_cents":    placed_rec.get("price_cents"),
            "cost_dollars":   placed_rec.get("cost_dollars"),
            "placed_at":      placed_rec.get("at"),
            "predicted_prob": predicted_prob,
        })

    # --- Core counts ---
    total_placed   = len(placed)
    total_resolved = len(enriched)
    total_open     = len(open_tickers)
    winners        = [r for r in enriched if r["won"]]
    losers         = [r for r in enriched if not r["won"]]

    win_rate = len(winners) / total_resolved if total_resolved else None
    total_pnl = sum(r["pnl"] for r in enriched)

    # --- Edge breakdown ---
    def avg_abs_edge(records: list[dict]) -> float | None:
        edges = [abs(r["edge_pct"]) for r in records if r["edge_pct"] is not None]
        return round(sum(edges) / len(edges), 2) if edges else None

    avg_edge_winners = avg_abs_edge(winners)
    avg_edge_losers  = avg_abs_edge(losers)

    # --- Brier Score ---
    # BS = (1/n) * Σ (predicted_prob − actual_outcome)²
    # Lower is better; random guessing gives 0.25.
    brier_records = [r for r in enriched if r["predicted_prob"] is not None]
    if brier_records:
        brier_score = sum(
            (r["predicted_prob"] - (1 if r["won"] else 0)) ** 2
            for r in brier_records
        ) / len(brier_records)
        brier_score = round(brier_score, 4)
    else:
        brier_score = None

    # --- Sharpe Ratio ---
    # Group resolved P&L by calendar day, compute annualised Sharpe.
    # Requires ≥2 days of data to compute a meaningful std.
    daily_pnl_map: dict[str, float] = defaultdict(float)
    for r in enriched:
        day = (r.get("resolved_date") or "unknown")
        daily_pnl_map[day] += r["pnl"]
    daily_pnls = list(daily_pnl_map.values())
    if len(daily_pnls) >= 2:
        mean_d = sum(daily_pnls) / len(daily_pnls)
        variance = sum((x - mean_d) ** 2 for x in daily_pnls) / (len(daily_pnls) - 1)
        std_d = math.sqrt(variance)
        sharpe_ratio = round((mean_d / std_d) * math.sqrt(365), 4) if std_d > 0 else None
    else:
        sharpe_ratio = None

    # --- Profit Factor ---
    gross_profit = sum(r["pnl"] for r in enriched if r["pnl"] > 0)
    gross_loss   = abs(sum(r["pnl"] for r in enriched if r["pnl"] < 0))
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None

    # --- Failure Classification ---
    failure_classes: dict[str, int] = {
        "BAD_PREDICTION": 0, "EXTERNAL_SHOCK": 0, "BAD_TIMING": 0, "LOW_EDGE": 0,
    }
    for trade in losers:
        label = classify_failure(trade)
        trade["failure_class"] = label
        failure_classes[label] = failure_classes.get(label, 0) + 1

    # --- Best / worst ---
    best  = max(enriched, key=lambda r: r["pnl"]) if enriched else None
    worst = min(enriched, key=lambda r: r["pnl"]) if enriched else None

    # --- Average cost per trade ---
    costs = [r["cost_dollars"] for r in enriched if r["cost_dollars"] is not None]
    avg_cost = round(sum(costs) / len(costs), 2) if costs else None

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "current_bankroll": state.get("current_bankroll"),
        "total_placed":     total_placed,
        "total_resolved":   total_resolved,
        "total_open":       total_open,
        "total_unaccounted": total_placed - total_resolved - total_open,
        "wins":             len(winners),
        "losses":           len(losers),
        "win_rate":         round(win_rate, 4) if win_rate is not None else None,
        "total_pnl":        round(total_pnl, 2),
        "avg_cost_per_trade": avg_cost,
        "avg_edge_winners": avg_edge_winners,
        "avg_edge_losers":  avg_edge_losers,
        "brier_score":      brier_score,
        "sharpe_ratio":     sharpe_ratio,
        "profit_factor":    profit_factor,
        "failure_classes":  failure_classes,
        "best_trade":       best,
        "worst_trade":      worst,
        "resolved_trades":  enriched,
    }


# ---------------------------------------------------------------------------
# Performance history (JSON Lines)
# ---------------------------------------------------------------------------

def append_history_snapshot(metrics: dict) -> None:
    """Append a condensed snapshot of current metrics to performance_history.json."""
    snapshot = {
        "timestamp":       metrics["generated_at"],
        "bankroll":        metrics["current_bankroll"],
        "total_trades":    metrics["total_placed"],
        "resolved_trades": metrics["total_resolved"],
        "wins":            metrics["wins"],
        "losses":          metrics["losses"],
        "win_rate":        metrics["win_rate"],
        "brier_score":     metrics["brier_score"],
        "total_pnl":       metrics["total_pnl"],
    }
    PERF_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(PERF_HISTORY, "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def load_history() -> list[dict]:
    """Load all snapshots from performance_history.json (JSON Lines)."""
    if not PERF_HISTORY.exists():
        return []
    entries = []
    with open(PERF_HISTORY) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


# ---------------------------------------------------------------------------
# Weekly trend
# ---------------------------------------------------------------------------

def print_weekly_trend(history: list[dict]) -> None:
    """Print week-over-week changes in win_rate and brier_score."""
    if not history:
        return

    # Group snapshots by ISO year-week
    from collections import defaultdict
    weeks: dict[str, list[dict]] = defaultdict(list)
    for entry in history:
        try:
            dt = datetime.fromisoformat(entry["timestamp"])
        except (ValueError, KeyError):
            continue
        iso = dt.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        weeks[key].append(entry)

    if len(weeks) < 2:
        return  # not enough weeks for a trend

    def week_avg(entries: list[dict], field: str) -> float | None:
        vals = [e[field] for e in entries if e.get(field) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    sorted_weeks = sorted(weeks)
    sep  = "=" * 58
    sep2 = "-" * 58

    print(f"\n{sep}")
    print(f"  Weekly Trend  (win rate & Brier score)")
    print(sep)
    print(f"  {'Week':<12} {'Win rate':>9} {'Δ win rate':>11}  {'Brier':>7} {'Δ Brier':>8}")
    print(sep2)

    prev_wr = prev_bs = None
    for week in sorted_weeks:
        entries = weeks[week]
        wr = week_avg(entries, "win_rate")
        bs = week_avg(entries, "brier_score")

        wr_str = f"{wr*100:.1f}%" if wr is not None else "  n/a"
        bs_str = f"{bs:.4f}"      if bs is not None else "    n/a"

        if prev_wr is not None and wr is not None:
            delta_wr = wr - prev_wr
            dwr_str = f"{'+' if delta_wr >= 0 else ''}{delta_wr*100:.1f}%"
        else:
            dwr_str = "     —"

        if prev_bs is not None and bs is not None:
            delta_bs = bs - prev_bs
            dbs_str = f"{'+' if delta_bs >= 0 else ''}{delta_bs:.4f}"
        else:
            dbs_str = "      —"

        print(f"  {week:<12} {wr_str:>9} {dwr_str:>11}  {bs_str:>7} {dbs_str:>8}")

        prev_wr = wr
        prev_bs = bs

    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# Report printing
# ---------------------------------------------------------------------------

def print_report(m: dict) -> None:
    sep  = "=" * 58
    sep2 = "-" * 58

    def fmt_pnl(v):
        if v is None:
            return "n/a"
        return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"

    def fmt_pct(v):
        return f"{v * 100:.1f}%" if v is not None else "n/a"

    def fmt_score(v):
        return f"{v:.4f}" if v is not None else "n/a"

    print(f"\n{sep}")
    print(f"  Kalshi Performance Report  —  {m['generated_at'][:10]}")
    print(sep)
    print(f"  Bankroll (current)   : ${m['current_bankroll']:.2f}" if m["current_bankroll"] else "")
    print(f"  Total P&L            : {fmt_pnl(m['total_pnl'])}")
    print(sep2)
    print(f"  Trades placed        : {m['total_placed']}")
    print(f"  Trades resolved      : {m['total_resolved']}")
    print(f"  Trades open          : {m['total_open']}")
    if m["total_unaccounted"] > 0:
        print(f"  Trades unaccounted   : {m['total_unaccounted']}  (state resets / pre-resolver)")
    print(sep2)
    print(f"  Wins / Losses        : {m['wins']} W  /  {m['losses']} L")
    print(f"  Win rate             : {fmt_pct(m['win_rate'])}")
    print(f"  Avg cost per trade   : ${m['avg_cost_per_trade']:.2f}" if m["avg_cost_per_trade"] else "  Avg cost per trade   : n/a")
    print(sep2)
    print(f"  Avg edge (winners)   : {m['avg_edge_winners']}%" if m["avg_edge_winners"] is not None else "  Avg edge (winners)   : n/a")
    print(f"  Avg edge (losers)    : {m['avg_edge_losers']}%" if m["avg_edge_losers"] is not None else "  Avg edge (losers)    : n/a")
    print(f"  Brier Score          : {fmt_score(m['brier_score'])}  (random=0.2500, perfect=0.0000)")
    sr = m.get("sharpe_ratio")
    print(f"  Sharpe Ratio         : {sr:.4f}  (annualised)" if sr is not None else "  Sharpe Ratio         : n/a  (need ≥2 trading days)")
    pf = m.get("profit_factor")
    print(f"  Profit Factor        : {pf:.4f}  (gross profit / gross loss)" if pf is not None else "  Profit Factor        : n/a  (no losses yet)")
    print(sep2)

    fc = m.get("failure_classes", {})
    if fc and m.get("losses", 0) > 0:
        print(f"  Failure breakdown    :")
        for label, count in sorted(fc.items(), key=lambda x: -x[1]):
            if count > 0:
                print(f"    {label:<18} : {count}")
    print(sep2)

    if m["best_trade"]:
        b = m["best_trade"]
        print(f"  Best trade           : {b['ticker']}  {fmt_pnl(b['pnl'])}  ({b['side'].upper()} → {b['result'].upper()})")
    else:
        print(f"  Best trade           : n/a")

    if m["worst_trade"]:
        w = m["worst_trade"]
        print(f"  Worst trade          : {w['ticker']}  {fmt_pnl(w['pnl'])}  ({w['side'].upper()} → {w['result'].upper()})")
    else:
        print(f"  Worst trade          : n/a")

    print(sep2)

    if m["resolved_trades"]:
        print(f"  {'Ticker':<32} {'Side':<5} {'Result':<7} {'P&L':>8}  {'Edge':>6}  Failure class")
        print(f"  {'-'*32} {'-'*5} {'-'*6} {'-'*8}  {'-'*6}  {'-'*16}")
        for r in sorted(m["resolved_trades"], key=lambda x: x["placed_at"] or ""):
            edge_str  = f"{abs(r['edge_pct']):.1f}%" if r["edge_pct"] is not None else "  n/a"
            fail_str  = r.get("failure_class", "") if not r["won"] else ""
            print(
                f"  {r['ticker']:<32} {r['side']:<5} {r['result']:<7}"
                f" {fmt_pnl(r['pnl']):>8}  {edge_str:>6}  {fail_str}"
            )

    print(f"{sep}\n")

    history = load_history()
    print_weekly_trend(history)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    placed   = load_placed_trades()
    resolved = load_resolved_trades()
    state    = load_state()

    if not placed and not resolved:
        print("No trade data found. Run the executor first.")
        sys.exit(0)

    metrics = build_metrics(placed, resolved, state)

    # Save snapshot to history and full metrics to disk
    append_history_snapshot(metrics)
    PERF_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(PERF_OUT, "w") as f:
        json.dump(metrics, f, indent=2)

    print_report(metrics)
    print(f"Metrics saved → {PERF_OUT}")


if __name__ == "__main__":
    main()
