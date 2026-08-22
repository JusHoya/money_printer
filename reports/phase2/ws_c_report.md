# Workstream C — Kalshi ladder-history backfill + fee verification

**Phase:** 2 (Forecast engine & calibration + go/no-go)
**Date:** 2026-07-26 (fetch timestamps are UTC, i.e. early 2026-07-27)
**Branch:** `phase-2-forecast-calibration` (no git write commands were run)
**Companion documents:** [`ws_c_ladder_stats.md`](ws_c_ladder_stats.md),
[`ws_c_fee_verification.md`](ws_c_fee_verification.md)

---

## 1. The exit criterion this workstream exists to satisfy

PRD §8, Phase 2, exit criterion 5, verbatim:

> **5.** Go/no-go report exists as a dated artifact: EV per bracket-distance
> band under maker and taker pricing with fees and a 1¢ adverse-fill
> allowance, on ≥30 days of recorded ladders; it names which trade shapes (if
> any) are +EV and states the PROCEED/HALT decision per FR-2.4. Red-team can
> recompute one band's EV from raw inputs and match within rounding.

"Maker **and** taker pricing" requires a bid **and** an ask. The VM's own
harvest CSVs cannot supply that — of 803 archived `logs/data_*.csv` files, 776
carry only a single `Price` column, 27 carry bid/ask, and exactly one carries
the Phase 1 bracket columns. This workstream therefore sources the ladder
history from **Kalshi's own recorded market history** instead.

**Status: satisfied on the data side.** 69 consecutive days × 4 cities × 6
brackets are persisted with full bid/ask, settlement, and bracket semantics —
2.3× the ≥30-day floor.

---

## 2. Verified upstream contract

All endpoints below were exercised **anonymously** (no credentials; the empty
`KALSHI_KEY_ID` is fine and preferred for a bulk read) against
`https://api.elections.kalshi.com/trade-api/v2`, HTTP 200 throughout.

### 2.1 Market metadata

```
GET /markets?event_ticker={SERIES}-{%y%b%d uppercased}&limit=200
```

Event ticker format: `KXHIGHNY-26JUL17`. Returns exactly **6 markets per
city-day** carrying `ticker`, `status`, `strike_type`, `floor_strike`,
`cap_strike`, `yes_sub_title`, `open_time`, `close_time`, and for settled
markets `result` and `expiration_value`.

Traps confirmed:

- `volume` and `open_interest` are **`None`** on this endpoint. The populated
  fields are `volume_fp` / `open_interest_fp` (strings).
- `expiration_value` is a **string** (`"86.00"`), and can be an **empty
  string** on a settled market (observed once in 1,656 — see §4.3).
- `floor_strike` is absent on `less` markets, `cap_strike` absent on `greater`
  markets. Never coerce either to 0.

Ladder shape, verified over all 276 city-days: 2 °F-wide `between` brackets
flanked by one `less` and one `greater`, tiling the temperature axis with no
gap or overlap. Example `KXHIGHNY-26JUL17`: `T83` (≤82) · `B83.5` (83–84) ·
`B85.5` (85–86) · `B87.5` (87–88) · `B89.5` (89–90) · `T90` (≥91).

### 2.2 Candlesticks — the bid/ask source

```
GET /series/{SERIES}/markets/{MARKET}/candlesticks
    ?start_ts={unix}&end_ts={unix}&period_interval={1|60|1440}
```

- **Max 5,000 candlesticks per response.** A wider range returns HTTP 400
  `max candlesticks: 5000`. `fetch_candlesticks()` chunks the range so a
  single request can never hit it.
- Each entry carries `end_period_ts`, `volume_fp`, `open_interest_fp`, and
  three OHLC nodes: `price`, `yes_bid`, `yes_ask`.
- **The nested keys end in `_dollars`.** `c["yes_ask"]["close"]` returns
  `None`; the real key is `close_dollars`. Pinned by
  `tests/test_kalshi_history.py::test_candle_dollars_requires_the_dollars_suffix`.
- An empty book is reported as `yes_ask = "1.0000"` and/or
  `yes_bid = "0.0000"` — a sentinel, not a tradeable price.
- Only the YES book is served. The NO side is the **exact identity**
  `no_bid = 1 − yes_ask`, `no_ask = 1 − yes_bid` (a YES and a NO on the same
  market pay exactly $1.00 between them). Nothing is modelled or interpolated.

### 2.3 Series metadata (fee discriminator)

