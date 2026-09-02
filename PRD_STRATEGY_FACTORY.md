# PRD — Money Printer Strategy Factory ("MaskForge-S")

**Status:** Draft v1.0 — 2026-09-02
**Branch:** `revival/pleiades-2026-09`
**Execution model:** each phase below is executed with `/sprint Phase F<N>` and red-teamed
against its exit criteria before the next phase begins. Phases are numbered **F0–F5** so
they never collide with the 2026-07-24 pivot PRD (`PRD.md`, Phases 0–5), whose FR-0..FR-5
requirements remain the governance record this document builds on.
**Design records (read these before implementing):**
`docs/factory/FACTORY_ARCHITECTURE.md` (the complete spec — genome, fitness, folds,
multiplicity, compute, storage, promotion, anti-self-deception ledger) and
`docs/factory/FACTORY_ROADMAP.md` (the full falsifiable exit-criteria lists). Both are the
synthesis of a 15-agent code+cluster survey (`docs/factory/survey_*.md`), four independent
architecture proposals and three adversarial judging lenses (statistician, systems
engineer, governance officer). This PRD is the contract; the records are the detail.

---

## 1. Overview & Vision

The owner asked for *"a strategy creation factory with the goal of developing the optimal
strategy for leveraging our data sources to maximize PnL … using an evolutionary technique
where bad strategies are culled automatically and good ones propagate … running on our
cluster hardware locally in the most optimized fashion."*

We are building an **offline evolutionary search over trading-rule genomes**, judged by the
one thing this project has learned to trust: **settlement-true realized PnL after real fees
with pessimistic fills**, computed on the Phase-2 opportunity frame that already joins
Kalshi hourly ladders, no-lookahead forecast vintages, walk-forward calibration and the
NWS climate-report truth. A candidate strategy is a 13-gene typed rule (which side, which
time windows, which forecast-distance bands, which probability/quote/margin thresholds)
that is a legal sandbox rule by construction. Evolution runs as a 16-core CPU fork-pool on
alcyone, never touching the GPU or the Hermes model. A winning genome compiles into
`GenomeStrategy`, which evaluates the identical predicate on live ladder rows in the maia
paper sandbox behind every existing risk gate — and only from there, through the
pre-registered FR-5.2 gate, can anything ever reach capital.

