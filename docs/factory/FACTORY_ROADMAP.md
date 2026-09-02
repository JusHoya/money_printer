# MaskForge-S — Factory Roadmap (F0..F5)

Companion to `FACTORY_ARCHITECTURE.md`. Each phase is independently shippable; every exit
criterion is a check a red team can run from the repo, the run directory, or the maia API
without the author. Calendar anchors: ladder retention for 07-26..08-31 expires
~2026-10-03; M0 capture kill date 2026-09-15; M1 (Sept–Oct verdict) ~2026-11-07.

Dependency graph: F0 and F1 run in parallel from day 1. F1 → F2 (evolution). F0 + F2 → F3
(sandbox strategy). F3 + ratification + data → F4. F5 is data-gated and independent.

---

## F0 — Sandbox admissibility and data retention (days 1–5, in parallel with F1)

**Objective.** Make the maia paper leg capable of producing admissible evidence, and secure
the only virgin data the factory will ever have before Kalshi prunes it. Nothing here is
factory code; all of it gates F3/F4 and the retention items are irreversible if missed.

**Deliverables.**
1. GAP 1 — `expiration_time`: stamped tz-aware at `bracket_payoff.attach_spec_to_signals`
   from `weather_settlement.settlement_date_for` + `settlement_timezone_for`
   (day close = `combine(day+1, 00:00, tz)`); `SimulatedExchange.open_position` backfill
   for `is_weather_symbol` only (never overriding a caller-supplied value);
   `_load_state` backfill for maia positions 1–3 (`KXHIGHNY-26SEP01-T83`,
   `KXHIGHLAX-26SEP01-B76.5`, `KXHIGHMIA-26SEP01-B89.5`). Tests in
   `tests/test_weather_lifecycle.py` extended so the stamp is exercised by the
   strategy→mixin→exchange path, not set by hand.
2. ML Weather fallback: `ML_WEATHER_ENABLED` module flag (owner-only, default False) that
   removes `MLWeatherStrategy` from the WeatherBot waterfall (weather_bot.py:140-143,
   392-405). Rationale logged in HANDOFF: `confidence` is identically 1.000
   (ml_weather.py:251, hrrr defaults to nws), implied σ=0.5F, positions immortal.
3. GAP 9 — `_ladder_for_city` (weather_bot.py:427) uses
   `datetime.now(ZoneInfo('America/New_York'))` so the last 5–8 h of every city-day are
   captured on maia.
4. CONTRA 3 — EXECUTED log line and `check_order` use the post-cap quantity
   (mixins.py:490-504). Log-only; no gate change.
5. Data retention (run in the `lab` service, network on):
   `backfill_ladders.py --start 2026-07-26 --end 2026-08-31 --out data/ladders_holdout`
   (+ SHA-256 manifest, committed); `backfill_forecasts.py` to 08-31 (merge-safe);
   `backfill_ensemble_history.py --out data/forecast_archive_2026-09/` for gefs (copy the
   current CSV aside first — it overwrites); `backfill_weather_truth.py --min-days 60` to
   08-31; `reconcile_weather.py` re-run for dates ≥ 2026-08-14 (Weather Company switch)
   with the per-city-day agreement count recorded in `data/ladders_holdout/RECONCILE.md`.
6. M0 ladder-capture timer on maia (or alcyone): daily `backfill_ladders.py --out
   data/ladders_2026-09` for D-1, separate root, prune ≤ retention; committed via the
   existing dated-artifact convention (never `git add -A`).
7. Housekeeping: `pip freeze > deploy/spark/requirements-lab.lock` inside the lab image;
   compose `lab` gains `user`, `tmpfs /app/logs`, `HOME=/tmp`; alcyone checkout
   fast-forwarded to origin; `bootstrap_alcyone.sh` REPO_DIR fixed; Hermes cron
   `mp-status` re-pinned (`--provider custom --model ykarout/Qwen3.5-9B-NVFP4`).

