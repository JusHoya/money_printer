# Workstream C — Ladder-quality descriptive statistics

**Date:** 2026-07-26
**Data:** `data/ladders/` — 69 target dates × 4 cities × 6 brackets, hourly
candles from Kalshi's own recorded market history.
**Purpose:** workstream E computes EV from these rows. This document is the
honest picture of what is actually in them, *before* any EV is claimed.

Reproduce every number here:

```powershell
$env:PYTHONPATH = "."
$env:OMP_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"
$env:MKL_NUM_THREADS = "2"; $env:NUMEXPR_NUM_THREADS = "2"
python scripts/backfill_ladders.py --stats
```

---

## 0. Headline — read this before modelling anything

> **Near settlement the weather book is one-sided in exactly the direction
> that makes both FR-3.1 trade shapes unexecutable.**
>
> - Far brackets (≥5 °F from the eventual outcome) have a YES **bid** in
>   **0.0 %** of hourly snapshots inside the last 6 hours to close, and 1.5 %
>   in the 6–12 h window. You cannot sell them. Their **ask** is available
>   100 % of the time at a median of 1 ¢ — the market will happily sell you a
>   loser, and will not buy one back.
> - The winning bracket has an **ask** in only **1.9 %** of snapshots inside
>   the last hour and 5.0 % inside 3–6 h. You cannot buy it. Its **bid** sits
>   at 0.99 in 100 % of those snapshots.
>
> The tradeable window is **more than ~6 hours before close**. Anything
> Phase 3 wants to do in the final hours has to survive the fact that only one
> side of the book exists there, and it is the wrong side.

The one genuinely encouraging number: in the **6–12 h** bucket (which contains
the settlement stations' local afternoon), the eventual winner's ask is quoted
**63.0 %** of the time at a median of **$0.85** — i.e. a bracket worth $1.00 at
settlement is still offered at 85 ¢ while the outcome is largely determined.
Whether a calibrated model can identify that bracket at that moment is
workstream B/E's question; this workstream only asserts that the quote exists.

---

## 1. Coverage

| metric | value |
|---|---|
| rows (one per market per hourly candle) | **62,932** |
| target dates | **69** (2026-05-18 … 2026-07-25, consecutive, no gaps) |
| cities | CHI, LAX, MIA, NY (KXHIGHCHI / KXHIGHLAX / KXHIGHMIA / KXHIGHNY) |
| city-days | **276** (69 × 4; **0 empty**) |
| markets | **1,656** |
| markets per city-day | min 6, median 6, max 6 |
| candles per market | min 29, median 38, max 41 |
| **rows with a two-sided quote** | **38,200 / 62,932 = 60.7 %** |
| `bracket_payoff` vs Kalshi `result`, from `expiration_value` | **1,655 / 1,655** |
| `bracket_payoff` vs Kalshi `result`, from CLI truth | **1,632 / 1,632** |
| union of the two (every market covered by at least one) | **1,656 / 1,656** |
| Kalshi `expiration_value` vs CLI truth | **1,631 / 1,631 agree** |
| HTTP failures | **0** (1,936 requests) |

Every city-day carries the same 6-bracket ladder — 2 °F-wide `between`
brackets plus a `less` floor and a `greater` cap — and the six brackets tile
the temperature axis with no gap or overlap
(`tests/test_kalshi_history.py::test_recorded_ladder_is_a_complete_partition`).

**Volume sanity check.** Summing per-candle `volume_fp` for
`KXHIGHNY-26JUL17` recovers 271,931 contracts against the market-level
`volume_fp` total of 273,761 — **99.33 %**. The residual is the partial period
at market open. The volume column is therefore real per-period flow, not a
cumulative field summed twice.

### Denominator gaps (both fully explained, neither hidden)

- **1 market of 1,656** has a blank `expiration_value` from Kalshi:
  `KXHIGHNY-26JUN23-T78` (`status: finalized`, `result: yes`,
  `expiration_value: ""`, `settlement_value_dollars: "1.0000"`). Its five
  siblings that day all carry `"71.00"`. That single market is excluded from
  the `expiration_value`-based agreement denominator (hence 1,655 not 1,656).
  The independent CLI-truth recompute covers it: KNYC 2026-06-23 high = 71 °F,
  and a `less` bracket with `cap_strike = 78` pays YES for high ≤ 77 → YES,
  matching Kalshi. Recorded in the manifest under
  `markets_missing_expiration_value`, and covered by the independent CLI-truth
  recompute (1,632 / 1,632).
- **24 markets** (all four cities on **2026-07-25**) have no CLI cross-check:
  `data/weather_truth/cli_daily_high_*.csv` ends at **2026-07-24**, because the
  NWS Climatological Report for a date publishes the following morning and the
  Phase 1 backfill last ran 2026-07-25. Kalshi's own `expiration_value` covers
  those markets. 1,656 − 24 − 1 = **1,631**, which is exactly the truth
  denominator.

---

## 2. Bid–ask spread

Computed on rows with a two-sided quote only (`has_quote`), as
`(yes_ask − yes_bid) × 100`, in cents. Prices are on a 1 ¢ tick, so 1 ¢ is the
floor.

### (a) By time to settlement

`minutes_to_close` is measured to the market's `close_time` (04:59 UTC the day
after the target date), so ">=24 h" is the overnight window before the target
day and "6–12 h" contains the settlement stations' local afternoon.

