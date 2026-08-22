# Phase 4 — inter-agent data contract (AAA gas convergence)

Fixed by the orchestrator before implementation began. Every Phase 4 workstream
reads and writes only through these shapes. Do not change a column name, a unit,
or a function signature here without the orchestrator's agreement — three
workstreams are built against this document concurrently.

## 0. What the contract settles on (verified against the live API)

`KXAAAGASM` settles on the **AAA national average retail price for regular
gasoline, as published by AAA on one specific calendar date** — not on a
month-to-date or monthly average. Verified 2026-07-29 from
`GET /markets/KXAAAGASM-26AUG31-4.60`:

```
strike_type              : greater
floor_strike             : 4.6
rules_primary            : "If average regular gas prices for United States are
                            strictly greater than $4.60 on Aug 31, 2026
                            according to AAA, then the market resolves to Yes."
close_time               : 2026-08-31T03:59:00Z   (23:59 ET Aug 30)
expected_expiration_time : 2026-08-31T14:00:00Z   (10:00 ET Aug 31)
```

Three consequences that are easy to get wrong:

1. **`strictly greater than`.** YES pays iff `value > floor_strike`. A settle
   exactly equal to the strike pays NO. Every payoff path must use `>`, never
   `>=`. This is the Phase 4 analogue of the Phase 1 bracket-semantics trap.
2. **Trading closes the evening BEFORE the settlement value is published.** The
   final decision is made on a projection, never on the settled value. Any
   backtest that reads the target date's AAA value at or before its own decision
   time is lookahead and invalidates the artifact.
3. **All open `KXAAAGASM` markets observed are `strike_type: greater`.** Do not
   write direction inference from the ticker suffix — read `strike_type` and
   `floor_strike` from the API, per PRD FR-1.1 / Phase 1 EC-2. An AST guard
   already scans for suffix inference; it will fire.

## 1. Persisted series (owned by WS-A)

All under `data/gas_truth/`. UTF-8, LF endings, header row present, sorted
ascending by date. Every row carries its own provenance — a value with no
retrievable source is not admissible evidence.

### `aaa_daily_national.csv`

```
date,value,source,source_url,fetched_at,raw_sha256,quality
```

| column | meaning |
| --- | --- |
| `date` | ET calendar date the value is published *for*, `YYYY-MM-DD` |
| `value` | USD/gal, 3 decimals, e.g. `4.106` |
| `source` | `aaa_live` \| `aaa_wayback` |
| `source_url` | exact retrievable URL. For Wayback, the full dated snapshot URL |
| `fetched_at` | UTC ISO-8601 of retrieval |
| `raw_sha256` | SHA-256 of the raw response bytes the value was parsed from |
| `quality` | `ok` \| `suspect` — never `interpolated`; see §1.1 |

### 1.1 Gaps are recorded as absent, never as invented rows

Wayback coverage is ~24 of 30 days per month. A missing day is a **missing
row**. Do not write an interpolated value into `aaa_daily_national.csv`. If a
model needs a regular grid it interpolates in memory and reports how many days
it filled. A row whose parse looked wrong (non-monotonic vs neighbours by
> $0.15/day, or outside `[1.00, 9.00]`) is written with `quality=suspect` and
excluded from fits by default.

### `eia_weekly_regular.csv`

```
week_ending,value,source,source_url,fetched_at
```

EIA weekly U.S. regular all-formulations retail price. `week_ending` is the
Monday EIA dates the observation to, `YYYY-MM-DD`.

### `rbob_daily.csv`

```
date,value,source,source_url,fetched_at
```

Daily RBOB spot, USD/gal. Treated as a **lagged covariate** — retail follows
wholesale with a multi-day lag; that lag is a fitted parameter, not an assumed
constant.

### `manifest.json`

```json
{
  "generated_at": "<UTC ISO-8601>",
  "series": {
    "aaa_daily_national": {"rows": 0, "first": "", "last": "",
                            "by_source": {"aaa_live": 0, "aaa_wayback": 0},
                            "suspect": 0, "content_hash": ""},
    "eia_weekly_regular": {"rows": 0, "first": "", "last": "", "content_hash": ""},
    "rbob_daily":         {"rows": 0, "first": "", "last": "", "content_hash": ""}
  },
  "corrections": []
}
```

