# Skill: Kalshi Market Researcher

## Purpose

Take a flagged Kalshi market (typically from the Scanner output) and produce a structured research brief: an independent probability estimate, the key information that would move the odds, and a clear view on whether the current market price represents a mispricing worth trading.

---

## Inputs

A single market record (JSON) as produced by the Scanner skill, e.g.:

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
  "close_time": "2024-12-31T21:00:00Z"
}
```

---

## Research Process

### Step 1 — Understand the resolution rules
Before forming any probability estimate, clarify exactly how the market resolves:
- What is the precise resolution criterion (source, threshold, date/time)?
- Who is the adjudicator (Kalshi's own rules, a named data provider, a government agency)?
- Are there edge cases that could lead to N/A or void resolution?

State the resolution rules explicitly in the brief.

### Step 2 — Establish the base rate
Identify the relevant reference class for this event:
- For recurring events (monthly jobs report, FOMC decision, earnings): look at historical hit rates over the last 12–36 periods.
- For one-time events (elections, specific legislation): find polling averages, prediction market consensus from other venues (Polymarket, Metaculus, PredictIt), or analyst forecasts.
- Express the base rate as a probability, e.g. "SPX has closed above 4500 on the last trading day of December in 3 of the last 10 years → base rate ~30%."

### Step 3 — Apply current evidence
Starting from the base rate, update up or down based on information available right now:
- Recent price action of the underlying asset or poll movement
- Scheduled events before resolution (Fed meetings, earnings, data releases, votes)
- Sentiment or positioning signals (options skew, futures curve, betting market flows)
- Any news in the last 48 hours directly relevant to resolution

For each piece of evidence, state:
- The fact
- The direction of update (raises / lowers probability)
- A rough magnitude (small / moderate / large shift)

### Step 4 — Form an independent probability estimate
Synthesize Steps 2 and 3 into a single point estimate and a 90% confidence interval, e.g.:

```
Estimated true probability: 48%  (90% CI: 38% – 58%)
```

Be honest about uncertainty. Widen the interval when evidence is thin or conflicting.

### Step 5 — Compare to market price
```
Market yes price : 0.63  (implied probability 63%)
Your estimate    : 0.48  (48%)
Edge             : −15 pp  (market overpricing YES)
```

Classify the edge:
- `|edge| < 5 pp` → **Marginal** — not worth trading given transaction costs and model uncertainty
- `5 pp ≤ |edge| < 10 pp` → **Moderate** — worth watching; trade only if conviction is high
- `|edge| >= 10 pp` → **Strong** — investigate further and consider a position

### Step 6 — Identify information that would move the odds
List 3–5 specific, concrete pieces of information that, if they became known before resolution, would materially shift the true probability. For each, note the direction:

| Information | Direction if true | Expected timing |
|-------------|------------------|-----------------|
| Fed signals rate cut at Dec 13 meeting | Raises YES | Dec 13 |
| CPI print > 3.5% on Dec 12 | Lowers YES | Dec 12 |
| Large institutional block trade on NO side | Lowers YES | Ongoing |

These are your monitoring triggers — set alerts or re-run research if any of them materialize.

---

## Output Format

Save to `data/logs/research_{ticker}_{YYYYMMDD_HHMMSS}.json` and print a formatted brief to stdout.

### JSON schema
```json
{
  "ticker": "INXD-24DEC31-T4500",
  "title": "Will S&P 500 close above 4500 on Dec 31?",
  "researched_at": "2024-12-24T15:00:00Z",
  "resolution_rules": "Kalshi resolves YES if SPX official closing print >= 4500 on Dec 31 per Bloomberg.",
  "base_rate": {
    "description": "SPX above 4500 on last trading day of December",
    "historical_periods": 10,
    "hit_count": 3,
    "base_probability": 0.30
  },
  "evidence": [
    {
      "fact": "SPX is currently at 4480, up 2.1% this week",
      "direction": "raises",
      "magnitude": "moderate"
    }
  ],
  "estimate": {
    "probability": 0.48,
    "ci_low": 0.38,
    "ci_high": 0.58,
    "confidence": "medium"
  },
  "market_price": 0.63,
  "edge_pp": -15,
  "edge_classification": "strong",
  "trade_direction": "NO",
  "monitoring_triggers": [
    {
      "information": "Fed signals rate cut at Dec 13 meeting",
      "direction_if_true": "raises_yes",
      "expected_timing": "2024-12-13"
    }
  ],
  "notes": "Free-text analyst commentary, caveats, model limitations."
}
```

### Stdout brief (markdown)

```
## Research Brief — INXD-24DEC31-T4500
**Will S&P 500 close above 4500 on Dec 31?**

**Resolution:** Kalshi resolves YES if SPX closing print >= 4500 on Dec 31 (Bloomberg).

**Base rate:** 30% (3/10 years)

**Current evidence summary:**
- SPX at 4480 (+2.1% this week) → moderate upward update
- Fed meeting Dec 13 — no signal yet → neutral
- Dec CPI on Dec 12 — consensus 3.3% — minor risk to downside

**Estimate: 48%  (90% CI: 38–58%)**
**Market price: 63%**
**Edge: −15 pp → STRONG — market overpricing YES**

**Suggested trade: BUY NO**

**Watch for:**
1. Fed language at Dec 13 press conference
2. Dec 12 CPI print vs. 3.3% consensus
3. Any block trades > 1000 contracts on YES side
```

---

## Constraints and Caveats

- Do not fabricate statistics. If you cannot find a reliable base rate, say so and widen the confidence interval accordingly.
- Do not recommend a specific dollar size — that is a risk-management decision for the operator.
- Flag any market where the resolution rules are ambiguous or where Kalshi has discretion — these carry additional tail risk beyond the probability estimate.
- Re-run this skill if more than 48 hours pass before trading, or if a monitoring trigger fires.

---

## Example invocation

```bash
python scripts/researcher.py --ticker INXD-24DEC31-T4500
python scripts/researcher.py --file data/logs/scan_20241224_143200.json  # researches all markets in a scan file
```