**Exit criteria (falsifiable).**
- `GET http://192.168.50.41:8050/api/status` shows positions 1–3 with non-null tz-aware
  `expiration_time` (ISO string with offset) and, within 36 h of their close, they leave the
  book via `_settle_weather_position` (reconcile_weather.py reports ≥1 sim-leg record,
  not "NOTHING TO CHECK").
- `grep -c 'confidence=1.000' logs/money_printer_*.log` on maia returns 0 for any log
  started after the deploy; `[Signal] EMIT strategy=ML Weather` lines are absent.
- A maia tape file for a city-day contains rows after 00:00Z D+1 up to the city's close
  (04:59Z NY/MIA, 05:59Z CHI, 07:59Z LAX).
- `data/ladders_holdout/` holds ≥140 city-days with `result ∈ {yes,no}`, ≥30 dates with a
  gfs_mex vintage of lead 4–20 h, and a manifest whose `sha256sum -c` passes; the search
  frame builder refuses to load it (test).
- `data/ladders_2026-09/` has ≥7 consecutive daily files by 2026-09-15 and `git pull
  --ff-only` on maia still succeeds.
- `deploy/spark/requirements-lab.lock` exists and `docker compose run --rm lab id -u`
  prints the host uid; no root-owned file appears in the checkout after a run.
- `tests/test_v3_risk_rules.py` and `tests/test_weather_lifecycle.py` green; `git diff
  --stat` shows zero changes to `risk_manager.py` thresholds, `mixins.py` gate order,
  `matching_engine.py` fee booking.

---

## F1 — Generation 0: settlement-true baseline in the lab image (days 1–4)

**Objective.** Put a settlement-true number in front of the owner within days: the five
pre-registered seeds and the sandbox's current fallback shape, scored by a numpy kernel
proven identical to `evaluate_shape`, on both the parity frame (Phase-2 convention) and the
hardened search frame, per anchored walk-forward campaign, with a frame-level Brier
skill vs the market. No evolution yet.

**Deliverables.**
1. `src/factory/lanes/base.py`, `lanes/weather.py`, `frame.py`, `features.py`, `fees.py`
   + `configs/fees/fee_regime.csv` (seeded from the 2026-09-02 `/series` and
   `/series/fee_changes?show_historical=true` records; KXHIGH* quadratic ×1 maker $0;
   KXAAAGASM maker-fee ×1; KXBTCY/KXETHY pinned ×1 until a prod receipt).
2. `src/factory/genome.py` with GENE_SPEC v1 and the six gen-0 seeds encoded
   (`fr31a_taker`, `fr31b`, `nofilter_no`, `salvage_5f` [diagnostic], `mlweather_fallback`,
   plus `fr31a_gefs` scored on the gefs twin).
3. `src/factory/fitness.py` kernel + `score_reference()`; `folds.py` (campaigns A/B/C/ALL69
   with 2-day embargo; blocked 5-fold with 2-day purge as diagnostic); `ledger.py`;
   `registry.py` with the first family line `weather/gfs_mex/taker/v1` written before gen-0.
4. `scripts/factory.py freeze-frame | gen0 | board`; `reports/factory/gen0_<date>/summary.{json,md}`
   + `board.md`; `reports/factory/coverage.json` from `coverage.py`.
5. `deploy/spark/docker-compose.lab.yml` `factory` and `factory-holdout` services (§7.1 of
   the architecture); `.gitignore` entries.
6. `hermes_plugin` `mp_factory_status` + `mp_factory_board`; cron `mp-factory-board`
   (60 min, `--no-agent --monitor-script`, byte-hash) → Discord.
7. Tests: `tests/test_factory_fitness_parity.py`, `tests/test_factory_genome.py`,
   `tests/test_factory_frame.py` (cutoff abort, no-lookahead abort, truth-filter counts,
   hidden columns absent from the visible namespace, truth-perturbation invariance,
   row-permutation invariance, fee regime equals evaluator fee on the parity frame).

