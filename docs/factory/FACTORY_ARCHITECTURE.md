# MaskForge-S — Strategy Factory Architecture (synthesis, 2026-09-02)

Base design: **MaskForge** (consensus winner: Statistician 56, Governance 59; Systems 52
with PROBE at 60). Grafts taken from PROBE, FORGE, LANEWORKS and the three judge lenses
are marked `[graft: <source>]`. Every judge-named fatal flaw is resolved in §12 or
explicitly accepted there. Nothing in this document modifies `src/backtest/ev_analysis.py`,
`src/core/risk_manager.py`, `src/bots/mixins.py` gate order, or the fee booking in
`src/core/matching_engine.py`.

House rules this design is bound by (HANDOFF.md:105-126, PRD FR-0.2/FR-5.2, REVIVAL R1/R3):
settlement-true realized PnL after real fees with pessimistic fills is the ONLY fitness;
no lookahead (vintages, injected clocks, no wall-clock reads in strategies); out-of-sample
walk-forward with clustering on the independent unit and an explicit multiple-testing
correction; the factory never trades; winners go only to the maia sandbox paper leg and
from there through the pre-registered gate; no risk gate is weakened; no ML training in
the runtime process.

---

## 0. One-paragraph summary

A candidate strategy is a 13-gene integer rule genome (typed column predicates over the
settlement-true opportunity frame that `build_opportunity_frame` already builds from
ladders + forecast vintages + walk-forward calibration + CLI truth). Fitness is exactly
what `evaluate_shape` computes today (date-clustered realized PnL after C=20 quadratic
fees, taker path, `price_paid = quote + $0.01`), re-implemented as a numpy kernel proven
equal to `evaluate_shape` to 1e-9 before any genome is bred. Selection uses the lower
bootstrap bound on the **search window only**; the headline number is the pooled
out-of-sample return of the pre-registered picker over three **anchored walk-forward
campaigns** with a 2-day embargo. Multiplicity is controlled by a White Reality Check /
Hansen SPA over every distinct phenotype in the run's ledger, Holm across registered
families, and three control runs (snapshot-efficient null, residual-shuffle null,
planted-edge positive control). Evolution is a CPU-only fork-pool on 16 of alcyone's 20
cores, never touching the GPU or mp-vllm. A winning genome compiles to `GenomeStrategy`,
which evaluates the *same* `to_mask` function on live ladder rows in the maia sandbox,
behind every existing gate, and is scored again — once — on sealed holdouts before the
FR-5.2 paper gate. Everything the factory prints is recomputable from the run directory.

---

## 1. Components (module paths)

All new code lives under `src/factory/` (offline-only), `src/strategies/genome_strategy.py`
(runtime), `scripts/factory.py` (CLI), `configs/factory/` (tracked configs and promoted
genomes), `deploy/spark/` (compose service + systemd unit), `hermes_plugin/` (two tools).

### 1.1 Offline (alcyone lab image only)

| Module | Responsibility |
|---|---|
| `src/factory/lanes/base.py` | `Lane` ABC: `independent_unit` (weather=`target_date`, gas=`settlement_date`, mention=`event_ticker`), `status() -> READY / NOT_PROMOTABLE(reason, n_units) / NOT_READY(reason)`, `build_frames(config)`. `[graft: LANEWORKS]` |
| `src/factory/lanes/weather.py` | The only READY lane. Calls the **unchanged** chain: `kalshi_history.load_ladders` → `ev_analysis.load_forecast_archive` → `forecast_vintage_table` → `WalkForwardCalibrator` → `build_probability_table` → `build_opportunity_frame(EVConfig(contracts=20, adverse_fill_dollars=0.01, calibration_mode='walk_forward', embargo_days=1))`, then applies the factory hardening of §4.2 on top. |
| `src/factory/lanes/gas.py` (F5) | Wraps `scripts/gas_backtest.py` replay with BOTH `clock=` and a new `gate=` injection (gas_convergence.py:357 / aaa_provider.py:1637 read `datetime.now()`). Status NOT_PROMOTABLE (14 settlement events). |
| `src/factory/lanes/{mention,tweets,crypto_annual}.py` | NOT_READY stubs quoting `reports/factory/coverage.json`. |
| `src/factory/frame.py` | `FrameSet` = (`parity`, `search`, `gefs_twin`) as slim contiguous numpy arrays split into **visible** (genome-facing) and **hidden** (scorer-only) matrices; provenance + sha256; cutoff / no-lookahead / truth asserts. `[graft: PROBE allowlist, FORGE visible/hidden]` |
| `src/factory/features.py` | Single source of truth for every genome-visible column derivation (window label, band, far_margin, quote, price_paid, sandbox_admissible, lagged features in v2). Imported by BOTH `frame.py` and `genome_strategy.py`. numpy-only (no pandas/scipy) so the Pi image can import it. `[graft: FORGE]` |
| `src/factory/genome.py` | `GENE_SPEC` (versioned), `Genome` dataclass, `encode/decode` to `np.int16`, `random/mutate/crossover/repair`, `to_mask(genome, F)` — works on a frame (arrays) and a single row (scalars) with the same code, `phenotype_hash(frame)`, `to_json/from_json`. numpy-only. |
| `src/factory/fitness.py` | Kernel `score(frame, mask, date_set, boot_index) -> FitnessResult`; `score_reference()` wraps `ev_analysis.evaluate_shape` for the parity test; constraint checks; BSS-on-trades. |
| `src/factory/folds.py` | Anchored walk-forward campaigns (headline) and blocked 5-fold with 2-day purge (diagnostic); date-code → membership arrays; workers receive frames with validation rows **physically removed**. |
| `src/factory/ledger.py` | Write-then-evaluate parquet rows per generation (`UNSCORED` never missing), phenotype dedupe, per-date PnL vectors for RC/SPA. `[graft: LANEWORKS]` |
| `src/factory/evolve.py` | Population, tournament, elitism, niching, immigration, kill codes, checkpoints, resume, `status.json` (timestamp-free). |
| `src/factory/null.py` | Control frames: snapshot-efficient, residual-shuffle, planted-edge (§6.4). |
| `src/factory/multiplicity.py` | White RC / Hansen SPA over the ledger; Holm; clustered Deflated Sharpe (full Bailey–López de Prado on the date series — NOT `validation_harness.deflated_sharpe`, which is `sqrt(2 ln N)` and unclustered). `[graft: FORGE, PROBE]` |
| `src/factory/registry.py` | Append-only `reports/factory/registry.jsonl`: a family line is written BEFORE any result exists; PROPOSED / RATIFIED / CLOSED / HALT transitions. |
| `src/factory/holdout.py` | Sealed roots (`data/ladders_holdout`, later `data/ladders_2026-09`); loader refuses without `--unseal RATIFIED-<date>` matching `docs/REVIVAL_2026_09.md`; every unseal appended to `reports/factory/unseal_log.jsonl`; ≤3 finalists per family, ≤3 unseals per quarter. `[graft: PROBE/LANEWORKS]` |
| `src/factory/guards.py` | Worker-process tripwire: `datetime.now` / `time.time` raise when the caller is `src.strategies.*`, `src.factory.genome`, or `src.factory.features`. `[graft: LANEWORKS]` |
| `src/factory/fees.py` | Reads `configs/fees/fee_regime.csv` (time-indexed, seeded from the 2026-09-02 `/series` + `/series/fee_changes?show_historical=true` pulls) and recomputes `fee_per_contract` for every row from its `ts_utc`. `[graft: LANEWORKS]` |
| `src/factory/report.py` | `summary.json` / `summary.md` / `oos_by_date.csv` / `board.md` (timestamp-free) / `latest.json`. |
| `src/factory/coverage.py` | Regenerates `reports/factory/coverage.json` (independent units per lane vs the 40-unit search floor). `[graft: LANEWORKS]` |