`content_hash` is SHA-256 of the CSV bytes. Corrections are **appended** to
`corrections`, never applied by editing a committed row — an as-run artifact
whose hash chain is broken stops being evidence.

## 2. Projection interface (owned by WS-C, consumed by WS-D)

`src/models/gas_projection.py`

```python
@dataclass(frozen=True)
class GasProjection:
    target_date: date      # the settlement date being projected
    as_of: date            # last observation used; MUST be < target_date
    point: float           # projected AAA national average, USD/gal
    sigma: float           # 1-sigma of the projection error, USD/gal
    ci_low: float          # 95% CI
    ci_high: float
    lead_days: int         # target_date - as_of
    n_train: int           # observations in the fit
    n_interpolated: int    # days filled in memory to build the grid
    model_version: str     # e.g. "lagdrift_v1"
    inputs_hash: str       # SHA-256 over the exact input rows used


def project(as_of: date, target_date: date, series: GasSeries) -> GasProjection:
    """Project the AAA national average on target_date using only rows dated
    <= as_of. Raises GasDataUnavailable rather than returning a default."""


def prob_above(proj: GasProjection, floor_strike: float) -> float:
    """P(settle > floor_strike). STRICTLY greater. Must be monotonically
    decreasing in floor_strike."""
```

`project()` **aborts** on insufficient or stale input — it never falls back to a
default or to the last observed value silently. Per
`abort-on-missing-critical-input`: a plausible default here is systematically
wrong and would size a trade.

## 3. Signal contract (owned by WS-C)

A gas entry may be produced only when all hold, each independently logged with
its measured value:

| gate | rule |
| --- | --- |
| window | `0 < (settlement_date - today) <= GAS_FINAL_WINDOW_DAYS` (default 14) |
| divergence | `abs(prob_above(proj, strike) - market_price) >= 0.08` (8pt) |
| data freshness | newest `aaa_daily_national` row is `<= 2` days old |
| fee-aware EV | EV net of fees computed via `src/core/fee_calculator.py` with the **symbol** passed, and `> 0` |
| sizing | small; capped per `RiskManager`, never scaled by the divergence |

`KXAAAGASM` is in `KNOWN_MAKER_FEE_SERIES` (`src/core/fee_calculator.py:78`) —
it is one of the few series that **does** bill resting liquidity. Any EV that
prices a maker fill as free is wrong. Always thread the symbol.

Every rejection is logged at INFO with a reason code and the measured value that
failed, so a gate that silently rejects everything is visible from the logs
alone (`make-silent-rejections-observable`).

## 4. File ownership (same-tree, disjoint)

Concurrent agents share one working tree. Touch only your own files; the
orchestrator owns the shared ones.

| workstream | owns |
| --- | --- |
| WS-A data | `src/data/aaa_provider.py`, `src/data/energy_covariates.py`, `scripts/backfill_gas_history.py`, `data/gas_truth/**`, `tests/test_aaa_provider.py`, `tests/test_gas_backfill.py` |
| WS-B truth | `src/data/gas_settlement.py`, `scripts/reconcile_gas.py`, `tests/test_gas_semantics.py`, `tests/test_gas_reconcile.py`, `tests/fixtures/gas/**` |
| WS-C model | `src/models/gas_projection.py`, `src/strategies/gas_convergence.py`, `src/bots/gas_bot.py`, `tests/test_gas_projection.py`, `tests/test_gas_strategy.py` |
| WS-D report | `scripts/gas_backtest.py`, `reports/phase4/**`, `tests/test_gas_backtest.py` |
| orchestrator | `src/bots/registry.py`, `PRD.md`, `requirements.txt`, `.env.example`, `CLAUDE.md`, this file |

## 5. Politeness and provenance when fetching

- `gasprices.aaa.com/robots.txt` allows the landing page (only `/wp-admin/` is
  disallowed) and asks `Crawl-delay: 10`. Honour 10 s between requests; the
  live recorder needs exactly **one** request per day.