```
GET /series/{SERIES}  ->  {"series": {..., "fee_type": ..., "fee_multiplier": 1}}
```

See §5.

### 2.4 Retention — the hard upstream limit

Measured by scan on 2026-07-26 across all four series: `/markets` returns
market metadata only for events whose target date is **≥ 2026-05-18**. Older
events still resolve HTTP 200 from `/events` (the settled-event listing goes
back to `HIGHNY-21AUG06`, 1,811 events), but they come back with
`markets: []` — so `strike_type`, strikes, `result` and `expiration_value` are
gone, and no ladder can be built from them. `?with_nested_markets=true` does
not recover them either.

This is why the backfill is 69 days and not 180. It is an upstream retention
window, not a limitation of this module.
`scripts/backfill_ladders.py --probe-retention` re-measures it by bisection on
demand. **Practical consequence: the retention window is rolling, so the
2026-05-18 edge of this dataset ages out at one day per day. The data is
committed to `data/ladders/` precisely because it cannot be re-fetched later.**

---

## 3. Backfill coverage

**Command run:**

```powershell
$env:PYTHONPATH = "."
python scripts/backfill_ladders.py --start 2026-05-18 --end 2026-07-25
```

| | |
|---|---|
| target dates | **69** consecutive (2026-05-18 … 2026-07-25) |
| cities | **4** — NY/KNYC, CHI/KMDW, LAX/KLAX, MIA/KMIA |
| city-days requested | **276** |
| city-days with rows | **276** (**0 empty, 0 skipped**) |
| markets | **1,656** (6 per city-day, min 6 / max 6) |
| rows (market × hourly candle) | **62,932** |
| candles per market | min 29, median 38, max 41 |
| rows with a two-sided quote | **38,200 = 60.7 %** |
| HTTP requests | **1,936** |
| HTTP failures | **0** |
| `bracket_payoff` vs Kalshi `result` | **1,656 / 1,656** (union of two independent recomputes, §4.1) |
| Kalshi `expiration_value` vs CLI truth | **1,631 / 1,631 agree** |
| on disk | 276 CSVs, 15.34 MB + 0.53 MB manifest |

| series | files | dates | MB |
|---|---|---|---|
| KXHIGHNY | 69 | 2026-05-18 … 2026-07-25 | 3.68 |
| KXHIGHCHI | 69 | 2026-05-18 … 2026-07-25 | 3.82 |
| KXHIGHLAX | 69 | 2026-05-18 … 2026-07-25 | 4.11 |
| KXHIGHMIA | 69 | 2026-05-18 … 2026-07-25 | 3.73 |

Resolution is hourly (`period_interval=60`). Quality statistics — spread
distributions, one-sided availability, volume/OI, and the thin-region flags —
are in [`ws_c_ladder_stats.md`](ws_c_ladder_stats.md) and are **required
reading before any EV is computed from these rows**.

---

## 4. Correctness checks

### 4.1 `bracket_payoff` vs Kalshi's own `result`: 100 %, on two independent recomputes

Every market's settled outcome was recomputed through
`src.core.bracket_payoff.settles_yes()` from the API's `strike_type` /
`floor_strike` / `cap_strike`, then compared to Kalshi's `result` — twice,
against two independent sources of the observed daily high:

| recompute source | matched | checked | not covered |
|---|---|---|---|
| Kalshi `expiration_value` | **1,655** | 1,655 | 1 (blank `expiration_value`) |
| NWS CLI truth (Phase 1 files) | **1,632** | 1,632 | 24 (2026-07-25, truth not yet published) |
| **union** | **1,656** | **1,656** | **0** |

**Zero disagreements on either check**, and the two checks' blind spots do not
overlap, so all 1,656 markets are validated by at least one. This extends
Phase 1's 1,632/1,632 across 68 dates to a second, independent 1,656-market
sample drawn from a different endpoint, with no drop.

### 4.2 Kalshi `expiration_value` vs CLI ground truth: 1,631 / 1,631 agree

Cross-checked against the read-only Phase 1 files
`data/weather_truth/cli_daily_high_{KNYC,KMDW,KLAX,KMIA}.csv`. **Zero
disagreements.** Both readings are carried on every row (`expiration_value`
and `cli_high`) with a `truth_agrees` flag; where they disagree the module
reports it and does **not** pick a winner (there were none to report here).

### 4.3 The two denominator gaps, both explained