| hours to close | quoted rows | % of rows quoted | median | p25 | p75 | p90 |
|---|---|---|---|---|---|---|
| <1 h | 10 | **0.7 %** | 1.0 ¢ | 1.0 | 2.0 | 4.3 |
| 1–3 h | 14 | **0.5 %** | 1.0 ¢ | 1.0 | 2.7 | 3.0 |
| 3–6 h | 83 | **1.8 %** | 1.0 ¢ | 1.0 | 2.0 | 5.0 |
| 6–12 h | 2,586 | **26.4 %** | 1.0 ¢ | 1.0 | 3.0 | 4.0 |
| 12–24 h | 13,938 | **71.3 %** | 1.0 ¢ | 1.0 | 2.0 | 3.0 |
| ≥24 h | 21,569 | **87.7 %** | 1.0 ¢ | 1.0 | 2.0 | 3.0 |

The spread *when quoted* is tight and stable — median 1 ¢ everywhere, p90 ≤ 5 ¢.
**The variable that moves is not the spread, it is whether a two-sided quote
exists at all**, and it collapses from 87.7 % to 0.5 % as settlement nears.
Any EV model that conditions on "quoted" rows and ignores the availability
column will silently be modelling a market that is open 88 % of the time when
it is actually open 0.5 % of the time in the window that matters.

### (b) By bracket distance from the settled outcome (moneyness proxy)

Distance = degrees F between the settled daily high and the bracket's YES band,
derived only from `strike_type` / `floor_strike` / `cap_strike` (PRD FR-1.1);
`0F` is the bracket that actually paid. **This banding is conditioned on
hindsight** and is a diagnostic of market structure, not a tradeable signal.

| distance | rows | % two-sided | median spread | p90 spread | median yes_ask |
|---|---|---|---|---|---|
| 0 °F (winner) | 10,678 | 79.4 % | 1.0 ¢ | 4.0 ¢ | 0.450 |
| 1–2 °F | 18,806 | 74.3 % | 1.0 ¢ | 3.0 ¢ | 0.230 |
| 3–4 °F | 16,268 | 60.0 % | 1.0 ¢ | 3.0 ¢ | 0.060 |
| 5–6 °F | 10,254 | **39.9 %** | 1.0 ¢ | 2.0 ¢ | 0.040 |
| ≥7 °F | 6,888 | **27.4 %** | 1.0 ¢ | 2.0 ¢ | 0.030 |

---

## 3. One-sided availability — what each FR-3.1 shape actually needs

`has_quote` demands both sides, but the two Phase 3 trade shapes need only one
each:

- **FR-3.1(a) far-bracket NO** = sell YES / buy NO → needs **`yes_bid > 0`**
  (`no_ask = 1 − yes_bid`; with no YES bid, the NO ask is $1.00 and the trade
  does not exist as a taker).
- **FR-3.1(b) lock-in** = buy the near-certain outcome → needs
  **`yes_ask < 1`**.

Reporting only the two-sided figure would misstate what is executable, so both
sides are sliced explicitly. Medians below are over the rows where that side
exists (so `med_bid` and `med_ask` are **not** paired and may invert).

### All rows, by bracket distance

| distance | rows | `yes_bid>0` | `yes_ask<1` | both | med bid | med ask |
|---|---|---|---|---|---|---|
| 0 °F | 10,678 | 100.0 % | 79.4 % | 79.4 % | 0.500 | 0.450 |
| 1–2 °F | 18,806 | 74.3 % | 100.0 % | 74.3 % | 0.210 | 0.120 |
| 3–4 °F | 16,268 | 60.0 % | 100.0 % | 60.0 % | 0.050 | 0.030 |
| 5–6 °F | 10,254 | 39.9 % | 100.0 % | 39.9 % | 0.030 | 0.010 |
| ≥7 °F | 6,888 | 27.4 % | 100.0 % | 27.4 % | 0.020 | 0.010 |

For every out-of-the-money bracket the ask is **always** there and the bid is
the binding constraint. The market is structurally willing to sell you a
long-shot and structurally unwilling to buy one.

### Far brackets (≥5 °F out) by time to close — the FR-3.1(a) constraint

| hours to close | rows | `yes_bid>0` | `yes_ask<1` | both | med bid | med ask |
|---|---|---|---|---|---|---|
| <1 h | 373 | **0.0 %** | 100.0 % | 0.0 % | — | 0.010 |
| 1–3 h | 761 | **0.0 %** | 100.0 % | 0.0 % | — | 0.010 |
| 3–6 h | 1,166 | **0.0 %** | 100.0 % | 0.0 % | — | 0.010 |
| 6–12 h | 2,712 | **1.5 %** | 100.0 % | 1.5 % | 0.015 | 0.010 |
| 12–24 h | 5,383 | 30.1 % | 100.0 % | 30.1 % | 0.020 | 0.010 |
| ≥24 h | 6,747 | 64.0 % | 100.0 % | 64.0 % | 0.020 | 0.020 |