**Exit criteria (falsifiable).**
- Parity frame, `fr31a_taker`, all 69 dates: kernel reports `trades=181, dates=65,
  realized=+0.0636, se=0.0248, t=+2.57, boot=[+0.0122, +0.1086]` and every `ShapeResult`
  field equals `ev_analysis.evaluate_shape` within 1e-9; all numeric leaves of
  `reports/phase2/ws_e_go_no_go_data_2026-07-26.json` for the four Phase-2 shapes match.
- 1,000 random legal genomes: kernel vs `evaluate_shape` agree on trades/dates/realized/
  boot_lo for every genome, including the `None ↔ −inf` mapping.
- `nofilter_no` reproduces `+0.0209 / 664 trades` on the parity frame.
- `frame.py` aborts with a logged reason on (a) a synthetic ladder with
  `target_date > 2026-07-25`, (b) a synthetic row with `init_time_utc + lag > ts_utc`,
  (c) a synthetic row with `payoff_matches_kalshi == False`; the truth filter keeps the 25
  `truth_agrees=None` markets (provenance shows `dropped_truth_disagree=0`,
  `kept_truth_none=25`).
- A test that references any hidden column from `to_mask` fails at construction; a test
  that permutes hidden `won` produces identical trade sets for all six seeds.
- `gen0/summary.json` contains, for each seed, the parity-frame full-period row, the
  search-frame full-period row (σ≤4F, availability lag, sandbox_admissible), and per-campaign
  validation rows (A/B/C) with date-clustered CI; the `mlweather_fallback` row is present
  with a number (expected ≤ baseline); the frame-level BSS of the calibration vs market mid
  on all two-sided rows is printed with a date-clustered CI.
- Measured ≥3,000 single-window genome evaluations/s on 16 workers inside the `factory`
  service with mp-vllm running; `docker stats` peak RSS < 8 GB; `mp-vllm` container id,
  config and `docker stats` memory unchanged before/after.
- `run.json` carries a non-empty git rev and the lock hash; the container runs as the
  host uid.
- Hermes posts the gen-0 board to Discord within 4 days of F1 start; `mp_factory_status`
  returns the same numbers as `summary.json`.

---

## F2 — Evolution, walk-forward campaigns, ledger multiplicity, control runs (days 5–14)

**Objective.** Run the full evolutionary procedure end to end on the frozen weather frame,
prove it is deterministic and resumable, prove it finds nothing when there is nothing and
finds what is planted, and produce the first pooled out-of-sample number for the procedure
— accepted as the deliverable whatever its sign.

**Deliverables.**
1. `src/factory/evolve.py` (μ=400, tournament 4, elitism 5%, phenotype niching with
   Jaccard>0.90 removal from the breeding pool, 5% immigration, kill codes, atomic
   checkpoints, resume, `status.json`); `guards.py` tripwire; RNG by
   `hash(master_seed, campaign, gen)`.
2. `src/factory/multiplicity.py` (RC/SPA over the ledger, Holm across registry entries,
   clustered DSR); `null.py` (snapshot-efficient ×20, residual-shuffle ×20, planted-edge ×1);
   `report.py`.
3. `scripts/factory.py run | resume | controls | report`; `configs/factory/
   weather_gfs_mex_taker_v1.yaml` (budget, picker, thresholds, seeds); `deploy/spark/
   mp-factory@.service`.
4. Family #1 run: campaigns A/B/C/ALL69 + blocked-5-fold diagnostic + the 41 control
   replicates; `reports/factory/<run_id>/{summary.json, summary.md, oos_by_date.csv,
   finalists.json, board.md}`.
5. Coexistence bench: throughput and mp-vllm p50 token latency (fixed prompt) with the
   factory idle vs running, recorded in `reports/factory/<run_id>/bench.json`.
6. Tests: `tests/test_factory_evolve.py` (determinism, resume, synthetic-edge recovery,
   tripwire raises), `tests/test_factory_multiplicity.py` (RC p uniform on iid noise; Holm
   ordering).