- **1 market of 1,656** — `KXHIGHNY-26JUN23-T78` — is `finalized` with
  `result: "yes"` and `settlement_value_dollars: "1.0000"` but a **blank**
  `expiration_value`, while its five siblings that day carry `"71.00"`. It is
  excluded from the `expiration_value` denominator (1,655, not 1,656) and
  listed in the manifest under `markets_missing_expiration_value`. The
  CLI-truth recompute validates it: KNYC 2026-06-23 high = 71 °F; a `less`
  bracket with `cap_strike = 78` pays YES for high ≤ 77 → YES, matching Kalshi.
- **24 markets** — all four cities on **2026-07-25** — have no CLI
  cross-check, because `cli_daily_high_*.csv` ends at 2026-07-24 (the NWS
  Climatological Report for a date publishes the next morning; Phase 1's
  backfill last ran 2026-07-25). Kalshi's `expiration_value` covers them.
  `1,656 − 24 − 1 = 1,631`, exactly the truth denominator.

### 4.4 Volume cross-check

Summing per-candle `volume_fp` for `KXHIGHNY-26JUL17` recovers **271,931**
contracts against the market-level `volume_fp` total of **273,761** — 99.33 %,
the residual being the partial period at market open. Per-market ratios span
0.98–1.00. The volume column is genuine per-period flow.

### 4.5 Reproducibility — the backfill was run three times

Run 1 and run 2 (≈40 minutes apart, independent full fetches) produced
**275 of 276 CSVs byte-identical**. The single difference,
`KXHIGHLAX/2026-07-25.csv`, was **row order only** — identical line count and
`diff <(sort a) <(sort b)` empty. Cause: `/markets` does not guarantee a
stable ordering of the six markets in an event. Fixed by sorting markets by
ticker before iteration (`src/data/kalshi_history.py::build_day_rows`); run 3
is the committed artifact. Aggregate totals were identical across all three
runs.

### 4.6 FR-1.1 compliance

Nothing in `src/data/kalshi_history.py` or `scripts/backfill_ladders.py`
inspects a ticker suffix letter. All bracket direction comes from the API's
`strike_type` / `floor_strike` / `cap_strike` via
`src.core.bracket_payoff.parse_bracket_spec`, which raises `BracketSpecError`
rather than guessing. Across all 1,656 markets: **0 bracket-spec errors**.

---

## 5. Fee findings (full detail in [`ws_c_fee_verification.md`](ws_c_fee_verification.md))

**Verdict for `KXHIGH*`, re-verified live 2026-07-27T03:22Z and against the
published schedule effective 2026-07-07:**

| | |
|---|---|
| **maker fee** | **$0.00** |
| **taker fee** | `ceil_to_cent(0.07 × C × P × (1 − P))` on the order total |
| **settlement fee** | **$0.00** |

Two independent sources agree. (1) Live API: all four
`/series/KXHIGH{NY,CHI,LAX,MIA}` report `fee_type: "quadratic"` with
`fee_multiplier: 1`, versus `"quadratic_with_maker_fees"` for `KXAAAGASM` —
the same field the 2026-07-24 review used to build `maker_fee_series.json`
(`review_2026_07_24/probe_kalshi_trades.py:31`), re-read today. (2) Published
schedule (SHA-256 `815e2d51…24c`, fetched 2026-07-27T03:19:01Z): the maker
formula's multiplier **defaults to M = 0**, and no `KXHIGH*` series — indeed
zero occurrences of `KXHIGH`, `temperature`, `weather`, or `Climate` in the
entire 12-page document — appears in the Non-Standard Fees table.

**Three defects found in `src/core/fee_calculator.py`** (orchestrator-reserved;
**not edited**, proposed diffs are in the fee document):

1. **F1 — floating-point ceil overstates the taker fee.** `taker_fee` matches
   only **14 of 21** published table rows; all 7 misses overstate by exactly
   $0.01 on the 100-contract column (`taker_fee(0.10, 100)` returns 0.64 where
   Kalshi publishes 0.63, because `0.07*100*0.10*0.90 == 0.6300000000000002`).
   Fix: `math.ceil(round(raw * 100, 9)) / 100.0` → **21/21**.
2. **F2 — a maker fee is charged where Kalshi charges none.** `maker_fee`
   applies 1.75 % unconditionally; on weather the true fee is $0.00. At
   P = 0.10 it invents a full cent — 10 % of the premium — on exactly the
   maker-first path FR-3.3 mandates.
3. **F3 — `ev_after_fees` double-charges a hold-to-settlement position.** It
   hard-codes a round trip (`2 * fee_per`). Weather holds to settlement
   (FR-1.5) and Kalshi charges no settlement fee, so only the **entry** fee
   applies.