- Send a `User-Agent` naming the project and a contact address.
- Wayback CDX/replay: keep concurrency at 1 and sleep between requests. A
  backfill is a one-time cost; there is no reason to hammer it.
- EIA's JSON API returns 403 without a key. Use the **keyless** bulk series
  files rather than adding a credential dependency; record the exact URL.

## 6. Ratified deviations and additions (appended by the orchestrator)

Recorded after implementation. Each was proposed by a workstream, reviewed, and
accepted; the original text above is left intact so the change is legible.

### 6.1 `fetched_at` means "when the source produced the bytes"

Contract §1 said "UTC ISO-8601 of retrieval". WS-A instead records the instant
the source *published or captured* the bytes — the Wayback capture instant, or
EIA's per-series `last_updated`. **Accepted, and it is the better rule:** it
makes all three CSVs byte-reproducible, so `content_hash` identifies the data
rather than the moment someone happened to download it. Wall-clock retrieval
time is not evidence of anything; the capture instant is.

### 6.2 Additive artifacts, not schema changes

Two files exist beside the three contract CSVs and are **not** part of §1:

- `data/gas_truth/backfill_audit.json` — per-month coverage, the enumerated
  missing days, the publication-hour sweep profile, and the cross-check
  evidence. This is the audit trail for the backfill.
- `data/gas_truth/scrape_failures.json` — a persisted per-ET-day failure record.
  WS-A found the freshness gate alone would **not** have blocked signals on a
  scrape-failure day, because a fresh row from earlier could still satisfy it.
  This file blocks signals across process boundaries and clears on a same-day
  success. It is load-bearing for exit criterion 1's second half.

### 6.3 The publication hour is piecewise, and its practical effect is small

**This section originally overstated the finding. Corrected here from red-team
measurements, which supersede the earlier text.**

What is solid: the orchestrator's brief said AAA "publishes each morning ET" and
WS-A initially assumed 07:00. That assumption was wrong, the backfill's gate
**refused to write** under it rather than shipping the series, and the shipped
attribution is a three-era piecewise schedule (`2022-01-01 -> 5 ET`,
`2024-01-01 -> 6 ET`, `2025-04-01 -> 3 ET`) — **not** the single 03:00 ET constant
this section first claimed, which was true only of the initial 14-month window.

What was overstated: the provider docstring and an earlier version of this
section said a single global constant "would misattribute entire years by a full
calendar day." It does not. **This is the decision record for how that effect is
measured** — two distinct metrics exist, neither dominates the other, and every
document that quotes a number must name which one it means (per
`one-decision-record-for-cross-document-state`, since three documents quote these
figures):

| metric | definition | hour-5 constant vs shipped schedule | worst case, hours 3-7 |
| --- | --- | --- | --- |
| `rows_redated` | a row that survives in both variants but carries a different `date` | **9 of 1,550 (0.58%)** | 13 (0.84%, hour 7) |
| `rows_moved` | series-level: also counts a day gained or lost, and a day whose winning capture changes | 25 (1.61%) | 52 (3.35%) |
| `rows_structurally_immune` | captures at ET hour >= 12, immune to any candidate hour | **1,401 / 1,550 = 90.39%** | — |

Neither metric dominates: at hour 3 there are 8 re-dated rows but only 7
series-level differences. The denominator is **1,550**, not 1,551 — the
`2026-07-28` row was removed (§6.7).

The immunity is structural: the capture sampler draws its anchors from a
12:00-20:00 ET window, i.e. by construction from where no candidate hour
discriminates. The piecewise schedule is the best available estimate; it is
**not** load-bearing and must not be described as if it were.

The era evidence is now reproducible, having previously not been. The audit
persists `publication_schedule_alternatives`, scoring seven **named** schedules on
every run (measured 2026-07-29):

```
3-era-shipped            3.12%   <- best of set
5-era-quarterly-2026Q2   3.38%
3-era-boundary-2025-01-01 3.44%
4-era-per-capture-year   3.44%
2-era                    3.57%
single-constant-5        4.92%
single-constant-3        6.43%
```

A test asserts nothing in the set beats the shipped schedule, so a future change
that invalidates this table goes red. Two honest limits remain:

- The **originally cited** rates (4-era 3.05%, 5-era 2.99%) had no artifact behind
  them and left "era" undefined; they are withdrawn. What is now reproducible is
  narrower: `2025-04-01` beats `2025-01-01` by 0.32pp on the same captures.
- The eras **may still be an artifact of Wayback's crawl schedule** rather than a
  change in AAA's behaviour. The share of captures that discriminate between
  candidate hours rises monotonically 2.2% -> 2.0% -> 10.4% -> 25.0% -> 44.1%
  across 2022-2026, so an apparent "era" can reflect which part of the
  early-morning clock the crawler sampled. Committed evidence does not settle it,
  and it does not need to — see the immunity figure above.

Attribution is nonetheless **confirmed correct** by a genuinely day-sensitive
external check: the series peaks at **5.016 on 2022-06-14**, AAA's documented
all-time-record date, with 5.014 on both neighbours — a plus-or-minus one-day
shift would move the peak.

**Do not cite the EIA cross-check as evidence of date alignment.** The AAA-EIA
methodological offset is ~13.5 mills while daily AAA moves are 1-5 mills, so that
check validates the *level* (and catches a wrong-column parse) but is blind to a
one-day shift.

The Yesterday-vs-Current chain check remains the right instrument and earned its
place — it is what refused the wrong constant. The lesson is narrower than first
written: it caught a real error early, and the error's blast radius was bounded
by a sampling choice made for unrelated reasons.

### 6.4 RBOB is Los Angeles spot, and that is a known compromise

Every NY Harbor RBOB series in EIA's keyless bulk file is a **futures** series
ending 2024-04-05, covering none of the Phase 4 window. The default covariate is
therefore `PET.EER_EPMRR_PF4_Y05LA_DPG.D` — LA RBOB spot. **LA is a
CARB-specific benchmark and an imperfect national wholesale proxy.** It is a
fitted covariate, not a truth source, so an imperfect proxy degrades the model
rather than corrupting the record — but no workstream may inherit it silently.
WS-D compares it against the Gulf Coast and NY Harbor conventional alternatives
and reports EV sign-stability across them.

### 6.5 Hold-to-settlement is one enforcement point, widened

Gas joins weather under a single guard (`is_held_to_settlement`) rather than
gaining a parallel one — a second guard is a second thing to forget. The Phase 1
names are retained as aliases bound to the *same* objects, so weather behaviour
is bit-identical and the Phase 1 goldens and mutation kill counts are unchanged.
The two families are provably disjoint (`KXHIGH` vs `KXAAAGAS`).

Gas truth grace is measured from the settlement date's **start**, not its close
as weather does, because gas publishes on the morning *of* the date; measuring
from the close would hold the age negative and could never escalate a late
value. The 36 h constant is **judgement, not measurement** — there is no
observed distribution of AAA publication latency behind it. Registered as a
deferral rather than presented as a derived bound.

### 6.6 Scope: monthly only, with the finding recorded

`KXAAAGASM` (monthly) is the Phase 4 target. The sibling series settle on the
same published AAA number under identical rule text:

| series | cadence | fee type | maker fee |
| --- | --- | --- | --- |
| `KXAAAGASM` | monthly | `quadratic_with_maker_fees` | yes |
| `KXAAAGASW` | weekly | `quadratic` | no |
| `KXAAAGASD` | daily | `quadratic` | no |

So the two cheaper-to-make instruments are **out of PRD scope**, and the daily
series yielded 67 settlement dates in two months against the monthly series' 2.
Recorded as a finding for the PRD owner; not acted on, because widening scope
mid-sprint is how exit criteria stop meaning anything.

`src/data/gas_settlement.py` nonetheless governs all three series — that is what
supplied the 15 exactly-on-strike settlements proving the strict-`>` boundary,
none of which the monthly series alone could provide.

### 6.7 The `2026-07-28` row was removed, and the reasoning cut both ways

A red-team found this row cited Wayback snapshot `20260728110106`, which **is not
in the CDX index**. Wayback answers that URL with **HTTP 200 serving the 07-27
capture** (`Memento-Datetime: Mon, 27 Jul 2026 11:01:14 GMT`), so the provenance
URL *resolves* while silently validating the wrong day — decorative provenance,
which is the one thing the `raw_sha256`/`source_url` columns exist to prevent.