**Exit criteria (falsifiable).**
- Two runs with the same master seed produce byte-identical `gen_*.parquet`; `kill -9`
  at generation 17 followed by `factory.py resume` produces the same final pick per campaign
  as the uninterrupted run.
- Planted-edge control: pooled validation of the recovered genome captures ≥80% of the
  planted +5c/contract.
- Snapshot-efficient null: ≤1 of 20 replicates reports pooled validation `boot_lo > 0`;
  `p_RC` across the 20 replicates' picks is not concentrated below 0.10 (KS test vs uniform
  p > 0.05).
- Residual-shuffle null: the 20 pooled-validation means are recorded; the real pick's rank
  among them is printed on the board.
- Family #1 report contains: per-campaign pick (genes, phenotype hash, in-sample boot_lo),
  validation-block result per pick, pooled 33-date OOS mean/se/t/boot CI, `p_RC` and SPA p
  per campaign and for ALL69, Holm-adjusted p, clustered DSR, N distinct phenotypes,
  paired-vs-no-filter test, 2c/3c/embargo-2 sensitivity signs, BSS_trades, phenotype
  Jaccard between the A/B/C/ALL69 picks; it states PASS-to-PROPOSED or CLOSED and the
  registry line reflects it. Either outcome ships the phase.
- `factory.py report <run_id>` recomputes every number in `summary.json` from the ledger
  and frame alone (byte-identical JSON after provenance-path normalisation).
- Full cycle (real run + 41 controls) < 4 h wall on the 16-core cpuset with mp-vllm
  serving; mp-vllm p50 token latency changes ≤10%; `free -g` used stays ≤ 40 GiB.
- Discord receives ≥3 progress posts from the monitor cron during the run and one
  `hermes send` at completion.
- A test that calls `datetime.now()` from `src/factory/genome.py` inside a worker raises.

---

## F3 — GenomeStrategy, live-vintage provider, replay parity, promotion plumbing (days 10–20, parallel with F2's run)

**Objective.** Make the promotion path real without touching a gate: a promoted genome
JSON becomes `GenomeStrategy` in the maia sandbox with injected clock and forecast
provider, hourly decision cadence, tz-aware expiration, pessimistic limit price, and a
literal trade-set match against the offline mask; build the FR-5.2 gate script and the
weekly lab-vs-paper reconciliation.

**Deliverables.**
1. `src/strategies/genome_strategy.py` (numpy-only imports; `clock`, `forecast_provider`,
   `fee_regime`, `calibration` injected; refuses on fee-type or calibration-hash mismatch;
   `log_rejection` codes `GENOME_NO_VINTAGE`, `GENOME_MASK_FALSE`, `GENOME_ALREADY_TRADED`,
   `GENOME_FEE_MISMATCH`).
2. `src/data/forecast_vintage_provider.py` over `MOSGuidanceProvider` with `fetched_at`
   recording and the availability-lag rule; a small on-disk cache under
   `/srv/money_printer/data/forecast_cache/`.
3. `weather_bot.py`: loop over `self.strategies` in declared order (behaviour-preserving
   for the existing two); `GENOME_STRATEGY_ID` env → `GenomeStrategy` inserted before V2;
   bot passes `clock=lambda: datetime.now(ZoneInfo('America/New_York'))` and the provider.
4. `configs/factory/promoted/<id>.json` format + loader with `config_hash` verification;
   `scripts/factory.py promote <id>` (copies, runs parity, refuses on mismatch).
5. `scripts/factory_replay_parity.py`: drives `GenomeStrategy` over all 276 ladder
   city-days with `clock = ts_utc` at each hourly candle and the vintage table as the
   provider; diffs `(market_ticker, ts_utc, contract_side, limit_price)` against the
   offline first-in-market trade set.
6. `scripts/gate.py` (FR-5.2 on `target_date` grouping from journal + closed_trades) and
   `TradeOutcome.target_date`; `gate_registration.json` template.