Combined, the current model charges **$0.02/contract** on a maker-first
weather entry that actually costs **$0.00**. On far brackets priced at 2–3 ¢
that phantom cost exceeds the entire gross premium and could HALT a shape that
is genuinely +EV.

**Recommendation for workstream E:** use the two functions given in
§1 of the fee document (`kxhigh_maker_fee`, `kxhigh_taker_fee`) directly for
the Phase 2 report rather than routing through `fee_calculator`, and land the
module fix before Phase 3 execution. Also note the fee is rounded on the
**order total**, so per-contract cost falls with size: at P = 0.05, C = 1 the
taker fee is 1 ¢/contract, at C = 20 it is 0.35 ¢/contract. Computing EV at
C = 1 overstates far-bracket taker cost by roughly 3×.

---

## 6. Loader API for workstream E

```python
from src.data.kalshi_history import load_ladders, load_manifest, LADDER_COLUMNS

df = load_ladders(
    root=LADDER_DIR,          # default: <repo>/data/ladders
    cities=None,              # ["NY","CHI","LAX","MIA"] or series tickers; None = all
    start_date=None,          # inclusive "YYYY-MM-DD" on target_date
    end_date=None,            # inclusive
    quoted_only=False,        # True keeps only rows with a two-sided quote
)                             # -> pandas.DataFrame

manifest = load_manifest()    # -> dict (provenance, per-day counts, failures)
```

`quoted_only` defaults to **False** deliberately: the unquoted rows are the
finding (see the stats report), and a loader that hides them by default would
let an EV model silently assume a market that was not there.

### Columns

| column | meaning |
|---|---|
| `series`, `city`, `station`, `target_date`, `event_ticker`, `market_ticker` | identity |
| `ts_utc` | tz-aware UTC end of the hourly period |
| `minutes_to_close`, `close_time_utc` | time to the market's `close_time` |
| `strike_type`, `floor_strike`, `cap_strike`, `yes_sub_title` | FR-1.1 bracket semantics, verbatim from the API |
| `yes_bid`, `yes_ask` | **close** of the period's quote candle, decimals 0–1 |
| `no_bid`, `no_ask` | exact complements `1 − yes_ask`, `1 − yes_bid` |
| `last`, `price_mean` | period close and mean traded price |
| `yes_bid_low`, `yes_ask_high` | worst intra-period quotes — use these for the adverse-fill model |
| `volume` | contracts traded **during** the period |
| `open_interest` | OI at period end |
| `has_quote` | `yes_bid > 0 AND yes_ask < 1` |
| `result`, `expiration_value` | Kalshi's settlement |
| `cli_high` | NWS CLI daily high (Phase 1 truth), `NaN` where unavailable |
| `recomputed_yes_expval`, `recomputed_yes_cli` | `bracket_payoff` recompute against each truth source |
| `payoff_matches_kalshi`, `truth_agrees` | the two agreement flags |

Numeric columns are floats and are **`NaN` where the API supplied no value —
never 0**. Booleans load as real `bool`. Blank means missing, always.

### Storage

One CSV per (series, target date): `data/ladders/<SERIES>/<YYYY-MM-DD>.csv`,
plus `data/ladders/manifest.json`. One file per city-day means a partial rerun
rewrites exactly one day and every per-day row count in the manifest is
verifiable against the file on disk.

### Provenance manifest

`data/ladders/manifest.json` records: API base and auth mode, the exact
endpoint templates and request parameters, the date range, the city→series→
station map, live `series_metadata` (including `fee_type`) with fetch
timestamps, the truth-source path, aggregate totals, and a `days[]` array with
**one entry per requested city-day** — `markets`, `markets_with_candles`,
`rows`, `quoted_rows`, both payoff denominators/numerators, truth checks and
disagreements, per-market detail (ticker, strikes, result, candle count),
HTTP failures, `empty` + `empty_reason`, the CSV path, and a UTC fetch
timestamp. **A day that came back empty appears as empty; nothing is silently
skipped.** Current run: `empty_days: []`, `http_failures: []`,
`bracket_spec_errors: []`.

---

## 7. Reproduction