### Winning bracket (0 °F) by time to close — the FR-3.1(b) constraint

| hours to close | rows | `yes_bid>0` | `yes_ask<1` | both | med bid | med ask |
|---|---|---|---|---|---|---|
| <1 h | 268 | 100.0 % | **1.9 %** | 1.9 % | 0.990 | 0.980 |
| 1–3 h | 546 | 99.6 % | **1.8 %** | 1.5 % | 0.990 | 0.965 |
| 3–6 h | 821 | 99.9 % | **5.0 %** | 4.9 % | 0.990 | 0.950 |
| 6–12 h | 1,650 | 100.0 % | **63.0 %** | 63.0 % | 0.950 | 0.850 |
| 12–24 h | 3,267 | 100.0 % | 99.7 % | 99.6 % | 0.450 | 0.470 |
| ≥24 h | 4,126 | 100.0 % | 100.0 % | 100.0 % | 0.360 | 0.370 |

---

## 4. Volume and open interest

| metric | value |
|---|---|
| rows with `volume > 0` | 54,054 / 62,932 = **85.9 %** |
| brackets that ever trade, per city-day | median **6 of 6** (min 6, max 6) |
| peak open interest per market | p10 4,162 · median **17,924** · p90 56,196 |
| volume per city-day (all 6 brackets) | p10 105,514 · median **195,966** · p90 575,242 |

**Every bracket trades on every city-day** across all 276 city-days. This is
not a dead market — median daily volume is ~196k contracts per city and peak
open interest is ~18k contracts per bracket. The problem is not that nobody is
there; it is that near settlement everyone is on the same side.

### By city

| city | rows | % two-sided | median spread | median volume/row |
|---|---|---|---|---|
| CHI | 15,613 | **66.8 %** | 1.0 ¢ | 133.5 |
| LAX | 16,739 | 58.4 % | 1.0 ¢ | **538.0** |
| MIA | 15,271 | **57.6 %** | 1.0 ¢ | 185.5 |
| NY | 15,309 | 60.1 % | 1.0 ¢ | 233.9 |

All four cities are usable; LAX carries the most flow per snapshot, CHI the
best two-sided coverage. No city needs to be excluded on liquidity grounds.

---

## 5. Thin / illiquid regions — flagged explicitly

1. **FR-3.1(a)'s target region is the thinnest region in the dataset.** The
   spec wants to sell brackets "≥ 4 °F from the calibrated forecast median".
   At ≥5 °F from the outcome, a YES bid exists in 39.9 % (5–6 °F) and 27.4 %
   (≥7 °F) of snapshots, falling to **0.0 % inside 6 hours of close**. A
   backtest that assumes a resting bid is always available there will
   manufacture fills that could not have happened.
2. **Far-bracket premium is 1–3 ¢, and the tick is 1 ¢.** Median far-bracket
   bid is 0.02–0.03. EC-5's mandatory **1 ¢ adverse-fill allowance therefore
   consumes 33–50 % of the gross premium on this trade shape** before any fee
   or model error. Workstream E must apply it — it is not a rounding detail
   here, it is most of the trade.
3. **The last 6 hours are not tradeable in either direction.** Far-bracket bid
   0.0 %, winner ask 1.9–5.0 %. Any strategy whose window is "the final hours
   before settlement" is modelling quotes that were not there.
4. **The 6–12 h window is the boundary case.** 26.4 % of rows two-sided
   overall; the eventual winner's ask available 63.0 % at median 0.85; far
   brackets effectively unsellable (1.5 %). If a shape is +EV anywhere, the
   evidence points at *buying the near-certain winner in the 6–12 h window*,
   not at *selling far brackets*.
5. **Availability, not spread, is the risk variable.** Median spread is 1 ¢
   in every single slice above. Modelling execution cost as "spread" and
   ignoring "is there a quote" would get the answer badly wrong in the
   optimistic direction.

---

## 6. Caveats on these statistics

- Rows are **hourly snapshots** (`period_interval=60`), specifically the
  **close** of each period's quote candle. A quote that existed for ten
  minutes inside an hour and vanished before the period ended is recorded as
  absent. Availability percentages are therefore a lower bound on
  *instantaneous* availability and an upper bound on *durable* availability;
  the columns `yes_bid_low` / `yes_ask_high` carry the worst intra-period
  quote for adverse-fill modelling. 1-minute resolution is available from the
  same endpoint if workstream E needs it (`--period-interval 1`, ~60× the
  request count).
- The distance banding is **hindsight-conditioned** (distance from the
  *settled* high). It describes market structure; it is not a signal.
- `has_quote` is defined as `yes_bid > 0 AND yes_ask < 1`. A genuine 0 ¢ bid
  or 100 ¢ ask is indistinguishable from an empty book in this feed; both are
  counted as no-quote. That choice is conservative in the direction of
  understating liquidity.
- Nothing here is interpolated, smoothed, or filled. A missing value is blank
  in the CSV and `NaN` in the loader, never 0.