### 1.2 Runtime (maia sandbox image; numpy+pandas only)

| Module | Responsibility |
|---|---|
| `src/strategies/genome_strategy.py` | `GenomeStrategy(spec_path, clock, forecast_provider, fee_regime)`: decodes the promoted JSON, builds the same visible row via `features.py` + `probability_engine.bracket_probabilities_point` with the frozen calibration referenced by hash, evaluates `genome.to_mask(row)`, emits one `TradeSignal` per market per day with `limit_price = quote + adverse_fill`, `confidence = p_win`, `contract_side`, `is_maker=False`, tz-aware `expiration_time`; `[] + log_rejection(CODE)` on every skip; refuses to construct if the live `fee_type` differs from the promoted spec or the calibration hash does not match. **No `datetime.now`, no `time.time`, no fitting code.** |
| `src/data/forecast_vintage_provider.py` | Thin runtime wrapper over `src/data/mos_guidance_provider.MOSGuidanceProvider` (:372): `latest_vintage(city, target_date, as_of) -> (init_time_utc, forecast_high_f, lead_hours, fetched_at)`; honours the availability lag (§4.2); records `fetched_at` so the lag becomes empirical. `[graft: Systems review]` |
| `src/bots/weather_bot.py` (small change) | Waterfall generalised to loop over `self.strategies` in declared order (hot-load design (b), facts B §1); `GENOME_STRATEGY_ID` env registers `GenomeStrategy`; `ML_WEATHER_ENABLED` owner flag (default **False** once F0 ships) removes the inadmissible fallback from the slot. No gate touched. |
| `scripts/gate.py` | The never-built FR-5.2 gate: ≥50 settled trades grouped by `target_date`, exact binomial p<0.05 vs fee-adjusted breakeven at actual entry, net PnL>0, spec hash unchanged; reads `data/trade_journal.jsonl` + `closed_trades` — never equity. |
| `scripts/factory_paper_reconcile.py` | Weekly lab-vs-paper: re-prices every sandbox fill with the lab formula, lists the sandbox trade set as a subset of the lab trade set with REJECT codes for the difference. `[graft: Governance]` |

### 1.3 CLI, deploy, Hermes