```powershell
$env:PYTHONPATH = "."
$env:OMP_NUM_THREADS = "2"; $env:OPENBLAS_NUM_THREADS = "2"
$env:MKL_NUM_THREADS = "2"; $env:NUMEXPR_NUM_THREADS = "2"

# Full backfill (~1,936 requests, ~12 min at the 0.12 s inter-request floor)
python scripts/backfill_ladders.py --start 2026-05-18 --end 2026-07-25

# Ladder-quality statistics
python scripts/backfill_ladders.py --stats

# Re-measure the upstream retention boundary (bisection, ~9 requests/series)
python scripts/backfill_ladders.py --probe-retention

# Tests (this file only -- never the full suite on this machine)
python -m pytest tests/test_kalshi_history.py -v
```

`tests/test_kalshi_history.py`: **31 tests, all passing, all offline** against
recorded fixtures in `tests/fixtures/ladders/`. They pin the `_dollars` trap,
the no-quote sentinels, the NO-side identity, `None`-never-becomes-0, the
complete-partition ladder shape, the `bracket_payoff`-vs-`result` agreement on
the fixture, the empty-day-is-reported behaviour, the truth-disagreement
reporting, the CSV↔loader round trip, and the published fee table.

---

## 8. Files delivered

| path | role |
|---|---|
| `src/data/kalshi_history.py` | client, row assembly, persistence, `load_ladders` |
| `scripts/backfill_ladders.py` | CLI: `--start/--end/--days`, `--stats`, `--probe-retention` |
| `tests/test_kalshi_history.py` | 31 offline tests |
| `tests/fixtures/ladders/*.json` | recorded API payloads + the published fee table |
| `data/ladders/<SERIES>/<date>.csv` | 276 files, 62,932 rows, 15.3 MB |
| `data/ladders/manifest.json` | provenance |
| `reports/phase2/ws_c_report.md` | this document |
| `reports/phase2/ws_c_ladder_stats.md` | deliverable 2 |
| `reports/phase2/ws_c_fee_verification.md` | deliverable 3 |

Nothing outside these paths was created or modified. `src/core/**`,
`src/data/kalshi_provider.py`, `src/backtest/**`, `data/weather_truth/**`,
`requirements.txt`, `.gitignore`, `PRD.md`, `CLAUDE.md`, `.env`, and the
workstream A/B files were read only.

---

## 9. Gaps, risks, and things this workstream did NOT establish

1. **69 days, not 180.** Kalshi's `/markets` metadata retention stops at
   2026-05-18. This clears EC-5's ≥30-day bar with 2.3× margin, but FR-2.2's
   ≥60 paired forecast-vs-CLI days for calibration must come from the Phase 1
   CLI backfill (209 days available), **not** from this ladder dataset.
2. **The window is rolling and shrinking.** One day of ladder history becomes
   unrecoverable every day. `data/ladders/` must be committed; it cannot be
   regenerated.
3. **Hourly resolution.** A quote that existed for part of an hour and was
   gone by period close is recorded as absent, so availability percentages are
   a lower bound on instantaneous availability. 1-minute data is available
   (`--period-interval 1`) at ~60× the request count if workstream E needs
   finer execution modelling.
4. **`data/ladders/` is not covered by `.gitignore`** — verified with
   `git check-ignore`, so all 276 CSVs and the manifest will be
   committed as written (15.87 MB total). That is the intended outcome given point 2, but the
   orchestrator should make it a conscious choice rather than an accident.
   `.gitignore` is not this workstream's file to edit.
5. **This workstream computed no EV and states no PROCEED/HALT.** It supplies
   inputs and their honest quality profile. The §0 headline of the stats
   report is a *liquidity* finding, not a verdict.
6. **Only 4 cities.** `KXHIGHDFW` exists in `src/data/harvester.py`'s
   `WEATHER_SERIES` but is not in `WEATHER_CITIES` and has no CLI truth file,
   so it is out of scope.
7. **Anonymous reads only.** No order was placed and no authenticated endpoint
   was touched. If Kalshi ever gates candlesticks behind auth, the backfill
   needs a signed key; `KalshiHistoryClient` already routes headers through
   `KalshiProvider._get_authenticated_headers` and will sign automatically once
   `KALSHI_KEY_ID` is populated.
8. **Not measured here:** whether resting-order fills are achievable at the
   quoted prices (FR-3.3's fill model), what the top-of-book depth is at those
   quotes (the candlesticks feed carries no depth — `KalshiProvider.
   fetch_orderbook` gives depth only for *live* markets, never historically),
   and whether the calibrated model can identify the winning bracket in the
   6–12 h window. All three are open and all three matter to the verdict.
   **Depth is the most consequential of these**: the stats report shows quotes
   exist, but nothing in this dataset says how many contracts sit behind them.