7. `scripts/factory_paper_reconcile.py` (weekly; re-prices sandbox fills with the lab
   formula; lists REJECT codes for lab-admissible trades the sandbox skipped).
8. `scripts/measure_fill_realism.py` on the 14-s maia tape: distribution of bid/ask drift
   within the 20-s cadence at hourly decision points; recommends `adverse_fill`.
9. Tests: `tests/test_genome_strategy.py` (parity, tz-aware expiration, no `datetime.now`
   / `time.time` in module, works with source='replay' and live), `tests/test_factory_isolation.py`
   (import graph of `run_dashboard`/`run_web_dashboard` contains only `src.factory.genome`
   and `src.factory.features`).

**Exit criteria (falsifiable).**
- Replay parity: 0 discrepancies between `GenomeStrategy`'s emitted set and the offline
  search-frame trade set over 1,656 markets for each of the six gen-0 seeds and the F2
  family-#1 picks; `p_yes` from the live path equals the frame's within 1e-9 for the same
  calibration payload.
- `grep -nE 'datetime\.now|time\.time' src/strategies/genome_strategy.py
  src/factory/features.py src/factory/genome.py` returns nothing.
- Every emitted signal has tz-aware `expiration_time` equal to the market's settlement-day
  close; a 24-h dry run on the dev box with `SimulatedExchange` settles the positions.
- On maia, `GenomeStrategy` EMIT lines appear at :00 UTC boundaries only; each has exactly
  one EXECUTED or REJECT line (FR-0.4); `limit_price` equals the logged quote + 0.01.
- `tests/test_v3_risk_rules.py`, `tests/test_weather_lifecycle.py` green; `git diff` on
  `risk_manager.py`, `mixins.py` (except the CONTRA-3 log fix), `matching_engine.py`
  (except `_load_state` backfill) is empty.
- The sandbox image builds without lightgbm/scipy/pyarrow and `python -c "import
  src.strategies.genome_strategy"` succeeds inside it.
- `gate.py` on a synthetic journal of 60 settled trades over 50 target_dates returns the
  exact binomial p and the PASS/FAIL verdict matching a hand computation in the test.
- `measure_fill_realism.py` report exists with the 90th-percentile drift; if it exceeds
  1c the registry line for family #1 records the raised `adverse_fill` and the F2 report is
  re-scored under it (a re-score, not a re-search).

---

## F4 — Sealed holdouts, R3 scoring, promotion, paper run under the gate (after ratification; work ≈ 4 days spread over the data wait)

**Objective.** Score the proposed genome exactly once on each never-searched root, run
the pre-registered R3 checks, promote through git to maia, and accumulate an admissible
paper record toward FR-5.2.

**Deliverables.**
1. `scripts/factory.py holdout --finalists --unseal RATIFIED-<date>` on
   `data/ladders_holdout` (≤3 finalists, Holm), appended to `unseal_log.jsonl`.
2. `scripts/factory.py score --genome <id> --ladders data/ladders_2026-09` (R3 checks;
   result sha256 printed before numbers; registry append).
3. `docs/FACTORY.md` runbook (registry discipline, family cap, controls, how to add a
   lane, promotion steps); HANDOFF.md entry recording the verdict either way.
4. Promotion commit: `configs/factory/promoted/<id>.json`, `GENOME_STRATEGY_ID`,
   `gate_registration.json`; maia deploy; `ML_WEATHER_ENABLED=False` confirmed.
5. Weekly `factory_paper_reconcile.py` cron + PAPER row on the board; `gate.py` verdict
   file `reports/factory/gate_<id>.json`.

**Exit criteria (falsifiable).**
- `unseal_log.jsonl` has exactly one entry per family for holdout-B listing ≤3 finalists
  with Holm-adjusted p; the search-frame builder still refuses the holdout root.
- `data/ladders_holdout` truth filter drops < 10% of city-days after the Weather Company
  re-reconcile (count printed in the holdout report).