- `scripts/factory.py` subcommands: `freeze-frame`, `gen0`, `run --config`, `resume <run_id>`, `controls <run_id>`, `report <run_id>`, `holdout --finalists <file> --unseal <tag>`, `score --genome <id> --ladders <root> --as-of` (prints result sha256 BEFORE the numbers), `promote <id>`, `coverage`, `board`. `run` asserts the lane has ≥40 independent units and a registry line exists.
- `deploy/spark/docker-compose.lab.yml`: new `factory` service (§7.1) and `factory-holdout` service (only one that mounts sealed roots).
- `deploy/spark/mp-factory@.service` (systemd --user template) wrapping `docker compose run --rm factory python scripts/factory.py resume %i`, `Restart=on-failure`, `StartLimitBurst=5`. `[graft: FORGE]`
- `hermes_plugin/__init__.py`: `mp_factory_status` and `mp_factory_board` reading `MONEY_PRINTER_FACTORY_DIR` (set explicitly on alcyone to `/home/jushoya/projects/money_printer/reports/factory`; the plugin's `~/money_printer` default at :22-24 does not exist on alcyone).

---

## 2. Data flow

```
ladders (data/ladders, 276 files) + forecast_archive (gfs_mex, gefs) + CLI truth
   │  unchanged ev_analysis chain (walk-forward calibration, embargo 1)
   ▼
frame.py ── parity frame (lag 0, no filters; for the 1e-9 test only)
        ├── search frame (truth filter, cutoff, no-lookahead, availability lag,
        │                 sigma_f <= 4F, sandbox_admissible, visible/hidden split)
        └── gefs twin (same keys; ex-ante disqualifier only)
   │  sha256 of every input + frame -> data/factory/frames/<id>/provenance.json
   ▼
registry.jsonl line written (family, config hash, budget, picker, thresholds)   <- BEFORE results
   ▼
evolve.py per campaign (A, B, C, ALL69) in a fork Pool of 16 workers
   workers see the search window only (validation rows physically removed)
   ledger rows written UNSCORED -> scored -> per-date vectors kept
   ▼
picker (pre-registered) chooses one genome per campaign on search-window boot_lo
   ▼
validation block scored ONCE per pick by the main process -> pooled OOS (33 dates)
   ▼
multiplicity.py: RC/SPA over ledger, Holm across families, clustered DSR
null.py: 20 x snapshot-efficient, 20 x residual-shuffle, 1 x planted-edge (full procedure each)
   ▼
report.py -> reports/factory/<run_id>/summary.{json,md}, board.md, latest.json
   ▼  (human) registry PROPOSED
holdout.py (after RATIFIED): data/ladders_holdout once, <=3 finalists, Holm
factory.py score (after M1 ladders): data/ladders_2026-09 once -> R3 numbers
   ▼  (human) promotion commit: configs/factory/promoted/<id>.json + GENOME_STRATEGY_ID
maia: git pull --ff-only && compose up -d --build
GenomeStrategy in WeatherBot -> every existing gate -> SimulatedExchange (fills at limit = quote+1c)
reconcile_weather.py daily; factory_paper_reconcile.py weekly; gate.py after >=50 target_dates
```

---

## 3. The genome (GENE_SPEC v1)

A candidate IS a fixed-length integer vector decoded into a conjunction of column
predicates over the **visible** frame plus a frozen entry policy. Every gene has a finite,
pre-registered domain; the search space is enumerable and every genome is a legal sandbox
rule. City subset, forecast source, sizing, and entries-per-market are deliberately NOT
genes (the first two were the documented HALT failure modes; R1 freezes gfs_mex; R3
requires one entry per market).

| # | gene | domain | predicate on the visible row |
|---|---|---|---|
| 1 | `direction` | {buy_yes, buy_no} | `direction == g` |
| 2 | `mode` | {taker} in v1 (maker gated to F5 under recorded-traversal evidence) | `mode == g` |
| 3 | `windows` | non-empty 6-bit subset of {≥24h, 12-24h, 6-12h, 3-6h, 1-3h, <1h} | `window_code ∈ set` (from `minutes_to_close` via `time_window_label`) |
| 4 | `bands` | non-empty 6-bit subset of BAND_EDGES bins {[0,1),[1,2),[2,3),[3,4),[4,5),≥5} | `band_code ∈ set` — the frame's `band`, which is binned on `distance_f = |midpoint_f − mu_f|` (ev_analysis.py:963-964) |
| 5 | `p_win_lo` | {OFF, 0.50, 0.55, …, 0.95} | `p_win ≥ v` |
| 6 | `p_win_hi` | {OFF, 0.60, …, 1.00} | `p_win ≤ v` |
| 7 | `far_margin` | {OFF, 0.00, 0.02, …, 0.20} | NO: `yes_ask − p_yes ≥ v` (requires `yes_ask < 1.0`); YES: `p_yes − yes_bid ≥ v` (requires `yes_bid > 0`). This is `fr31a_mask`'s `p_yes <= yes_ask − margin` exactly (ev_analysis.py:1220-1226). **[fix: Statistician flaw 2]** |
| 8 | `quote_lo` | {OFF, 0.02, 0.05, …, 0.50} | `quote ≥ v` |
| 9 | `quote_hi` | {OFF, 0.10, …, 0.98} | `quote ≤ v` |
| 10 | `sigma_cap` | {2.0, 2.5, 3.0, 3.5, 4.0} in search; OFF legal only for encoding/parity | `sigma_f ≤ v` — the search frame already has σ≤4F applied pre-selection (R3 #1), so this gene can only tighten. **[fix: Governance flaw 4]** |
| 11 | `lead_buckets` | non-empty 3-bit subset {short, medium, long} | `lead_bucket_code ∈ set` |
| 12 | `edge_distance_lo` | {OFF, 1, 2, 3, 4, 5, 6} | `edge_distance_f ≥ v` (fr31a uses 4.0) |
| 13 | `entries_per_market` | {1} frozen (schema slot for a future ratification) | first masked executable snapshot per market |

Search space ≈ 2 × 63 × 63 × 11 × 10 × 12 × 12 × 15 × 5 × 7 × 7 ≈ 4 × 10⁹ syntactic
genomes; the multiplicity unit is the **phenotype** (sha1 of the sorted set of
`market_code` the genome trades on the search window), which is far smaller.

Generation-0 seeds (pre-registered, encoded exactly; verified encodable):
`fr31a_taker` = {NO, taker, windows {≥24h,12-24h}, far_margin 0.08, edge_distance_lo 4,
all else OFF/full}; `fr31b` = {YES, taker, windows {6-12h,3-6h,1-3h,<1h}, p_win_lo 0.95};
`nofilter_no` = {NO, taker, all windows, all bands, everything OFF} (the HALT baseline,
+2.09c/664 trades); `salvage_5f` = {NO, bands {≥5}, maker — scored as a **diagnostic row
only**, mode gene forced to taker for any search}; `mlweather_fallback` = the shape the
sandbox trades today (buy NO on the highest-YES-bid bracket when the forecast is ≥1.2F
outside a 1F bracket) — scored settlement-true so the owner sees, for the first time, a
number for what maia is paper-trading `[graft: FORGE]`.

Operators: per-gene mutation rate 1/L (subset genes flip one bit, never to empty; ordinal
genes step ±1 quantum with p=0.1 jump to/from OFF; direction flips p=0.02); uniform
crossover p=0.5 per gene between two tournament winners; children 50% crossover+mutation,
50% mutation-only clone; legality repair (non-empty subsets, `p_win_lo ≤ p_win_hi`,
`quote_lo ≤ quote_hi`, `sigma_cap ≠ OFF`) by resampling the offending gene.

GENE_SPEC v2 (F5, `[graft: FORGE lagged features]`): threshold genes over ex-ante
per-market lags computed by `features.py` with a no-forward-reference unit test —
`bid_move_1h ∈ {OFF, ≤−0.05, ≤−0.02, ≥+0.02, ≥+0.05}`, `oi_lo ∈ {OFF, 50, 200, 1000}`,
`hours_quoted_lo ∈ {OFF, 1, 3, 6, 12}`, `exec_edge ∈ {OFF, 0.00, 0.02, …, 0.10}`
(`p_win − quote ≥ v`; a predicate, never fitness). v1 genomes decode under v2 with the new
genes OFF; the v1 parity test must still pass.

---

## 4. Data pipeline and frame

### 4.1 Substrate and coverage matrix

| lane | substrate | truth | independent unit | joinable units today | status |
|---|---|---|---|---|---|
| weather (KXHIGH NY/CHI/LAX/MIA) | `data/ladders/<SERIES>/<date>.csv`, hourly candles, 62,932 rows, 1,656 markets, 276 city-days, 69 dates 2026-05-18..07-25, tz-aware, result+expiration_value+cli_high+truth_agrees pre-joined | Kalshi `result` (1,655/1,656 payoff-matched, 0 truth disagreements, 25 `truth_agrees=None` on 07-25) | `target_date` | 69 dates (development) | **READY** |
| weather holdout-B | `data/ladders_holdout/` 2026-07-26..08-31 (+148 city-days, ~37 dates) — **must be backfilled before ~2026-10-03 retention expiry**; forecasts via `backfill_forecasts.py` (merge-safe), gefs via `backfill_ensemble_history.py --out` (it overwrites — copy aside); truth via `backfill_weather_truth.py --min-days 60`; re-reconcile ≥08-14 (settlement source NWS→The Weather Company) | Kalshi `result`; `truth_agrees` empirical after 08-14 | `target_date` | ~37 dates | **SEALED** (never searched) |
| weather Sept–Oct (R3 reserve) | `data/ladders_2026-09/` from the M0 capture timer (kill 2026-09-15) | — | `target_date` | 0 today | **RESERVED** until RATIFIED |
| gas (KXAAAGASM/W) | `reports/phase4/gas_quote_tape.csv` 39,623 rows / 405 markets / 14 events | AAA CSV to 07-29; Kalshi settled record 81 dates | `settlement_date` | 14 | NOT_PROMOTABLE (<40) |
| mention (KXTRUMPMENTION) | maia tape since 09-01 only (post-cutoff = "looking") | 1,306 finalized markets / 42 events, no quotes attached | `event_ticker` | 0 | NOT_READY |
| tweets | 233 tape rows | — | event | 0 | NOT_READY |
| crypto_annual | one settlement 2027-01-01 | none | event | 0 | NEVER a lane |

The 14-s harvest CSV tape is NOT a substrate (settleable columns only from 07-27, late-day
gap from the naive-clock ladder filter at weather_bot.py:427, no day-of forecast column,
blank Bid/Ask → fake zero-spread book). It is used only for the F3 fill-realism study.

### 4.2 Frame hardening (applied by `frame.py` on top of the unchanged evaluator output)

1. **Data cutoff assert**: `max(target_date) ≤ FACTORY_DATA_CUTOFF` (2026-07-25 until the
   registry carries `RATIFIED <date>`); abort with a logged reason otherwise. The sealed
   roots are not mounted into the `factory` service at all (§7.1).
2. **Truth filter**: keep rows with `result ∈ {yes, no}` AND `payoff_matches_kalshi != False`
   AND `truth_agrees != False` (None allowed — the 25 markets of 2026-07-25 are kept, so the
   frame has 69 dates and the 181-trade parity target is reachable). Counts of dropped
   rows/markets per reason are written to provenance. `settles_yes` scoring a missing
   result as NO (ev_analysis.py:1027) can therefore never contribute a trade.
   **[fix: Statistician flaw 3]**
3. **No-lookahead assert**: every row has `init_time_utc + availability_lag ≤ ts_utc`.
   The Phase-2 join uses `init_ts ≤ ts_utc` (lag 0); MOS MEX bulletins issue ~3.5-4 h after
   init, so a 12Z vintage is not really available until ~16Z — a 1-4 h lookahead on rows
   inside the 12-24h window. `parity` frame: lag 0 (matches Phase 2 to the digit).
   `search` frame: lag = `FORECAST_AVAILABILITY_LAG_MIN` (default 240; made empirical from
   `fetched_at` once the runtime provider has recorded a month). **[new — no proposal
   caught this]**
4. **Pre-selection σ filter**: `search` frame keeps `sigma_f ≤ 4.0` (R3 #1).
5. **Sandbox admissibility**: `sandbox_admissible = trade_is_profitable(p_win, price_paid,
   contracts=1, is_maker=False)` — the maia EV gate (mixins.py:291-318: two taker legs at
   C=1) evaluated at the limit price `GenomeStrategy` will emit. Folded into the `search`
   frame's `executable`. The factory therefore searches only trades the sandbox will
   execute; the parity frame keeps the evaluator's own `executable`. **[new; resolves
   Governance's "parity is emit-level only"]**
6. **Fee regime as data**: `fee_per_contract` recomputed from `configs/fees/fee_regime.csv`
   at each row's `ts_utc` (KXHIGH* quadratic ×1, maker $0 — identical to the evaluator
   today, so parity holds; a future change is an input diff).
7. **Visible / hidden split** `[graft: PROBE, FORGE]`:
   - visible (genome-facing, float32/int16): `city_code, target_date_code, market_code,
     ts_utc, minutes_to_close, window_code, direction_code, mode_code, band_code,
     lead_bucket_code, lead_hours, p_yes, p_win, mu_f, sigma_f, midpoint_f, distance_f,
     edge_distance_f, yes_bid, yes_ask, no_bid, no_ask, last, price_mean, volume,
     open_interest, quote, price_paid, fee_per_contract, executable, sandbox_admissible,
     floor_strike, cap_strike, strike_type_code` (+ v2 lags).
   - hidden (scorer-only): `won, realized_per_contract, result, settles_yes,
     expiration_value, cli_high, truth_agrees, payoff_matches_kalshi, maker_yes_fill,
     maker_no_fill, fwd_min_ask, fwd_max_bid, yes_bid_low, yes_ask_high, ev_per_contract`.
     (`yes_bid_low/yes_ask_high` are intra-candle extremes = within-candle future;
     `ev_per_contract` is carried for diagnostics only and is never an input to fitness.)
   - A unit test asserts `to_mask` cannot reference a hidden column; a truth-perturbation
     test asserts that altering `won` leaves every trade set unchanged.
8. Rows sorted by `(market_code, ts_utc)`; per-market block boundaries precomputed; the
   `gefs` twin frame is keyed on `(market_code, ts_utc)` for the ex-ante disqualifier.
9. Provenance: sha256 of every ladder CSV, forecast archive, CLI truth file, calibration
   directory, fee regime, `EVConfig`, git rev (**abort if empty** — the root-container
   trap), `deploy/spark/requirements-lab.lock` hash, and of the frame itself. Path
   separators normalised before hashing.

Frame sizes: parity 251,728 × 63 (172 MB pandas, built in ~1.5 s cold); search slim
≈ 150k rows × ~34 visible + 15 hidden columns ≈ 30 MB numpy.

---

## 5. Fitness (recomputable by a red team from `frame` + genome JSON alone)

Let F be the search frame (§4.2). For genome g and date set D:

1. `M = to_mask(g, F) & F.executable & (F.target_date_code ∈ D)`.
2. Trades T = the FIRST masked row of each `market_code` in `ts_utc` order
   (`np.maximum.accumulate` within sorted market blocks; reproduces
   `groupby('market_ticker').head(1)` at ev_analysis.py:1299-1303).
3. Per trade: `price_paid = quote + 0.01` (taker YES: `quote = yes_ask`; taker NO:
   `quote = 1 − yes_bid`; NaN and not executable if `> 0.99`); `fee = ceil_cents(mult ×
   0.07 × 20 × P(1−P)) / 20` from the fee regime at `ts_utc`; `won = (result == 'yes') ==
   (direction == buy_yes)`; `realized = won − price_paid − fee`. One fee leg; settlement
   free; held to settlement (matches the sandbox's held-to-settlement rule for KXHIGH*).
4. Cluster: `d_k = mean(realized)` over trades with `target_date = k`; `n` = dates with ≥1
   trade. (Four cities under one synoptic pattern are one draw.)
5. Statistics: `mean = mean(d_k)`; `se = std(d_k, ddof=1)/√n`; `t = mean/se`;
   bootstrap = 4000 resamples of the n dates with replacement using
   `np.random.default_rng(20260726).integers(0, n, size=(4000, n))` — the identical call
   `evaluate_shape` makes, so the draws are bit-identical for a given n; `boot_lo/hi` =
   2.5/97.5 `np.percentile`; `losing_dates`, `worst_date_pnl`, `win_rate`, `cities`.
6. **Hard constraints** (violation → fitness = −inf, reason code logged in the ledger):
   `n ≥ 0.6 × |D|` dates traded; `trades ≥ 40`; `cities ≥ 3`; `worst_date_pnl ≥ −0.50`;
   gefs-twin realized mean ≥ 0 on the same trade keys (R3 #2 ex-ante disqualifier);
   `n_active_clauses ≤ 8`; `BSS_trades ≥ −0.05` where `BSS_trades = 1 − Brier(p_win)/
   Brier(p_mkt)` on the genome's trades with two-sided quotes, `p_mkt = (yes_bid +
   yes_ask)/2` mapped to the traded side `[graft: PROBE]`.
7. **In-sample selection fitness**: `fit(g, D) = boot_lo(g, D)`. The lower bound is the
   built-in penalty for thin, fat-tailed trade sets. Ties → fewer active clauses.
8. **Promotion-time gates** (admit, never rank; all on the pooled validation dates and again
   on each sealed holdout): paired-by-date bootstrap of `(g_k − B_k)` vs the no-filter
   baseline B on the dates g traded, lower bound > 0 (HANDOFF rule 2); sign survives
   `price_paid = quote + 0.02` and `+ 0.03`; sign survives `embargo_days = 2` (frame
   rebuilt); `BSS_trades ≥ 0` (R3 Brier-beats-market); point estimate ≥ 4c/contract (R3 #5);
   tail ratios within [0.8, 1.25] at |z| ≥ 2.5 (R3 #4); `cities ≥ 3`.

Modelled EV (`ev_per_contract`, `positive_ev_shapes`) is carried for diagnostics only and
is never an input to `fit()`, a filter, or a tiebreak.

Frame-level diagnostic printed at gen-0 with a date-clustered CI `[graft: PROBE]`: BSS of
the walk-forward calibration vs the market mid on ALL two-sided rows per city-day. Expected
negative; it is a lane-level fact about the calibration, not a genome property, and it is
what R3's "Brier beats market" is measured against.

---

## 6. Folds, multiplicity, controls

### 6.1 Headline: anchored walk-forward campaigns (2-day embargo) `[graft: PROBE; fixes Statistician/Systems "blocked CV ≠ walk-forward"]`

69 development dates 2026-05-18..07-25 (declared **development data**: Phase 2's 4×2×6×6
grid, 24-cell sweep and two sources already swept all of them; no fold inside is virgin for
the seed shapes).

| campaign | search (in-sample) | embargo | validation (never seen by the search) |
|---|---|---|---|
| A | 05-18..06-16 (30 dates) | 06-17, 06-18 | 06-19..06-30 (12) |
| B | 05-18..06-30 (44) | 07-01, 07-02 | 07-03..07-14 (12) |
| C | 05-18..07-14 (58) | 07-15, 07-16 | 07-17..07-25 (9) |
| ALL69 | 05-18..07-25 (69) | — | none (the deployment genome; its OOS is the sealed holdouts) |

Workers for a campaign receive a frame with the embargo and validation rows **physically
removed**. Each campaign runs to completion; the **pre-registered picker** selects exactly
one genome (highest search-window `boot_lo` among constraint-satisfying elites; ties →
fewer clauses). That genome is scored ONCE on the validation block by the main process,
after the campaign's final checkpoint is written. **Pooled OOS** = the 33 validation-date
PnLs concatenated (each date scored by a genome that never saw it), summarised with the
§5 statistics. This is the headline number for the *procedure*. The ALL69 pick is the
promotion candidate; its phenotype Jaccard to the A/B/C picks is reported as stability
evidence, and its first true OOS is holdout-B (once, after RATIFIED), then the Sept–Oct
R3 reserve.

### 6.2 Diagnostic: blocked 5-fold with 2-day purge (never headline)

Five contiguous blocks of 13-14 dates; each held block plus a 2-day purge on both sides is
removed from its workers; per-fold pick scored once on its block; pooled 69-date series
reported *alongside* with the label "in-sample blocks postdate the held block". A 3-day
purge sensitivity is reported.

### 6.3 Multiplicity `[graft: FORGE RC/SPA, LANEWORKS ledger, PROBE clustered DSR; fixes the unreachable p < 0.05/K_families]`

- **Ledger**: every distinct phenotype evaluated in a campaign keeps its per-date PnL
  vector on the search window (write-then-evaluate; a crash leaves `UNSCORED`, never a
  missing row). Kills and duplicates' first copies are in the ledger — they were tests.
- **White Reality Check / Hansen SPA** on the search window: for B=4000 date resamples,
  the centered max over all L phenotypes of `(mean*_l − mean_l)/se_l`; `p_RC(pick)` = share
  of resamples whose max ≥ the pick's observed `t`. SPA reported with the studentised
  poor-model recentering. Computed per campaign and for ALL69. A continuous p with no
  `1/(K+1)` floor.
- **Family-wise control**: every run is a registered family member (lane / source / mode /
  gene-spec version / config hash). At promotion, **Holm at α = 0.05 across all registry
  entries** on the pooled-validation one-sided p (from the date-bootstrap) and, separately,
  on the holdout p. Cap: 6 registered families before 2027. A "rerun with a better seed" is
  a new family line.
- **Clustered Deflated Sharpe**: full Bailey–López de Prado on the validation DATE series
  with `N_trials` = distinct phenotypes in the ledger, skew/kurtosis from the date series;
  reported, not gated (the crude `validation_harness.deflated_sharpe` is not used).
- **Promotion requires**: pooled-validation `boot_lo > 0`; Holm-adjusted p < 0.05;
  `p_RC(ALL69) < 0.10`; the pick beats every control-run pooled validation (§6.4); all §5.8
  gates. A family whose pooled `boot_lo ≤ 0` or Holm p ≥ 0.05 is CLOSED in the registry —
  a valid, expected outcome, never retried with a new seed inside the same family.

### 6.4 Control runs (each is the FULL procedure: campaigns A/B/C, same population, same
generations, same seeds) — required artifacts per frame version

Why the market-implied null of the base proposal was dropped: a settlement-true frame has
one outcome per city-day, so a "market-implied" outcome must be drawn from the market's
distribution *at one snapshot*; entries earlier than that snapshot then have positive null
edge and later entries negative, whatever snapshot is chosen. It is not a no-edge null for
every entry window. It is replaced by:

1. **Snapshot-efficient null (K=20)** — hidden `won` redrawn per ROW as
   `Bernoulli(p_mkt_row)` where `p_mkt_row` is the market mid at that row mapped to the
   traded side (rows without two-sided quotes are dropped). Every price equals its win
   probability minus spread and fee, so no rule has edge; the loss of within-market
   outcome correlation makes this null *easier* for the search than reality, i.e. it is a
   stress test: if the machine reports pooled `boot_lo > 0` in more than 1 of 20 such runs
   it over-claims. The real run's pooled validation is reported with its rank among the
   20 (calibration, not the gate).
2. **Residual-shuffle null (K=20)** — per city, the residual `cli_high − mu_f` of the last
   pre-window vintage is circularly shifted across dates; `result` per bracket is recomputed
   with `bracket_payoff.settles_yes` from the shifted high; prices, forecasts and market
   structure untouched. Structural edge survives this null; date-specific luck does not.
   A real pick whose pooled validation does not exceed the null's 95th percentile is
   "date-luck-consistent" and cannot be promoted.
3. **Planted-edge positive control (K=1)** — a known 3-clause rule is given a +5c/contract
   edge by editing hidden `won` on its rows; the procedure must recover ≥80% of the planted
   edge in pooled validation. Proves the machine finds what is there.

---

## 7. Compute plan on the GB10

### 7.1 Process model and coexistence with mp-vllm

- One `docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory python
  scripts/factory.py run --config configs/factory/weather_gfs_mex_taker_v1.yaml` per
  registered run, launched by `systemctl --user start mp-factory@<run_id>` (one-shot unit,
  `Restart=on-failure`, journald logs). No daemon, no listener, no port (F1 respected).
- `factory` service: same image `money-printer-lab:latest`; `user: "${UID}:${GID}"`;
  `environment: HOME=/tmp, TZ=UTC, PYTHONPATH=/app, PYTHONDONTWRITEBYTECODE=1`;
  `tmpfs: /app/logs:mode=1777` (logger opens `logs/` at import); `network_mode: none`
  (the factory never calls any API; ladders are pre-joined; no Kalshi creds exist in the
  container); **no `gpus` stanza**; `cpuset: "0-3,5-9,10-11,15-19"` = 16 cores (all 10
  fast X925 + 6 A725), leaving `{4, 12, 13, 14}` for mp-vllm (3.25% CPU) and the two 1-CPU
  Hermes containers; `cpu_shares: 512` and the command wrapped in `nice -n 10`;
  `mem_limit: 24g`. Volumes: checkout `:ro` at `/app`, with `data/factory` and
  `reports/factory` bind-mounted `rw` on top; `/archive` not mounted; **sealed roots not
  mounted**. Consistent 16 everywhere. **[fix: Governance "14 vs 16 cores"]**
- `factory-holdout` service: identical plus `data/ladders_holdout:ro` and
  `data/ladders_2026-09:ro`; only `factory.py holdout` and `factory.py score` run there.
- `lab` service (network on) runs the backfills; the factory never does.
- Workers: main process builds the frames (~2 s, RSS ≈ 0.5 GB), writes them, then forks a
  `multiprocessing.Pool(16)` that inherits the slim arrays copy-on-write; each worker calls
  `os.sched_setaffinity` to its cpu; fast cores get the bootstrap-heavy final scoring;
  `imap_unordered(chunksize=64)` absorbs the 4.4 vs 8.6 ms/cand core imbalance.
- The factory never calls the vLLM endpoint. GPU: **no** — the kernel is a boolean mask
  over ~150k rows plus a bincount and a 4000×n bootstrap, memory-bandwidth-bound at pool
  scale (measured 0.37 ms/cand/core, 7,231 masks/s at pool20); the binding constraint is
  69 dates, not FLOPs. An LLM-proposed-mutation experiment (FORGE) is explicitly deferred:
  a second vLLM time-shares 48 SMs with Hermes and violates "must not be disturbed".

### 7.2 Throughput and memory (from measured figures)

- Full fitness per genome (mask + first-in-market + bincount + 4000-draw bootstrap +
  constraints + gefs twin + BSS) ≈ 1.2-1.8 ms/core → ≥ 3,000 genome-evaluations/s on 16
  workers is the conservative exit criterion (theoretical ≈ 9-13k/s).
- Real cycle: 4 campaigns (A, B, C, ALL69) + 5 blocked folds = 9 runs × μ400 × ≤60 gens
  = 216k evaluations ≈ 1-2 min. Controls: 41 procedure replicates × 3 campaigns × 24k =
  ~3M evaluations ≈ 20-60 min. **Full cycle < 4 h wall** including report generation.
- Memory: search frame 30 MB + 16 workers × ~150 MB + parquet checkpoints < 4 GB; control
  frames rebuilt one at a time (~0.2 s each). Peak < 8 GB against the ~80 GiB budget;
  mp-vllm's 22 GB pinned footprint and unified-memory pressure are untouched
  (`MemAvailable` ~94 GiB measured).
- CPU contention with an active Hermes generation is unmeasured: F2 records throughput
  with vLLM idle and during a scripted Hermes turn, and records mp-vllm p50 token latency
  for a fixed prompt with the factory idle vs running (acceptance: ≤10% change).

### 7.3 Reproducibility, resumability, supervision

- `run.json`: config hash, frame sha256, `requirements-lab.lock` hash, git rev (non-empty
  or abort), master seed, fee regime hash. A run refuses to resume on a different frame
  hash or lock hash.
- Generation RNG seeded by `hash(master_seed, campaign, gen)`; every generation writes
  `gen_<N>.parquet` atomically (tmp + rename) plus `status.json`; resume from the last
  complete generation is bit-identical to an uninterrupted run (F2 exit criterion).
- `status.json` is timestamp-free so the existing Hermes `--no-agent --monitor-script`
  byte-hash pattern posts only on change.
- Determinism and row-permutation checks on `to_mask` `[graft: FORGE]`: the same genome
  on a row-permuted frame yields the permuted mask (proves row-wise purity).
- Wall-clock tripwire (`guards.py`) installed in every worker.

---

## 8. Storage layout

```
configs/factory/<family>.yaml                 tracked   run configs (budget, picker, thresholds)
configs/factory/promoted/<genome_id>.json     tracked   the ONLY factory artifact that reaches maia
                                                        (under configs/, not data/, because .dockerignore
                                                         excludes data/ from the sandbox image)
configs/fees/fee_regime.csv                   tracked   time-indexed fee regime (2026-09-02 /series pull)
data/factory/frames/<lane>_<as_of>_<sha12>/   ignored   visible.npy hidden.npy columns.json provenance.json frame.sha256
data/factory/runs/<run_id>/                   ignored   run.json inputs.json folds.json status.json
    ledger/<campaign>/gen_NNN.parquet                   genomes, phenotype hash, fitness, reason codes, per-date vectors
    picks.json                                          the pre-registered picker's choice per campaign
    controls/{snapshot,residual,planted}/<k>/...        full-procedure replicates
data/ladders_holdout/                         tracked   sealed 07-26..08-31 ladders + SHA manifest
data/ladders_2026-09/                         tracked   M0 capture root (R3 reserve)
reports/factory/registry.jsonl                tracked   append-only family registry (written pre-run)
reports/factory/unseal_log.jsonl              tracked   every holdout look
reports/factory/coverage.json                 tracked   lane readiness (weekly)
reports/factory/<run_id>/{summary.json,summary.md,oos_by_date.csv,finalists.json,board.md}  tracked
reports/factory/latest.json                   tracked   pointer for Hermes
.gitignore additions: data/factory/  reports/factory/*/gen_*  reports/factory/*/controls/
```
Never `git add -A` (data caches under `data/` are not ignored).

---

## 9. Promotion path to maia and the gate

1. **Factory closes a family** with the §6.3 promotion conditions met → human writes
   `PROPOSED <genome_id>` into `registry.jsonl`. Nothing automatic.
2. **Holdout-B (once)**: after `RATIFIED <date>` and once `data/ladders_holdout` is
   complete and re-reconciled, `factory.py holdout --finalists finalists.json --unseal
   RATIFIED-<date>` scores ≤3 finalists on 07-26..08-31 with Holm; appended to
   `unseal_log.jsonl`. Failure = the genome is HALTed in the registry; no re-tune.
3. **R3 (once, at M1)**: `factory.py score --genome <id> --ladders data/ladders_2026-09`
   prints the result sha256 before the numbers and appends the R3 checks (§5.8) to the
   registry. Any failure = HALT #3 for that genome. (The REVIVAL M1 `go_no_go.py
   UNMODIFIED` run for fr31a happens on the same root; the two are independent
   pre-registered tests.)
4. **Promotion commit**: `configs/factory/promoted/<id>.json` + `GENOME_STRATEGY_ID=<id>`
   in `/srv/money_printer/.env`; maia `git pull --ff-only && docker compose up -d --build`
   (deploy/README.md path; no hot-load mechanism invented).
5. **Sandbox execution** — `GenomeStrategy` REPLACES the ML Weather fallback in the
   per-city slot (`ML_WEATHER_ENABLED=False`; appended strategies are starved by
   `WEATHER_SLOT_FULL`, mixins.py:320-361, and the immortal `confidence=1.000` positions)
   **[fix: Governance flaw 1]**; V2 stays as the last fallback. Decision cadence: the
   strategy evaluates a city's ladder at the top of each UTC hour (the candle-close grid the
   frame was scored on) using the latest vintage whose `init + lag ≤ clock`. Signals carry
   `limit_price = quote + adverse_fill` so the sim (which fills at limit, p=1) books the
   lab's pessimistic price natively — no engine change; `confidence = p_win`; `is_maker=False`
   so the sim books taker fees; tz-aware `expiration_time`. Every existing gate applies
   unchanged (slot, EV gate, Kelly, daily cap, cooldowns, drawdown, allocation,
   MAX_CONTRACTS=50).
6. **Fills** — `realistic_fills=True` is a penny-floor coin flip in [0.01, 0.05]
   (matching_engine.py:437-448, 814-827), **not** a fill model, and is not claimed as
   FR-5.2's "realistic fills". The factory's claim is: fills at `quote + 1c` (more
   pessimistic than a real limit order, which would price-improve to the ask) plus the F3
   fill-realism study on the 14-s maia tape, raising `adverse_fill` until it covers the 90th
   percentile of intra-cadence bid/ask drift. Whether that satisfies FR-5.2 is owner
   decision #4 and is flagged as such. **[fix: Governance flaw 2]**
7. **Paper record**: ≥30 days; `reconcile_weather.py` daily (13:30Z timer);
   `factory_paper_reconcile.py` weekly re-prices every sandbox fill with the lab formula
   (quote+1c, C=20 one leg, held to settlement) and reports the sandbox trade set as a
   subset of the lab trade set with REJECT codes (KELLY_ZERO, cooldowns, allocation) for the
   difference; PnL taken from `closed_trades`/journal, never equity (UTC-midnight reset
   double-subtracts). Kelly sizing differs from the 20-contract frame assumption (CONTRA
   10); the reconciliation reports per-contract numbers at actual qty.
8. **Gate**: `scripts/gate.py` — ≥50 settled trades grouped by `target_date` (~7+ weeks),
   exact binomial p < 0.05 vs fee-adjusted breakeven at actual entry, net PnL > 0, spec hash
   unchanged; `gate_registration.json` (thresholds, grouping, hash) committed BEFORE the
   first paper trade. Only then does the owner's M3 capital decision arise. The factory has
   no path to capital and no Kalshi credentials.

---

## 10. Hermes / Discord reporting

- `mp_factory_status`: reads `reports/factory/latest.json` → run id, campaign, generation,
  budget used, elite in-sample `boot_lo`, pooled OOS CI, `p_RC`, phenotypes evaluated,
  registry status, control-run status.
- `mp_factory_board`: renders `board.md` — one row per lane
  `[lane | READY/NOT_PROMOTABLE(n)/NOT_READY | family | pick | pooled OOS lo..hi | dates |
  trades | p_RC | Holm p | vs no-filter | vs fr31a | N_phenotypes | controls | coverage
  units / next-data ETA]` plus the PAPER row (settled target_dates, sandbox c/contract from
  closed_trades vs the factory prediction). `[graft: LANEWORKS]`
- Cron `mp-factory-board` (60 min, `--no-agent --monitor-script`, byte-hash of `board.md`)
  → discord:1491982736989093961; `hermes send` on run end, family close, and every unseal.
  Any agent-driven cron pins `--provider custom --model ykarout/Qwen3.5-9B-NVFP4` (the
  mp-status drift failure is the precedent; fixing that cron is an F0 side item).
- `MONEY_PRINTER_FACTORY_DIR` set explicitly in the Hermes environment on alcyone.

---

## 11. Anti-self-deception — how this factory could lie, and the prevention

| lie | prevention (and where it is tested) |
|---|---|
| Selecting on the block it is judged on | Validation/embargo rows physically removed from worker frames; the pick is made and checkpointed before the validation block is scored; picker pre-registered in the config hash. Blocked-CV result never headlined. |
| Reading truth or within-candle futures | visible/hidden split; `to_mask` cannot name a hidden column (test); truth-perturbation test; row-permutation test. |
| Forecast lookahead | vintage rule `init + availability_lag ≤ ts` with per-row assert; the parity frame documents the Phase-2 lag-0 convention explicitly. |
| In-sample calibration | `WalkForwardCalibrator` inside `build_probability_table`; the committed `data/calibration/*_v1.json` are asserted NOT loaded. |
| Scoring an unsettled market as NO | truth filter on `result ∈ {yes,no}`; dropped counts in provenance; the run aborts if `payoff_matches_kalshi == False` appears. |
| Optimistic fills | taker headline, `quote + 1c`, 2c/3c sensitivity at promotion; maker is a diagnostic column with the traversal proxy labelled "lower bound on traversal, upper bound on durable fills"; sandbox fills at `quote + 1c`. |
| Counting genomes instead of tries | phenotype hash is the multiplicity unit; every evaluated phenotype (including kills) is in the ledger and in the RC null max. |
| Best-of-many-runs | registry line written before results; Holm across all entries; new seed = new family; cap 6 families before 2027. |
| Undercounting the search that already happened | all 69 dates declared development data; first true OOS is sealed holdout-B, then Sept–Oct; both opened once and logged. |
| Machine that finds edge in noise | snapshot-efficient null (≤1/20 false pooled `boot_lo > 0`), residual-shuffle null (date-luck), planted-edge positive control (≥80% recovery) — all required artifacts per frame version. |
| Peeking at post-cutoff data ("looking") | cutoff assert; sealed roots not mounted into the `factory` service; loader whitelist test refuses any other root; `factory.py score` prints the result hash before the numbers. |
| Silent input drift | sha256 of every input, the frame, the lock file, the fee regime, and the git rev in `run.json`; resume refuses on mismatch; the runtime `GenomeStrategy` refuses to construct on a fee-type or calibration-hash mismatch. |
| Wall-clock reads | `guards.py` tripwire in workers; `grep` test on `genome_strategy.py`; clock injected by the bot. |
| Modelled-EV trap | `ev_per_contract` is hidden; `fit()` is realized settlement PnL only; the EV gate exists only as `sandbox_admissible` (a stricter admission filter, never a score). |
| Lab ≠ sandbox | `features.py` shared; replay parity test (0 discrepancies over 1,656 markets); hourly decision cadence; `sandbox_admissible` in the search frame; weekly lab-vs-paper reconciliation with REJECT-coded differences; runtime forecast provider is the same product (`gfs_mex` via IEM MOS) at the same vintage rule. |
| Presenting an unsearchable lane as evidence | `factory.py run` asserts ≥40 independent units; the board prints NOT_PROMOTABLE(n) for gas (14) and NOT_READY for mention/tweets/crypto_annual. |
| Root container blanking provenance | non-root service; run aborts on empty git rev. |

---

## 12. Judge-named fatal flaws — resolution ledger

| flaw (judge) | resolution |
|---|---|
| RC gate `p < 0.05/K_families` unreachable with 20/50 nulls (Stat, Sys, Gov) | Replaced: RC/SPA over the ledger gives a continuous p; Holm across families on the validation/holdout p; null runs report a rank, never a Bonferroni threshold. |
| Gene 7 cannot encode fr31a (Stat) | `far_margin` = `yes_ask − p_yes ≥ v` for NO (symmetric `p_yes − yes_bid` for YES); fr31a and fr31b verified encodable; band gene binned on `distance_f` as in the frame, `edge_distance_lo` separate. |
| Truth filter drops the 25 `truth_agrees=None` markets (Stat) | `!= False` semantics; 69 dates retained; parity target reachable. |
| Blocked CV is not walk-forward (Stat, Sys) | Anchored campaigns with 2-day embargo are the headline; blocked CV demoted to a labelled diagnostic with a 2-day purge. |
| Live gfs_mex vintage missing on maia (Sys) | `forecast_vintage_provider.py` over the existing `MOSGuidanceProvider`; genes restricted to gfs_mex-computable features; gefs offline-only. |
| Appended strategy starved by `WEATHER_SLOT_FULL` (Gov) | Replace, not append: `ML_WEATHER_ENABLED=False`, WeatherBot loops over `self.strategies`, `GenomeStrategy` takes the slot; immortal maia positions backfilled with `expiration_time`. |
| `realistic_fills=True` claimed as a fill model (Gov) | Not claimed; `limit = quote + 1c` pessimistic fills in the sim + fill-realism study; owner decision #4 flagged. |
| σ≤4F only kill-listed at promotion, R3 wants pre-selection (Gov) | Applied to the search frame; gene domain ≤ 4.0. |
| 14 vs 16 cores inconsistency (Gov) | 16 everywhere: cpuset `0-3,5-9,10-11,15-19`, four cores reserved. |
| 1-day purge thin against synoptic regimes (Stat) | 2-day embargo/purge default, 3-day sensitivity reported. **Accepted residual**: contiguous blocks are still weather regimes; the sealed holdouts are the answer, not more purging. |
| Expressivity ceiling — rediscovers fr31a variants (Stat, MaskForge itself) | **Accepted for v1** (the un-memorisable genome is the point on 69 dates); GENE_SPEC v2 adds ex-ante lagged quote features as bounded threshold genes in F5. |
| Statistics, not compute, is binding; "nothing beats the no-filter baseline" is the likely outcome (all) | **Accepted and designed for**: a CLOSED family is a deliverable; the report is published either way. |

---

## 13. Existing modules — reused vs quarantined

**Reused unchanged (imported, never copied):** `src/backtest/ev_analysis.py` (frame chain,
`evaluate_shape` as the parity reference, `fr31a_mask`/`fr31b_mask`, `EVConfig`, `GFS_MEX`
/`GEFS`, `time_window_label`, `band_label`, `add_maker_fill_flags`), `src/data/kalshi_history.py`
(`load_ladders`, `LADDER_COLUMNS`, `backfill`), `src/calibration/forecast_calibration.py`
(`WalkForwardCalibrator`, `content_fingerprint`), `src/calibration/probability_engine.py`
(`bracket_probabilities_point`, `integer_pmf`), `src/core/fee_calculator.py`
(`taker_fee`/`maker_fee`, `trade_is_profitable`), `src/core/bracket_payoff.py`
(`settles_yes`, `attach_spec_to_signals`), `src/core/weather_settlement.py`
(`settlement_date_for`, `settlement_timezone_for`), `src/data/mos_guidance_provider.py`,
`scripts/go_no_go.py` (reference JSON `reports/phase2/ws_e_go_no_go_data_2026-07-26.json`),
`scripts/gas_backtest.py` (F5, with `gate=` injection added), `scripts/backfill_ladders.py
--out`, `backfill_forecasts.py`, `backfill_weather_truth.py`, `reconcile_weather.py`,
`src/ml/brier.py`, `src/core/matching_engine.py` / `risk_manager.py` / `mixins.py` (zero diff
except the pre-cap qty log line, CONTRA 3).

**Modified, gate-preserving:** `src/bots/weather_bot.py` (strategy loop, flags, ET ladder
filter at :427), `src/core/bracket_payoff.attach_spec_to_signals` (stamp `expiration_time`),
`src/core/matching_engine._load_state` (backfill `expiration_time` for weather positions),
`src/ml/trade_journal.TradeOutcome` (+`target_date`), `hermes_plugin/__init__.py` (+2 tools),
`deploy/spark/docker-compose.lab.yml` (+2 services), `.gitignore`.

**Quarantined (tainted; never a seed, never a fitness, never fed to a gate):**
`scripts/lab.py` (zero replay trades; optimizer scores unrealized), `scripts/simulate.py`
(`random.choice([-5,10])`), `src/backtest/engine.py` (fills at limit, synthesised books),
`scripts/train_models.py` (label leakage), `src/ml/predictor.py` weather fallback and
`src/strategies/ml_weather.py` (σ=0.5F implied, `confidence` identically 1.000 at :251),
`src/strategies/weather_strategy.py` V2 as a *factory* input (no clock injection, source
whitelist — stays in the sandbox as the last fallback only), `data/calibration/*_v1.json`
(in-sample), `src/backtest/metrics.py::validate_criteria`, `sandbox.py`, `README.md:51`
criteria, `src/ml/validation_harness.deflated_sharpe` (unclustered), `positive_ev_shapes`
(modelled-EV gate), `latency_arb.py`, the 14-s harvest tape as a substrate, the maker
traversal proxy as a headline, `fee_calculator.SERIES_FEE_MULTIPLIER` static allowlist
(superseded by the regime file), the sandbox equity series (use `closed_trades`).

---

## 14. Owner decisions the factory cannot make (carried from facts B §9)

1. `expiration_time` stamp location (design: `attach_spec_to_signals`, FR-1.1-safe).
2. Disable the ML Weather fallback (design: `ML_WEATHER_ENABLED=False`).
3. Confidence semantics (design: `confidence = p_win`, EV gate as-is).
4. Replay fill model / what satisfies FR-5.2 "realistic fills" (design: `quote + 1c` in
   the sim + fill-realism study; `realistic_fills` flag left as-is and not claimed).
5. Hot-load design (design: loop over `self.strategies`, env-selected genome).
6. Grouping unit for FR-5.2 (design: `target_date`), family cap (6), cutoff (2026-07-25),
   "looking" ruling — encoded in the registry and editable at ratification.
7. Whether a PROPOSED genome may paper-trade on maia before the Sept–Oct R3 verdict
   (design recommends yes: it replaces an inadmissible shape, touches no capital, and the
   factory never reads maia's tape back until RATIFIED).
8. Prod KXBTCY fee receipt, Weather Company vs IEM reconcile policy, $3000→$350 sizing —
   unchanged from HANDOFF.