The experience the factory must deliver: **a machine that cannot lie to itself.** It writes
its family registration before it has a result; it keeps a ledger of every phenotype it
ever tried and corrects for all of them; it physically removes validation data from the
workers; it proves on planted edge that it finds what is there and on shuffled truth that
it finds nothing when nothing is there; and it reports **CLOSED** ("nothing beats the
baseline after correction") with the same prominence as **PROPOSED**. Two HALT verdicts
came from modelled edge beating settlement truth; an evolutionary search rediscovers every
such illusion unless the fitness forbids it, so here the fitness *is* the settlement.

The first owner-visible settlement-true number (the pre-registered seed shapes and the
sandbox's current fallback shape, scored by the new kernel) lands within days; the first
pooled out-of-sample report for the whole procedure by the end of week two.

## 2. Target User & Context

- **User:** the project owner — single operator, expert engineer, time-constrained;
  interacts via Discord (Hermes on alcyone), the maia web dashboard, and log/report review.
- **Operator context:** alcyone (DGX Spark GB10, 20 aarch64 cores, 121 GB unified memory,
  ~22 GB pinned by `mp-vllm`) runs the factory and Hermes; maia (Pi 4) runs only the paper
  sandbox. Results must be readable from Discord (`mp_factory_status`, `mp_factory_board`)
  in under a minute and recomputable from a run directory in under an hour.
- **Agent context:** built by orchestrated agent teams via `/sprint`; every requirement is
  verifiable by a red team from the repo, the run directory, or the maia API alone.

## 3. Goals & Non-Goals

### Goals
1. A **settlement-true, fee-inclusive, no-lookahead fitness kernel** proven equal to the
   Phase-2 evaluator (`evaluate_shape`) to 1e-9 before any genome is bred.
2. An **evolutionary procedure** (population, tournament, elitism, niching, kill rules,
   checkpoints, resume) whose headline is the **pooled out-of-sample** return of a
   pre-registered picker over anchored walk-forward campaigns, with **multiplicity
   control** over every phenotype ever evaluated and three control runs.
3. A **promotion path that touches no gate**: genome JSON → `GenomeStrategy` in the maia
   sandbox (replacing the inadmissible ML-Weather fallback) → sealed-holdout and R3 scoring
   once each → the FR-5.2 paper gate.
4. **Sandbox admissibility fixes** the survey exposed (weather positions that can never
   settle; `confidence` identically 1.000) and **data retention** (the 2026-07-26..08-31
   ladders expire ~2026-10-03 — the only virgin holdout the factory will ever have).
5. **Lane extensibility**: a `Lane` interface with a coverage board that states honestly
   which data sources are READY (weather), NOT_PROMOTABLE (gas, 14 events) and NOT_READY
   (mention, tweets, crypto_annual).
6. Runs on the cluster **in the most optimized fashion that the statistics allow**: the
   binding constraint is 69 development dates, not FLOPs; a full cycle including 41
   control replicates is < 4 h of alcyone wall time.

### Non-Goals
- **No trading by the factory, ever.** It has no Kalshi credentials, no network in its
  container, and no path to capital. Live capital remains the owner's separate M3 decision
  after the FR-5.2 gate.
- No weakening or reordering of any RiskManager / SignalProcessorMixin gate; no change to
  fee booking in the matching engine.
- No GPU use, no second vLLM instance, no LLM-proposed mutations in v1 (deferred; it would
  time-share SMs with Hermes).
- No ML training in the runtime process (FR-0.2 stands; `features.py` and `genome.py`
  are the only factory modules the sandbox imports).
- No revival of the quarantined replay paths (`scripts/lab.py`, `scripts/simulate.py`,
  `src/backtest/engine.py`, `scripts/train_models.py`) as seeds, fitness or gate inputs.
- No mention/tweets/crypto_annual search until their lanes hold ≥40 independent units.

## 4. Key Assumptions & Decisions

| # | Decision | Rationale |
|---|---|---|
| A1 | Base design = **MaskForge** (rule genomes over the Phase-2 frame), synthesized as MaskForge-S with grafts from PROBE (feature allowlist, Brier-skill-vs-market kill/gate, anchored campaigns, sealed holdout ledger), FORGE (ledger Reality-Check/SPA, planted-edge control, determinism tests, shared `features.py`), LANEWORKS (fee regime as data, write-then-evaluate ledger, wall-clock tripwire, Lane board) | 2 of 3 judges (56/59 of 70); maximal reuse of the only settlement-true machinery that exists; smallest new surface (~2.5k LOC) |
| A2 | Fitness = date-clustered realized PnL per contract after C=20 quadratic fees, taker path, `price_paid = quote + $0.01`; in-sample selection on the bootstrap **lower** bound; modelled EV is a hidden diagnostic and never an input | HANDOFF rules; both HALTs were modelled-EV failures |
| A3 | The 69 development dates (2026-05-18..07-25) are declared **already searched** (Phase 2 swept them); first true out-of-sample = sealed holdout-B (07-26..08-31), then the Sept–Oct R3 reserve | Honest multiplicity accounting |
| A4 | Headline = pooled OOS of the pre-registered picker over anchored campaigns A/B/C (2-day embargo, 33 validation dates); blocked 5-fold is a labelled diagnostic only | Walk-forward is the only defensible OOS on a time series |
| A5 | Multiplicity: White RC / Hansen SPA over the phenotype ledger, Holm across registered families (cap 6 before 2027), clustered Deflated Sharpe reported; the arithmetically unreachable `p<0.05/K` null-run gate is not used | Continuous p, no `1/(K+1)` floor |
| A6 | Genes freeze what the HALTs proved dangerous: city subset (all four), forecast source (`gfs_mex`), sizing, entries per market (1) are **not** genes; `sigma_f ≤ 4F` is applied to the search frame pre-selection | R1/R3 of `docs/REVIVAL_2026_09.md` |
| A7 | Compute: CPU-only fork-pool of 16 workers on cpuset `0-3,5-9,10-11,15-19`, `nice 10`, `mem_limit 24g`, `network_mode: none`, non-root, sealed roots not mounted; cores `{4,12,13,14}` left to `mp-vllm` and Hermes | Measured 0.37 ms/candidate/core; the frame is 30 MB; "must not disturb Hermes" |
| A8 | The factory's only artifact that reaches maia is `configs/factory/promoted/<id>.json`; deployment is the existing `git pull --ff-only && compose up -d --build`; no hot-load mechanism | One promotion path, auditable in git |
| A9 | `GenomeStrategy` **replaces** the ML-Weather fallback in the per-city slot (`ML_WEATHER_ENABLED=False`) rather than being appended; V2 stays as the last fallback | Appended strategies are starved by `WEATHER_SLOT_FULL` and the immortal `confidence=1.000` positions |
| A10 | Sandbox fills are booked at `limit = quote + adverse_fill` (the sim fills at limit), taker fee; `realistic_fills=True` is a penny coin-flip and is **not** claimed as a fill model; a fill-realism study on the maia 14-s tape sets `adverse_fill` | Governance judge; owner decision #4 below |
| A11 | Forecast availability lag: the search frame requires `init_time + 240 min ≤ ts` (MOS MEX issues ~4 h after init); the parity frame keeps Phase-2's lag 0 so the 1e-9 test is exact | New finding — no proposal caught the 1–4 h lookahead |
| A12 | Owner-decision defaults are encoded and editable at ratification: `expiration_time` stamped in `attach_spec_to_signals`; `confidence = p_win`; FR-5.2 grouping unit `target_date`; a PROPOSED genome may paper-trade before the Sept–Oct verdict | §9 lists all eight |

## 5. Functional Requirements

### FR-F0 Sandbox admissibility & data retention
- FR-F0.1 Every weather `TradeSignal` carries a tz-aware `expiration_time` (settlement-day
  close from `weather_settlement`), stamped in `bracket_payoff.attach_spec_to_signals`;
  `SimulatedExchange.open_position` backfills it for weather symbols when absent;
  `_load_state` backfills the three live maia positions. The settlement branch
  (`_settle_weather_position`, FR-1.2) becomes reachable from the live path.
- FR-F0.2 `ML_WEATHER_ENABLED` module flag (default **False**) removes `MLWeatherStrategy`
  from the WeatherBot waterfall; the reason (confidence identically 1.000 because the
  fallback compares a forecast to itself; implied σ = 0.5F) is recorded in HANDOFF.md.
- FR-F0.3 `_ladder_for_city` filters by the ET date, so the last 5–8 h of each city-day
  are captured on maia.
- FR-F0.4 The EXECUTED log line and `check_order` use the post-cap quantity (log-only).
- FR-F0.5 Retention: `data/ladders_holdout/` (07-26..08-31) backfilled with SHA manifest,
  forecasts, gefs, truth and the ≥08-14 re-reconcile; a daily capture timer for
  `data/ladders_2026-09/` (kill date 2026-09-15). The search-frame loader **refuses** both
  roots.
- FR-F0.6 Housekeeping: `deploy/spark/requirements-lab.lock`; lab compose runs as host uid
  with `tmpfs /app/logs`; alcyone checkout fast-forwarded; Hermes `mp-status` cron re-pinned.

### FR-F1 Frame, genome, kernel, generation 0
- FR-F1.1 `src/factory/lanes/weather.py` builds frames through the **unchanged**
  `ev_analysis` chain; `frame.py` applies the hardening: cutoff assert (2026-07-25 until
  RATIFIED), truth filter (`result ∈ {yes,no}`, `payoff_matches_kalshi != False`,
  `truth_agrees != False`), no-lookahead assert with availability lag, `sigma_f ≤ 4F`,
  `sandbox_admissible` (the maia EV gate at C=1 folded into `executable`), fee regime from
  `configs/fees/fee_regime.csv`, **visible/hidden column split**, provenance sha256 of
  every input, the frame, the lock file and the git rev (abort if empty).
- FR-F1.2 `genome.py`: GENE_SPEC v1 (13 genes, finite domains), encode/decode to
  `np.int16`, random/mutate/crossover/repair, `to_mask` that works identically on a frame
  and on a single live row, `phenotype_hash`, JSON round-trip. numpy-only.
- FR-F1.3 `fitness.py`: `score()` reproduces `evaluate_shape` (first masked executable
  snapshot per market, per-date clustering, the identical 4000-draw seeded bootstrap);
  hard constraints (`n ≥ 0.6|D|` dates, ≥40 trades, ≥3 cities, worst date ≥ −0.50,
  gefs-twin ≥ 0, ≤8 clauses, `BSS_trades ≥ −0.05`) → −inf with reason code; `fit = boot_lo`.
- FR-F1.4 `folds.py` (campaigns A/B/C/ALL69 with 2-day embargo; blocked 5-fold diagnostic),
  `ledger.py` (write-then-evaluate parquet, per-date vectors), `registry.py` (family line
  written **before** any result), `coverage.py`, `report.py`.
- FR-F1.5 Gen-0 seeds encoded exactly: `fr31a_taker`, `fr31b`, `nofilter_no`,
  `salvage_5f` (diagnostic), `mlweather_fallback` (what maia trades today), `fr31a_gefs`.
- FR-F1.6 `scripts/factory.py freeze-frame | gen0 | board`; compose `factory` and
  `factory-holdout` services; `.gitignore` entries; Hermes `mp_factory_status` and
  `mp_factory_board` tools plus the hourly byte-hash monitor cron to Discord.

### FR-F2 Evolution, campaigns, multiplicity, controls
- FR-F2.1 `evolve.py`: μ = 400, tournament 4, elitism 5 %, phenotype niching (Jaccard
  > 0.90 removed from the breeding pool), 5 % immigration, per-gene mutation 1/L with
  legality repair, uniform crossover, kill codes, atomic checkpoints, resume, timestamp-free
  `status.json`; RNG by `hash(master_seed, campaign, gen)`.
- FR-F2.2 Workers receive frames with validation and embargo rows **physically removed**;
  the picker (highest search-window `boot_lo` among constraint-satisfying elites; ties →
  fewer clauses) is checkpointed before the validation block is scored once by the main
  process.
- FR-F2.3 `multiplicity.py`: RC/SPA over the ledger per campaign; Holm across registry
  entries; clustered DSR reported.
- FR-F2.4 `null.py` control runs, each the full procedure: snapshot-efficient null ×20,
  residual-shuffle null ×20, planted-edge positive control ×1.
- FR-F2.5 `guards.py`: `datetime.now`/`time.time` raise inside workers when called from
  strategy, genome or features code.
- FR-F2.6 Coexistence bench recorded (`bench.json`): throughput and `mp-vllm` p50 token
  latency with the factory idle vs running.

### FR-F3 GenomeStrategy and promotion plumbing
- FR-F3.1 `src/strategies/genome_strategy.py`: numpy-only; `clock`, `forecast_provider`,
  `fee_regime`, `calibration` injected; hourly decision cadence; `limit_price = quote +
  adverse_fill`, `confidence = p_win`, `is_maker=False`, tz-aware `expiration_time`;
  refuses to construct on fee-type or calibration-hash mismatch; FR-0.4 reject codes.
- FR-F3.2 `src/data/forecast_vintage_provider.py` over `MOSGuidanceProvider` with
  `fetched_at` recording and the availability-lag rule.
- FR-F3.3 `weather_bot.py` loops over `self.strategies` in declared order;
  `GENOME_STRATEGY_ID` env inserts `GenomeStrategy` before V2; the bot injects the clock.
- FR-F3.4 `scripts/factory_replay_parity.py` (live path vs offline trade set, 1,656
  markets), `scripts/gate.py` (FR-5.2: ≥50 settled trades grouped by `target_date`, exact
  binomial p < 0.05 vs fee-adjusted breakeven, net PnL > 0, spec hash unchanged),
  `scripts/factory_paper_reconcile.py` (weekly lab-vs-paper re-pricing with REJECT codes),
  `scripts/measure_fill_realism.py`.

### FR-F4 Sealed holdouts, R3, promotion, paper run
- FR-F4.1 `factory.py holdout --finalists --unseal RATIFIED-<date>` (≤3 finalists, Holm,
  logged to `unseal_log.jsonl`, once per family); `factory.py score` on the Sept–Oct root
  prints the result sha256 **before** the numbers and appends the R3 checks to the registry.
- FR-F4.2 Promotion commit (`configs/factory/promoted/<id>.json`, `GENOME_STRATEGY_ID`,
  `gate_registration.json` committed before the first paper trade); `docs/FACTORY.md`
  runbook; HANDOFF.md verdict entry either way; weekly reconcile cron; gate verdict file.

### FR-F5 Second lanes and GENE_SPEC v2 (data-gated)
- FR-F5.1 GENE_SPEC v2 ex-ante lagged quote genes with a no-forward-reference test; v1
  genomes decode under v2 with new genes OFF and parity still passes.
- FR-F5.2 Gas lane with `clock=` **and** `gate=` injection (NOT_PROMOTABLE at 14 events);
  mention settled-markets harvester toward a ≥40-event lane; maker mode and sizing genes
  gated on resting-fill evidence.

## 6. Design & Experience Requirements

- **Honesty is the interface.** Every report states PROPOSED / CLOSED / HALT with the
  registry line that pre-committed the thresholds; `summary.md` leads with pooled OOS
  mean, CI, `p_RC`, Holm p, N phenotypes, and the rank among the null replicates; a CLOSED
  family is published with the same layout as a PROPOSED one.
- **Recomputable, not asserted.** `factory.py report <run_id>` regenerates every number in
  `summary.json` from the ledger and frame alone (byte-identical after path normalisation);
  `score` prints the result hash before the numbers.
- **Board** (`board.md`, Discord via `mp_factory_board`): one row per lane —
  status, family, pick, pooled OOS lo..hi, dates, trades, `p_RC`, Holm p, vs no-filter,
  vs fr31a, N phenotypes, controls, coverage units / next-data ETA — plus a PAPER row
  (settled `target_date`s, sandbox c/contract from `closed_trades` beside the prediction).
- **Quiet progress.** `status.json` is timestamp-free so the byte-hash monitor cron posts
  only on change; `hermes send` on run end, family close and every unseal.
- **CLI.** `scripts/factory.py` subcommands `freeze-frame, gen0, run, resume, controls,
  report, holdout, score, promote, coverage, board`; `run` refuses without a registry
  line and ≥40 independent units.
- **Failure is loud.** Cutoff, lookahead, truth-disagreement, empty git rev, frame/lock
  hash mismatch on resume, hidden-column reference — all abort with a logged reason.

## 7. Architecture & Tech Stack

```
ladders (data/ladders, 276 city-days) + forecast vintages + CLI truth
   │  unchanged ev_analysis chain (walk-forward calibration, embargo 1)
   ▼
frame.py  ── parity frame (lag 0; the 1e-9 test only)
          ├─ search frame (truth filter, cutoff, no-lookahead lag, σ≤4F,
          │                sandbox_admissible, visible/hidden split)
          └─ gefs twin (ex-ante disqualifier)
   ▼ registry.jsonl family line (BEFORE results)
evolve.py per campaign A/B/C/ALL69 — Pool(16), validation rows physically absent
   ▼ ledger (write-then-evaluate) → pre-registered picker → validation scored ONCE
multiplicity.py (RC/SPA, Holm, clustered DSR) · null.py (20+20+1 controls)
   ▼ report.py → reports/factory/<run_id>/ · board.md · latest.json → Hermes/Discord
   ▼ (human) PROPOSED → holdout-B once → R3 once → promotion commit
maia: GenomeStrategy (same to_mask) → every existing gate → SimulatedExchange
      → reconcile_weather daily → factory_paper_reconcile weekly → gate.py
```

- **Offline package** `src/factory/` (lanes, frame, features, genome, fitness, folds,
  ledger, evolve, null, multiplicity, registry, holdout, guards, fees, report, coverage) —
  numpy/pandas/pyarrow in the lab image; `features.py` and `genome.py` numpy-only so the
  Pi image imports them.
- **Runtime** `src/strategies/genome_strategy.py`, `src/data/forecast_vintage_provider.py`,
  small `weather_bot.py` change, `scripts/gate.py`, `scripts/factory_paper_reconcile.py`.
- **Deploy** `deploy/spark/docker-compose.lab.yml` services `factory` (no network, no GPU,
  16-core cpuset, non-root, sealed roots absent) and `factory-holdout`;
  `deploy/spark/mp-factory@.service` (systemd --user one-shot, `Restart=on-failure`).
- **Storage** `configs/factory/` (tracked configs + promoted genomes), `configs/fees/`,
  `data/factory/` (ignored frames/runs), `data/ladders_holdout/` and
  `data/ladders_2026-09/` (tracked, sealed), `reports/factory/` (registry, unseal log,
  coverage, per-run summaries, `latest.json`).
- **Reused unchanged:** `ev_analysis`, `kalshi_history`, `forecast_calibration`,
  `probability_engine`, `fee_calculator`, `bracket_payoff`, `weather_settlement`,
  `mos_guidance_provider`, `go_no_go` (reference JSON), the backfill/reconcile scripts,
  `matching_engine`/`risk_manager`/`mixins` (zero gate diff).
- **Quarantined:** see `docs/factory/FACTORY_ARCHITECTURE.md` §13.

## 8. Phased Roadmap

Dependency graph: F0 ∥ F1 from day 1 → F2 → F3 (∥ F2's run) → F4 (after ratification and
data) ; F5 is data-gated. Calendar anchors: holdout ladders expire ~2026-10-03; M0 capture
kill date 2026-09-15; M1 verdict ~2026-11-07. Full criteria: `docs/factory/FACTORY_ROADMAP.md`.

### Phase F0 — Sandbox admissibility and data retention (days 1–5)
**Objective:** make the maia paper leg capable of producing admissible evidence and secure
the only virgin data before Kalshi prunes it.
**Deliverables:** FR-F0.1–F0.6.
**Exit criteria:**
- `GET /api/status` on maia shows positions 1–3 with non-null tz-aware `expiration_time`,
  and within 36 h of their close they leave the book via `_settle_weather_position`
  (`reconcile_weather.py` reports ≥1 sim-leg record, not "NOTHING TO CHECK").
- `grep -c 'confidence=1.000'` on any maia log started after the deploy returns 0; no
  `[Signal] EMIT strategy=ML Weather` lines.
- A maia tape file for a city-day contains rows after 00:00Z D+1 up to the city's close.
- `data/ladders_holdout/` holds ≥140 city-days with `result ∈ {yes,no}`, ≥30 dates with a
  gfs_mex vintage of lead 4–20 h, a passing SHA manifest; the search-frame builder refuses
  it (test). `data/ladders_2026-09/` has ≥7 consecutive daily files by 2026-09-15.
- `requirements-lab.lock` exists; the lab container runs as the host uid; no root-owned
  file appears in the checkout.
- `tests/test_v3_risk_rules.py`, `tests/test_weather_lifecycle.py` green; `git diff --stat`
  shows zero changes to risk thresholds, mixin gate order, or fee booking.

### Phase F1 — Generation 0: settlement-true baseline (days 1–4)
**Objective:** a settlement-true number in front of the owner within days, from a kernel
proven identical to the evaluator, on parity and hardened frames, per campaign.
**Deliverables:** FR-F1.1–F1.6 and tests `test_factory_fitness_parity.py`,
`test_factory_genome.py`, `test_factory_frame.py`.
**Exit criteria:**
- Parity frame, `fr31a_taker`, all 69 dates: `trades=181, dates=65, realized=+0.0636,
  se=0.0248, t=+2.57, boot=[+0.0122, +0.1086]`, every `ShapeResult` field within 1e-9 of
  `evaluate_shape`; the four Phase-2 shapes match `reports/phase2/ws_e_go_no_go_data_2026-07-26.json`.
- 1,000 random legal genomes agree with `evaluate_shape` on trades/dates/realized/boot_lo
  (including the `None ↔ −inf` mapping); `nofilter_no` reproduces +0.0209 / 664 trades.
- `frame.py` aborts on a post-cutoff date, a lookahead row, and a payoff mismatch; keeps
  the 25 `truth_agrees=None` markets (provenance `kept_truth_none=25`).
- A `to_mask` referencing a hidden column fails at construction; permuting hidden `won`
  leaves all six seed trade sets unchanged.
- `gen0/summary.json` has parity-frame, search-frame and per-campaign validation rows for
  every seed including `mlweather_fallback`, plus the frame-level Brier skill vs market
  with a date-clustered CI.
- ≥3,000 genome evaluations/s on 16 workers with `mp-vllm` running; peak RSS < 8 GB;
  `mp-vllm` untouched. `run.json` carries a non-empty git rev and the lock hash.
- Hermes posts the gen-0 board to Discord; `mp_factory_status` matches `summary.json`.

### Phase F2 — Evolution, campaigns, multiplicity, controls (days 5–14)
**Objective:** the full procedure end to end, deterministic and resumable, proven on nulls
and planted edge, yielding the first pooled OOS number — accepted whatever its sign.
**Deliverables:** FR-F2.1–F2.6; family #1 run with 41 control replicates; tests
`test_factory_evolve.py`, `test_factory_multiplicity.py`.
**Exit criteria:**
- Same master seed → byte-identical `gen_*.parquet`; `kill -9` at generation 17 then
  `resume` → the same final pick per campaign as the uninterrupted run.
- Planted-edge control recovers ≥80 % of the planted +5c/contract in pooled validation.
- Snapshot-efficient null: ≤1 of 20 replicates reports pooled `boot_lo > 0`; the picks'
  `p_RC` are not concentrated below 0.10 (KS vs uniform, p > 0.05). Residual-shuffle null:
  the real pick's rank among 20 is printed on the board.
- Family #1 report contains every field listed in FR-F2 of the roadmap (per-campaign picks,
  pooled 33-date OOS mean/se/t/CI, `p_RC`/SPA, Holm p, clustered DSR, N phenotypes,
  paired-vs-no-filter, 2c/3c/embargo-2 signs, `BSS_trades`, phenotype Jaccard) and states
  PROPOSED or CLOSED with the registry line updated.
- `factory.py report` recomputes `summary.json` byte-identically from ledger + frame.
- Full cycle < 4 h wall on the cpuset with `mp-vllm` serving; `mp-vllm` p50 latency
  change ≤10 %; `free -g` used ≤ 40 GiB.
- Discord receives ≥3 monitor posts during the run and one completion message; a
  `datetime.now()` call from `genome.py` inside a worker raises.

### Phase F3 — GenomeStrategy, live vintages, replay parity, gate plumbing (days 10–20)
**Objective:** make promotion real without touching a gate.
**Deliverables:** FR-F3.1–F3.4; tests `test_genome_strategy.py`, `test_factory_isolation.py`.
**Exit criteria:**
- Replay parity: 0 discrepancies between `GenomeStrategy`'s emitted set and the offline
  trade set over 1,656 markets for the six seeds and the family-#1 picks; live `p_yes`
  within 1e-9 of the frame's.
- `grep -nE 'datetime\.now|time\.time'` over `genome_strategy.py`, `features.py`,
  `genome.py` returns nothing.
- Every emitted signal has a tz-aware `expiration_time` at settlement-day close; a 24-h
  dev-box dry run settles its positions.
- On maia: EMIT lines at :00 UTC only, each with exactly one EXECUTED or REJECT line;
  `limit_price = quote + 0.01`.
- Risk/mixin/engine diffs empty except the CONTRA-3 log line and the `_load_state` backfill;
  the sandbox image builds without lightgbm/scipy/pyarrow and imports the strategy.
- `gate.py` on a synthetic 60-trade / 50-date journal reproduces a hand-computed binomial p
  and verdict; `measure_fill_realism.py` reports the 90th-percentile drift and, if > 1c,
  the registry records the raised `adverse_fill` and family #1 is re-scored (not re-searched).

### Phase F4 — Sealed holdouts, R3, promotion, paper run (after ratification)
**Objective:** score the proposed genome exactly once per virgin root, promote through git,
accumulate an admissible paper record toward FR-5.2.
**Deliverables:** FR-F4.1–F4.2.
**Exit criteria:**
- `unseal_log.jsonl` has exactly one holdout-B entry per family (≤3 finalists, Holm p); the
  search-frame builder still refuses the root; the holdout truth filter drops < 10 % of
  city-days after the Weather-Company re-reconcile.
- R3 computed once on `data/ladders_2026-09` with the result hash; PASS or HALT #3 in the
  registry; no second scoring of the same genome on the same root exists anywhere.
- Holm-adjusted p < 0.05 across all families on both holdout-B and Sept–Oct for the
  promoted genome.
- maia positions settle via `reconcile_weather.py` within 3 days; the PAPER board row shows
  settled `target_date`s and sandbox c/contract beside the prediction.
- After ≥50 settled `target_date`s, `gate_<id>.json` reports grouped count, exact binomial
  p, net PnL and spec-hash check; if HALT, the board shows the family KILLED and no file
  under `src/factory/` references a live-capital flag.

### Phase F5 — Second lanes and GENE_SPEC v2 (data-gated)
**Objective:** extend the same frame/kernel/registry to lanes and genes that lack
statistics today, without changing the fitness definition.
**Deliverables:** FR-F5.1–F5.2.
**Exit criteria:**
- Gas gen-0 reproduces `n_accepted = 198/376` for July with the gate injected and the
  weekly figure (351 trades, −1.78c, CI [−9.41, +5.85], 10 settlements); the board shows
  gas NOT_PROMOTABLE n=14; `factory.py run --lane gas` exits non-zero on the 40-unit assert.
- Mention truth JSONL grows ≥1 event/day with joinable quotes; the board shows NOT_READY
  with the count and an ETA.
- v1 genomes decode under v2 with new genes OFF and the F1 parity test passes; shuffling
  later rows of a market cannot change earlier rows' lagged features.
- Any maker-mode genome carries `fill_model=traversal_proxy` and cannot be PROPOSED until
  resting-fill rates exist for ≥30 city-days.

**Effort:** F0 ≈ 3 d, F1 ≈ 3–4 d, F2 ≈ 6–8 d, F3 ≈ 5–7 d, F4 ≈ 4 d over the data wait,
F5 data-gated. Roughly four engineer-weeks to a promotable-or-closed answer; the calendar
is set by dates of tape, not by compute.

## 9. Risks & Open Questions

**Risks**
1. **Statistical power.** 33 pooled validation dates and ~37 holdout dates cannot separate
   a real 3–5c edge from noise after correction. The honest expected outcome of family #1
   is **CLOSED**. The design treats that as a deliverable, and the Sept–Oct reserve plus
   winter tape are the remedy — not more searching on the 69 dates.
2. **Data retention.** The 07-26..08-31 ladders expire ~2026-10-03 and the M0 capture
   timer is scheduled nowhere (kill 09-15). F0 must run this week or the factory never has
   a true out-of-sample test before winter.
3. **Calibration skill.** The frame-level Brier skill of the walk-forward calibration vs
   the market mid is expected negative; genes can only select rows, not fix a
   mis-calibrated `p_yes`. GENE_SPEC v2 and a recalibration family are the escape, both
   registered as new families.
4. **Lab ≠ sandbox.** Kelly sizing, cooldowns and allocation differ from the 20-contract
   frame; mitigated by `sandbox_admissible` in the search frame, replay parity, and the
   weekly reconciliation with REJECT codes.
5. **Expressivity ceiling.** A 13-gene conjunction may only rediscover fr31a variants; on
   69 dates that is the point (un-memorisable). Accepted for v1.
6. **Forecast lag on maia.** The live `gfs_mex` vintage must come from the IEM MOS product
   at the same rule the frame used; a provider that silently serves a newer vintage
   recreates the lookahead. The `fetched_at` record makes the lag empirical.

**Owner decisions (defaults are encoded; confirm or override at ratification)**
1. `expiration_time` stamp location — default `attach_spec_to_signals`.
2. Disable the ML-Weather fallback — default `ML_WEATHER_ENABLED=False`.
3. Confidence semantics — default `confidence = p_win`, EV gate unchanged.
4. Whether `limit = quote + 1c` in the sim plus the fill-realism study satisfies FR-5.2
   "realistic fills".
5. Hot-load design — default: loop over `self.strategies`, env-selected genome, deploy via git.
6. FR-5.2 grouping unit (`target_date`), family cap (6), cutoff (2026-07-25), and the
   "looking" ruling — as encoded in the registry.
7. May a PROPOSED genome paper-trade on maia before the Sept–Oct R3 verdict? Default yes
   (it replaces an inadmissible shape, touches no capital, and the factory never reads
   maia's tape back until RATIFIED).
8. Unchanged from HANDOFF: prod KXBTCY fee receipt, Weather Company vs IEM reconcile
   policy, $3000→$350 sizing.