**The evidence was not one-sided, and the argument against removal is recorded
because it is the stronger-looking one.** A local temp-dir cache file
`20260728110106.html` exists whose sha256 **does** match the recorded hash, is
95,193 bytes (distinct from both neighbours), parses to Current 4.099 /
Yesterday 4.110, and chains correctly against 07-27 (Current 4.110) and 07-29
(Yesterday 4.099). Sibling subresources of that same crawl are still in CDX. So
the crawl almost certainly happened and the landing-page capture was later
de-indexed: **4.099 is probably the correct value.**

It was removed anyway. An uncommitted file in an OS temp directory is not
retrievable provenance, and a value a third party cannot re-obtain does not belong
in a file whose entire purpose is provenance. The row was **not** re-derived from
the 07-27 capture, and no URL was invented. `manifest.json`'s append-only
`corrections` list carries the removal with four evidence items **including the
case against it** — per `correct-the-record-not-the-as-run-artifact`, the record
is corrected additively rather than by quietly editing history.

Three guards now make this class of defect self-reporting:

1. `fetch_snapshot` reads `Memento-Datetime` and treats a served capture
   disagreeing with the requested one by more than 2 s as `CAPTURE_MISS` — no row,
   no cache entry. This is what would have caught it at write time.
2. `reconcile_with_disk()` compares a run's series against the rows already on
   disk **before** the write. Post-write it is a tautology, and under
   `--regenerate` it would pass while silently dropping rows.
3. A committed-artifact test asserts no date the audit calls *missing* has a row
   in the CSV — the disagreement (audit said 121 missing, file had 120) is
   precisely how this row hid for as long as it did.

**Consequence for exit criterion 1 that looks alarming and is not:** removing
07-28 makes 07-29 an island, so `--status` reports "run ending at the newest row
= 1 day". The longest *trailing contiguous* block is 55 days ending 2026-07-27 and
the longest run anywhere is 355 days (92 excluding `suspect` rows), so
"≥14 consecutive daily values persisted with provenance" remains comfortably
satisfied on the archival reading. See §6.8 for why the archival reading is not
the one that closes EC-1.

### 6.8 EC-1's "≥14 consecutive daily values" means live operation

Adjudicated against the PRD's own house style, which distinguishes the two
readings consistently:

- **Backfill criteria say "backfill".** Phase 1 EC-4: "**Backfill:** ≥180 days of
  CLI daily highs per city persisted".
- **Recorder criteria name the component and a consecutive-day count.** Phase 1
  EC-3: "Settlement recorder **has run** ≥3 consecutive days on the VM". Phase 2
  EC-1: "**Ensemble provider** returns ≥20 members … on **≥5 consecutive days**".

Phase 4 EC-1 names "**AAA provider**" plus a consecutive-day count — the recorder
pattern, not the backfill pattern. It is the acceptance test for FR-4.1, whose
verb is "**scrapes**" and whose requirement is "values persisted **daily**".
Two further signals point the same way: EC-1's second half is unambiguously about
live operation ("an induced scrape failure … **that day**"), and 14 is exactly
`GAS_FINAL_WINDOW_DAYS`, i.e. "the provider must demonstrably feed one full
trading window".

**Therefore archived Wayback history does not close EC-1's first half, however
long its runs are.** Only 14 consecutive days of live recorder operation does, and
that is a clock. It is registered as a dated deferral, not waived.

Two things must be true before that clock can even start, and neither was true
when this section was written:

1. **`aaa_live` count is 0**, and `upsert_observations` is first-writer-wins — so
   the live recorder *structurally cannot* add a row for any date the backfill
   already covers. The backfill covers every date through today. This is the
   steady state, not an accident.
2. **No scheduler exists.** There is no cron, systemd unit, or timer anywhere in
   the repo invoking the recorder.

The live cron should run in the **12:00-20:00 ET** window. Since §6.3's fix this is
a preference rather than a correctness dependency — `today_et()` and
`attribute_et_date()` now share one implementation, so the failure key and the row
date cannot drift apart — but it remains the window where no candidate publication
hour changes attribution.