- R3 computed once on `data/ladders_2026-09`; the registry shows PASS or HALT #3 with the
  result hash; no second scoring of the same genome on the same root exists anywhere in the
  registry or unseal log.
- Holm-adjusted p across all registry families < 0.05 for the promoted genome, on both
  holdout-B and Sept–Oct.
- maia: `GenomeStrategy` positions settle via `reconcile_weather.py` within 3 days;
  `mp_factory_board` PAPER row shows settled `target_date` count, sandbox c/contract from
  `closed_trades`, and the factory's pooled-OOS prediction side by side.
- After ≥50 settled `target_dates`: `gate_<id>.json` reports grouped count, exact binomial
  p vs fee-adjusted breakeven at actual entry, net PnL, spec hash unchanged; PASS requires
  all four. If HALT: the board shows the weather family KILLED, and no file under
  `src/factory/` references any live-capital flag (grep).

---

## F5 — Second lanes, GENE_SPEC v2, richer genes (data-gated; 2027 unless data arrives earlier)

**Objective.** Extend the same frame/kernel/registry to lanes and genes that lack
statistics today, without changing the fitness definition.

**Deliverables.**
1. GENE_SPEC v2: ex-ante lagged features in `features.py` (`yes_bid/yes_ask` lag 1h/3h,
   `dvolume_1h`, `doi_1h`, `hours_since_first_quote`) with a no-forward-reference test;
   threshold genes `bid_move_1h`, `oi_lo`, `hours_quoted_lo`, `exec_edge`; new family line.
2. Gas lane: `lanes/gas.py` with BOTH `clock=` and `gate=` injected into
   `GasConvergenceStrategy` via `gas_backtest.py`; gen-0 re-score of the 5-axis
   sensitivity sweep labelled NOT_PROMOTABLE (14 events); maia timers `aaa --record`
   12:00Z and `reconcile_gas` 15:00Z.
3. Mention: settled-markets harvester `scripts/harvest_settled.py`
   (`/markets?series_ticker&status=settled` → `data/<lane>_truth/settled_<SERIES>.jsonl`,
   ≥6 h); lane frame builder once ≥40 joined events exist (post-ratification tape).
4. Maker mode gene gated on maia resting-fill evidence; sizing gene {5,10,20,50} with
   per-size fee recomputation.
5. `coverage.py` weekly cron; new-epoch trigger when a lane gains ≥7 independent units.

**Exit criteria (falsifiable).**
- Gas gen-0 reproduces `n_accepted = 198/376` for July with the gate injected (the
  2026-09-01 zero-trade artefact is gone) and the weekly figure (351 trades, −1.78c,
  CI [−9.41, +5.85], 10 settlements); the board shows gas NOT_PROMOTABLE n_units=14;
  `factory.py run --lane gas` exits non-zero with the 40-unit assertion.
- Mention truth JSONL grows by ≥1 event/day with quotes joinable to each settled market;
  board shows NOT_READY with the quoted-events count and an ETA.
- v1 genomes decode under v2 with new genes OFF and the F1 parity test still passes;
  lagged-feature test: shuffling later rows of a market cannot change earlier rows'
  features.
- Any maker-mode genome in a search carries `fill_model=traversal_proxy` and is excluded
  from PROPOSED until the fill-realism study reports resting-fill rates on ≥30 city-days.

---

## Effort

F0 ≈ 3 days (fixes + backfills, mostly waiting on throttled pulls). F1 ≈ 3–4 days
(~800 LOC + tests). F2 ≈ 6–8 days (~1,000 LOC + tests; a cycle is < 4 h of alcyone time).
F3 ≈ 5–7 days (~700 LOC + tests). F4 ≈ 4 days of work over the data wait. F5 ≈ 5+ days,
data-gated. First owner-visible settlement-true number: day 3–4. First pooled OOS report:
end of week 2. Roughly 4 engineer-weeks to a promotable-or-closed answer; compute is
never the calendar — 69 dates and the Sept–Oct ladders are.
