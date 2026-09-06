# HANDOFF — Money Printer, 2026-08-22

Written at the point the project moved off Google Cloud and onto a local 24/7 host.
If you are a new agent or a returning human picking this up cold, **read this file
before `PRD.md`, before `CLAUDE.md`, and before any code.** It exists so you do not
spend your first week rediscovering what four phases of work already settled.

---

## 1. Where this actually stands

Money Printer trades event contracts on Kalshi. It has never made money, and as of
today **it has no strategy cleared for trading.** That is not a failure state to be
fixed by trying harder on the same ideas — it is the accumulated output of a
deliberately honest evaluation process, and the process is the part worth keeping.

Three engines have been proposed and adjudicated:

| engine | verdict | date | why |
|---|---|---|---|
| Short-horizon crypto | **structurally unwinnable** | 2026-07-24 | Fee floor 2.25–4.5 pt exceeds the 2.1–2.8 pt signal ceiling. Arithmetic, not execution. Torn out in Phase 0. |
| Weather (`KXHIGH*`) — flagship | **HALT** | 2026-07-26 | The one positive shape reverses sign when the forecast source changes, and FR-2.4's own gate quantity ranks the losing source *higher*. |
| AAA gas convergence (`KXAAAGAS*`) — second engine | **HALT** | 2026-07-30 | Model Brier 0.1332 vs market mid 0.0775; the model was the better forecaster at **0 of 10** settlements. A $0.01 strike ladder against $0.14 of honest 14-day uncertainty. |

Two consecutive HALTs from the two flagship candidates. **`PRD.md` has no third
engine.** The roadmap runs Phase 0 → 5 and Phases 3 and 5 are both downstream of a
proceed decision that never came. Section 6 below is the open question this hands you.

A structural point worth internalising before you propose anything: both HALT
verdicts turned on the *same* failure mode, found independently. In each case the
modelled EV — the quantity the PRD authorises sizing from — was decisively wrong in
the optimistic direction, and in each case the market's own price was the better
forecaster. Weather: modelled EV ranked the money-losing source above the winning
one. Gas: modelled EV sat 7.9 standard errors above the realized mean. **A new
strategy that is evaluated on modelled EV will fail the same way.** Whatever you
build next, the acceptance evidence has to be realized, settlement-true outcomes
clustered on the correct independent unit.

### What the HALTs do *not* say

Read the two verdict documents rather than the summary table; both are careful about
their own limits and you will misread the project if you flatten them.

- Weather: *"It does not say the weather signal is worthless."* Under `gfs_mex` the
  shape realized +6.36¢/contract over 181 trades with a bootstrap CI excluding zero,
  and the sign held across four cities and three months. What the evidence cannot do
  is separate that from a source-selection artifact. `reports/phase2/phase2_go_no_go_2026-07-26.md`
  §9.3 **pre-registers exactly what would settle it** — that is your cheapest path
  back to weather if you want one.
- Gas: *"It does not say the strategy loses money"* — the realized 95% CI contains
  zero on 2 monthly and 10 weekly settlements. It says the gate quantity is measuring
  the wrong thing. It also does **not** say the projection is bad: held-out MAE is
  $0.0080/gal at 1-day lead, $0.0705 at 14 days, and the settlement rule reconciles
  644/644 against Kalshi's own results.

---

## 2. What is proven and worth keeping

The strategies failed. The infrastructure underneath them did not, and several
pieces are independently verified at a standard most trading code never reaches.

**Settlement semantics — the strongest asset.** The `strike_type` / `floor_strike` /
`cap_strike` → payoff rule replaced an inverted suffix-letter parser that had the
B/T direction backwards. The VM kept reconciling daily after the Phase 2 HALT, and
the snapshot captured the whole record: **112 city-days, 1,296 markets checked,
1,188 verified, 1,188 matched, 0 unexplained** (2026-07-25 → 2026-08-21). Every
non-match is the one explained category `NO_RESULT` — Kalshi closing a market without
publishing a result. Trust this layer.

- Caveat, stated because the reconcile report states it: the **sim leg is untested
  against live data** — *"No weather position has ever been opened."* Semantics are
  verified; the settlement path *through the simulator* is covered only by unit tests.
  The first thing any live-ish run must do is close that gap.

**Ground-truth pipeline.** `src/data/iem_cli_provider.py` + `scripts/backfill_weather_truth.py`
+ `scripts/reconcile_weather.py`. IEM CLI daily highs for KNYC/KMDW/KLAX/KMIA, the
correct settlement stations — an earlier build used KJFK/KORD and was silently wrong.

**Forecast + calibration stack.** `src/calibration/` (GEFS ensemble, GFS MEX guidance,
per-city bias/σ by lead time, probability engine), `src/data/ensemble_provider.py`,
`src/data/mos_guidance_provider.py`. Deterministic and byte-identical on re-run.

**Gas projection.** `src/data/aaa_provider.py`, `src/data/energy_covariates.py`,
`src/data/gas_settlement.py`, `scripts/gas_backtest.py`. The lag/drift projection is
genuinely accurate; it is the *ladder resolution* that defeats it.

**The evaluation harness — the real inheritance.** `scripts/go_no_go.py`,
`scripts/gas_backtest.py`, `src/backtest/`. These are the tools that produced two
HALTs instead of two hopeful greenlights. They cluster on the settlement event rather
than the trade, refuse to quote EV for a fill the recorded tape says was unavailable,
recompute rather than carry numbers between workstreams, and sweep the gate parameter
to check whether tightening it makes the *outcome* better or only the *model* happier.
Do not weaken any of that to get a green result.

**Harvested market tape.** 12.78 M weather rows across 174 days — see §4.

---

## 3. Operating discipline that produced these verdicts

These are house rules, learned expensively. They are why the project has no money but
also no illusions.

1. **Reconcile against external ground truth.** Internal books agreeing to the cent
   prove nothing. Every claimed outcome is re-settled against IEM CLI or Kalshi's
   published result. The 2026-06-10 review found that *all* sim profits to date were
   fiction produced by a settlement bug.
2. **Beat the trivial baseline.** Phase 2 §6.2 found a third of the apparent weather
   edge was a model-free warm-season tail-sell that the filter added nothing to.
3. **Cluster on the independent unit.** 351 weekly gas trades are 10 independent
   draws, because every bracket on a ladder settles against one AAA publication.
   Trade-level standard errors flatter the result and are printed only for reference.
4. **A gate that ranks a loser above a winner is not a gate.** This single check
   carried the Phase 2 HALT on its own.
5. **Sweep the filter threshold.** If tightening a divergence gate improves modelled
   EV while degrading realized PnL — monotonically, as gas did from 8→15→25 pt — the
   filter is selecting for the model's own error.
6. **A documented HALT satisfies the exit criterion.** Phases are allowed to conclude
   "no". This is written into the PRD and it is why the verdicts are trustworthy.
7. **Abort on missing critical input** rather than defaulting. A "safe" default is
   systematically wrong.
8. **No ML training in the runtime process** (FR-0.2). `src/ml/` and `scripts/train_*.py`
   are offline-only. Prior in-process retrains caused ~55-minute tick freezes.
9. **Register deferred evidence; never waive it silently.** If an exit criterion can't
   be met, record the deferral inline beside the criterion.

Longer-form versions of several of these live in the user's cross-project brain
(hypoCamp `wiki/insights/`) — `reconcile-against-external-ground-truth`,
`beat-the-trivial-baseline`, `backtest-before-deploy`,
`staged-deployment-gates-with-statistical-criteria`, `circular-constraints-justify-nothing`.

---

## 4. The data you inherit

The GCE instance was archived and stopped on 2026-08-22. Everything of value is in
`vm_snapshot_2026_08_22/` — **read its `RESTORE.md` to get the data back, and its `MANIFEST.md`**, which records provenance,
exclusions, and the checksum verification.

| asset | scale | where |
|---|---|---|
| Market tape (`data_*.csv`) | 12.78 M weather rows, 904 sessions, 174 days, 2026-01-27 → 2026-08-22, all 4 cities | `archive/market_csv.tgz` |
| Settlement truth | 27 daily reconcile runs, 112 city-days, 0 unexplained | **`reconcile_record/` — committed to git**, also in `archive/data_dir.tgz` |
| `data/` tree | calibration, forecast archive, ladders, models, journals | `archive/data_dir.tgz` |
| Production log | 75 MB, 24 days continuous, feed-only | `archive/prod_logs.tgz` |
| Named experiment runs | 54 labelled runs | `archive/named_runs.tgz` |
| Hermes agent config | config, skills, cron | `archive/hermes_config.tgz` |

Two things to know about the tape before you backtest on it:

- **August is the good data.** Monthly weather-row volume runs 35.7 k (Jan) → 5.66 M
  (Aug). The step change at 2026-07 is the Phase 0 harvester hardening (FR-0.6: full
  ladder depth, both bid *and* ask, all four cities). Pre-March rows came from the
  crypto-era harvester and predate the Phase 1 bracket-semantics fix.
- **The reconcile outputs exist nowhere else.** The VM cron was their only producer.
  Because they are small (892 KB) and irreplaceable, they are **committed** at
  `vm_snapshot_2026_08_22/reconcile_record/` rather than left inside the ignored
  archive. Do not restore them to `data/weather_truth/reconcile/` — that path stays
  untracked so a future harvester can write to it freely.

The 197 MB archive payload and the working `extracted/` tree are git-ignored. The
manifest, checksums, dedup file list, captured VM state (`meta/`), and the reconcile
record are tracked — so the repository alone carries the evidence and the provenance
even if the bulk archive is not copied to the new machine.

---

## 5. Migrating to the local 24/7 host

The VM is **TERMINATED**, not deleted — the boot disk is retained and the instance can
be restarted with `gcloud compute instances start money-printer-preschool-20260322
--zone=us-central1-c` if something turns out to be missing. Compute billing has
stopped; disk billing (~$1.20/mo for 29 GB) continues until the disk is deleted.

**Before deleting that disk, rotate the credentials.** The VM's `.env` held a live
`ANTHROPIC_API_KEY`, a Discord webhook URL, Kalshi read-only credentials, and
placeholder Coinbase keys. No secret value is in the snapshot, but the stopped disk
still has them in plaintext. Treat the Anthropic key and Discord webhook as exposed.

### Move the archive by hand — `git clone` will not bring it

`vm_snapshot_2026_08_22/archive/` is git-ignored. Cloning this repo onto the new
host gives you `MANIFEST.md`, the checksums, the captured VM state, and the
committed `reconcile_record/` — **but not the 197 MB of data they describe.** A
clone alone produces a repository that documents an archive it does not contain.

Copy `vm_snapshot_2026_08_22/archive/` across by whatever means suits (USB, rsync,
object storage), then verify it arrived intact rather than assuming it did:

```bash
cd vm_snapshot_2026_08_22 && sha256sum -c meta/archive_sha256.txt
```

The GCE instance is stopped, not deleted, so until that disk is removed the archive
can also be re-pulled from the source. Once the disk is gone, this copy is the only
one — the market tape in particular cannot be regenerated, because Kalshi prunes
settled markets from the public API after ~60 days.

### What to stand back up

The VM did exactly four things. Only the first three are worth reproducing:

1. **Harvester** — `python scripts/run_web_dashboard.py --auto-cycle --sim-balance 3000`,
   feed-only. This is what produced the 12.78 M-row tape.
2. **Daily weather reconcile** — `scripts/reconcile_weather.py --days 2` at 13:30 UTC.
3. **Daily settlement reconcile** — `scripts/settlement_reconcile.py` at 06:00 UTC.
4. **Watchdog** — `scripts/host_watchdog.sh` every 5 min. Keep the *function*; on a
   machine you can see, the Discord alerting matters less.

The exact crontab is in `vm_snapshot_2026_08_22/meta/vm_state_2026-08-22.txt`, along
with the VM's `pip freeze` and Python version. `docs/gcloud_vm_deploy.md` and
`docs/host_watchdog.md` describe the deployment that is being retired — useful as a
reference for what the new host must replicate, not as instructions to follow.

### Two migration decisions to make deliberately

- **Is continued harvesting worth it?** With both engines halted, the tape is
  accumulating against no active hypothesis. It is cheap and it is the input to any
  future backtest — but "keep harvesting" should be a decision, not a default. If you
  do keep it, note that ladder capture (`data/ladders/`) stopped at 2026-07-25 while
  the CSV tape continued; check whether you want both.
- **Timezone.** The VM ran UTC. Kalshi symbols are **ET**, and `parse_expiry()` must
  convert ET→UTC. A prior host-timezone mismatch silently produced 0 training samples
  for months. If the new host is not UTC, verify this end-to-end before trusting any
  derived data.

---

## 6. The open question this hands you

**`PRD.md` has no third engine, and that is the decision that has to be made before
any code is written.** Do not pick one unilaterally; this is the user's call. The
honest options, with what each costs:

1. **Settle the weather question.** Phase 2 §9.3 pre-registers exactly what would
   distinguish a real edge from a source-selection artifact. Highest information per
   unit effort, because the harness, the tape, and the truth pipeline already exist
   and the pre-registration was written before anyone knew the answer.
2. **Find a market where the ladder is coarse relative to the model's uncertainty.**
   This is the direct lesson of the gas HALT: the projection was accurate and still
   useless because $0.01 brackets cannot be resolved by $0.14 of honest uncertainty.
   That framing is a *screening criterion* for candidate markets and it is reusable.
3. **Stop trading and harvest.** Keep the feed running, bank the tape, and revisit when
   there is a hypothesis worth testing. Defensible given two HALTs.
4. **Retire the project.** Also defensible. It has produced substantial reusable
   engineering knowledge and no edge across seven months.

What would be a mistake: proposing a fourth strategy evaluated on modelled EV,
loosening the gate criteria to get a green result, or re-opening crypto. The
2026-07-24 review closed crypto on arithmetic that has not changed.

---

## 7. Repo orientation

```
PRD.md                     Drives all pivot work. Phases 0-5, FRs, exit criteria.
CLAUDE.md                  Architecture + Kalshi API reference. Read the API section.
HANDOFF.md                 This file.
vm_snapshot_2026_08_22/    Cloud archive + MANIFEST.md. Payload git-ignored.

reports/phase2/            Weather go/no-go (HALT) + 7 workstream reports.
reports/phase4/            Gas backtest (HALT) + quote tape.
review_2026_07_24/         The strategic-reset review that killed crypto.

src/calibration/           Forecast calibration, GEFS series, probability engine.
src/backtest/              Backtest engine, EV analysis, metrics, stress.
src/data/                  Providers: kalshi, iem_cli, ensemble, mos, aaa, energy.
src/core/                  interfaces.py (ABCs), risk_manager.py, matching_engine.py.
src/bots/                  registry.py registers ONLY `weather`, feed-only.

scripts/go_no_go.py        Phase 2 verdict generator.
scripts/gas_backtest.py    Phase 4 verdict generator.
scripts/reconcile_weather.py  Daily settlement truth. Ran on VM cron.
```

**Branch state.** `pivot_weather_v1` @ `9dcc78e` is the integration branch carrying
Phases 0–2 (105 commits ahead of `main`). `phase-4-gas-convergence` @ `72e4e3f` holds
the Phase 4 work and is **not merged**. `main` @ `5c00ef0` is stale since 2026-03-08
and does not represent the project. Everything is pushed. Merging Phase 4 into
`pivot_weather_v1`, and promoting that to `main`, is unfinished housekeeping.

**Kalshi API — the recurring trap.** V2 uses `_dollars` string fields
(`yes_bid_dollars`), not integer cents (`yes_bid`). The old names are gone from V2
responses. Always use `_parse_price()`. This has broken market data three times.
Production endpoint is `api.elections.kalshi.com`; `api.kalshi.co` is defunct.

**Tests.** Run targeted files (`python -m pytest tests/test_v3_risk_rules.py -v`);
the full suite is heavy on this machine. `tests/test_output_cooldown.txt` is binary
and must be `--ignore`d. If the suite reds with no code change, check for tests
pinned to absolute dates — five were fixed in `3760211` and two were time bombs set
for 2027-01-01.

---

## 8. Dated addenda (after 2026-08-22)

### 2026-09-02 — ML Weather taken out of the sandbox waterfall (PRD_STRATEGY_FACTORY FR-F0.2)

`src/bots/weather_bot.py` now carries `ML_WEATHER_ENABLED = False` (owner-only,
next to `WEATHER_TRADING_ENABLED`). With it off, `MLWeatherStrategy` is not
constructed and the tick goes straight to `WeatherArbitrageStrategyV2`
("Meteorologist V2"); with it on, the waterfall is byte-for-byte the old one.

**Why.** Every executed ML Weather signal on maia logged `confidence=1.000`.
`src/strategies/ml_weather.py:251` reads
`hrrr_forecast = extra.get("hrrr_forecast", nws_high or 0)` — no HRRR feed
exists in the live path, so the "second forecast" is the NWS high itself — and the
predictor's analytical fallback (`src/ml/predictor.py:598`,
`confidence = max(0.2, 1.0 - forecast_spread / 10.0)`) scores forecast
agreement. Spread 0 → confidence 1.0 → Kelly at its maximum on every signal →
the 50-contract hard cap every time; the implied forecast σ is 0.5F against a
measured day-of NWS σ of ~2.5F, and the resulting positions were never sized
down or exited. That is not evidence, so it is off until the model has a second
independent forecast to disagree with. Exit check: no `confidence=1.000` and no
`[Signal] EMIT strategy=ML Weather` lines in any maia log started after the deploy.

Two sibling fixes landed in the same commit and change how the tape and the log
should be read: `_ladder_for_city` tracks D-1/D/D+1 on the **ET** calendar
(FR-F0.3 — a UTC host was dropping the last 5–8 h of every city-day after
00:00Z), and `[Signal] EXECUTED ... qty=` / `check_order` now see the
50-contract post-cap quantity that `record_execution` books (FR-F0.4, log-only;
earlier logs overstate qty/cost by up to 75/50). Tests:
`tests/test_weather_bot_f0.py`.

### 2026-09-04 — Phase F2 verdict: strategy-factory family #1 is CLOSED (PRD_STRATEGY_FACTORY FR-F2)

The evolutionary factory ran end to end on alcyone (`reports/factory/run_2026-09-03b/`,
registry `weather/gfs_mex/taker/v1` → **CLOSED**). Four anchored campaigns plus the
blocked-5-fold diagnostic, 216,000 genome evaluations, then 41 control replicates
(20 snapshot-efficient nulls, 20 residual-shuffle nulls, 1 planted +5c edge); 1,217 s
wall on the 16-core cpuset with `mp-vllm` serving (p50 token latency +6 %), peak 29 GiB.
Two independent cycles are byte-identical (8,002 ledger/control files), and the same
picks and numbers reproduce on the Windows dev box under numpy 1.25 vs alcyone's 2.5.

**The number.** Pooled 33-date out-of-sample PnL of the pre-registered picker:
**+0.031 per contract, bootstrap CI [−0.090, +0.142], t = 0.51**, 29 dates traded,
49 trades. The picks sit on the 40-trade floor with in-sample lower bounds of
0.08–0.19 and validate at −0.037 / +0.035 / +0.109 (A/B/C): the search memorises
date luck, exactly the failure the settlement-true fitness was built to expose.
Feasible-set Reality Check p = 0.41 / 0.77 / 0.72 / 0.89, Holm p = 0.29, paired
difference vs the no-filter baseline −0.053 (lower bound −0.19). Failing promotion
conditions: pooled lower bound, Holm, p_RC(ALL69), beats-every-control, paired
baseline, and the 4c floor. The machine's own controls behaved: 0 of 20 snapshot
nulls reported a positive lower bound and the planted edge was recovered (capture
1.09, rule-level 1.07 — only 2 of the picks' 48 validation trades were flipped, so the
pick-level ratio is one-trade granular).

**What this does and does not say.** It says the 13-gene rule space over the frozen
May–July frame contains no shape that beats fees after correction — the outcome the
PRD named as the honest expectation (risk #1). It does not say the harvested data is
worthless: the blocked-5-fold diagnostic (in-sample blocks postdate the held block,
never headline) pooled +0.049 [+0.012, +0.085] over 64 dates, which is the usual
look-ahead-flavoured optimism and the reason walk-forward is the headline. **Nothing
from the factory is cleared for paper trading, let alone capital**; maia keeps running
V2 only.

**Two method amendments, ratified 2026-09-04** (`docs/factory/FACTORY_ARCHITECTURE.md`
§6.4a): (1) `p_RC` is computed on the picker's feasible competition set (≥ 0.6·D dates,
≥ 40 trades) with the all-phenotype value reported beside it — over every ledger row the
max is owned by 2–5-date kills and every pick scores p ≈ 1, zero power; (2) the
residual-shuffle null is **not** a no-edge null: late-window quotes already embed the
observed high, so under shuffled truth the market is confidently wrong and null picks
earn +0.60–0.88/contract (real pick rank 21/20 by construction). It stays a diagnostic;
a pre-observation-window or joint-shift variant is an F4 design item. Two F2 exit
criteria therefore fail on the letter (snapshot KS p = 0.001 because null p_RC skew
*high* — 3.3 % below 0.10; residual rank) and are accepted as documented.

Read next: `PRD_STRATEGY_FACTORY.md` §8 (F3 starts on the six seeds plus these CLOSED
picks for replay parity); `docs/factory/F2_RUNBOOK.md` for re-summarising a run
without re-searching; a second family needs new data (sealed holdout-B, the Sept–Oct
R3 reserve), never a new seed on the same 69 dates.

### 2026-09-05 — Phase F3 shipped; the NO-side settlement sign bug (ratified engine change)

`GenomeStrategy` (`src/strategies/genome_strategy.py`) now exists and reproduces the
factory's offline trade set: replay parity over the 1,656 archived ladder markets is
0 discrepancies for the ten **taker** genomes under test — six of the seven gen-0 seeds
plus the four family-#1 picks — with `p_yes` bit-identical. Two things that number does
not say, both registered inline in `PRD_STRATEGY_FACTORY.md` section 8. The seventh seed
`salvage_5f` is the maker diagnostic and carries all 148 discrepancies the report
actually contains (`ok_strict` is **false**; the quoted 0 is `discrepancies_gating`) —
a permanent exclusion, because maker executability folds fill flags no live path can know
at decision time, which is also why `factory.py` refuses to promote a maker genome at all.
And parity was measured with the *frame's* walk-forward calibration, not the
`FrozenCalibrationProvider` that `weather_bot.py:280` builds: substituting the real
provider gives **60 discrepancies** for the deployed genome. That is an F4 blocker, not
an F3 one, but it is the reason the parity claim must not be repeated unqualified.

Because family #1 is CLOSED, only **shadow** specs exist
(`configs/factory/promoted/*.json`): the bot logs the EMIT line, then rejects
`GENOME_SHADOW`; nothing reaches the exchange. `scripts/gate.py` (FR-5.2, exact
Poisson-binomial over `target_date` units), `scripts/factory_paper_reconcile.py`,
`scripts/measure_fill_realism.py` (the 02:00Z and 03:00Z maia boundaries: next-poll
p90 drift 0.00, `adverse_fill` stays 0.01 — see the coverage gap below) and the
accelerated dry run `scripts/genome_dry_run.py` are in. Runbook:
`docs/factory/F3_RUNBOOK.md`; one-command maia deploy: `deploy/pi/deploy_f3_shadow.sh`.

**The bug.** The dry run found that `SimulatedExchange._close_position` booked the YES-leg
payoff (1.00/0.00) against NO entries at binary settlement, so **every settled NO paper
trade since 2026-09-01 had its sign flipped** (a winning BUY NO at 0.33 closed at 0.00 and
booked −0.33/contract; a losing NO at 0.60 booked +0.40). Mark-to-market already inverted
correctly, which is why the equity curve and the closed-trade ledger disagreed. Commit
724d93c inverts the payoff for NO positions at binary settlement (unresolved flat closes
excluded); it is the one change to a protected file, ratified by the owner on 2026-09-05,
and `tests/test_protected_files.py` allow-lists exactly that hunk. The fix only corrects
future settlements: maia's `exchange_state.json` and `trade_journal.jsonl` must be repaired
with `scripts/repair_no_settlement_pnl.py --apply` (the deploy script does it with the
sandbox stopped and `.bak-n` backups), and `gate.py` refuses to run over unrepaired rows.
Read the F0 paper record accordingly: positions did settle, but their booked NO-side PnL
was wrong until the repair.

**It emitted — the maia exit criterion is MET.** Genome `0c4b20502f2daf65`
(`fr31a_taker`, mode=shadow) has run on maia since 2026-09-05T03:49:28Z and produced its
first EMIT lines at **15:00:23Z and 15:00:33Z**: four signals on
`KXHIGHLAX-26SEP06-T76`, `KXHIGHLAX-26SEP06-B78.5`, `KXHIGHLAX-26SEP06-B76.5` and
`KXHIGHMIA-26SEP06-B88.5`, each followed by exactly one `GENOME_SHADOW` reject, each with
`limit = quote + 0.01`, `verified_ok 4`, `emit_executed []` — nothing reached the
exchange. At 18:00Z those same four markets returned `GENOME_ALREADY_TRADED`, so
`entries_per_market: 1` survives a state reload. (The log-tail endpoint is capped at 500
lines, an 8-16 minute window, so these lines are no longer retrievable from it.)

**Correction to this entry as first written.** The fresh-deploy case does not cost "the
first hour". A late first tick marks every *visible city-day* for that city missed, not
one hour, and the bot walks all four cities — so it costs a whole market-day across
NY/CHI/LAX/MIA, and the marks persist in `state_dict()` for `STATE_KEEP_DAYS = 7` days
before `_prune` drops them. The weekly reconcile flags it; it is a lost day of tape, not
a lost tick.

**A promotion defect, quantified and decided: the genome's own signals size to zero at
cold start.** `RiskManager.calculate_kelly_size` blends recent win rate with signal
confidence as `p = 0.6 * wr + 0.4 * p_win`, and below `MIN_WIN_SAMPLES` = 20 closed trades
in the window it uses the neutral prior `wr = 0.50` — so a cold-start `p` cannot exceed
0.70 no matter how confident the signal. The fee `calculate_kelly_size` puts into `b` is
the **maker** fee, which on the KXHIGH standard schedule is exactly 0.0 for all 130 trades
(the genome is a taker; the sizing function reads the maker leg regardless), so
`f = p - q/b` reduces to positive **iff `p > price`** — the ceiling is a price cut, not a
confidence cut. Measured against the real sizer at confidence 0.99: 0.65 sizes, 0.70 does
not. Across the genome's 130 trades the blended `p` spans only [0.638, 0.700], so on the
1c grid price <= 0.63 is always admitted and price >= 0.71 never is. `fr31a_taker` buys NO
at a median quote of 0.83 (min 0.37, max 0.94) — median **price paid 0.84**, since the
signal prices at `quote + 1c`.
Replaying the genome's 130 offline trades through the real sizing arithmetic,
**117 of 130 (90 %) return 0 contracts with `KELLY_ZERO`**. Which 13 survive is
**identical at every bankroll stage from $100 (Seed) to $60,000 (Compound), including the
sandbox's actual ~$2,873**, because admission depends only on the sign of `f` and the
stage merely scales it. The QUANTITY does move with the balance (median 7 contracts at
$100, 38 at $500, at the `MAX_CONTRACTS = 50` cap from the live balance up). It cannot be funded away and there is no caller-side workaround —
reaching `p = 0.80` would need `confidence = 1.25`. Shadow mode hides it:
`GENOME_SHADOW` rejects before sizing is ever reached.

**This is a defect in the promotion, not in the RiskManager.** The factory searched a space
the runtime cannot trade in: `src/factory/columns.py` folds the mixin's EV gate into the
frame as `sandbox_admissible` (which passes 130/130 and never binds) but models the gate
that *does* bind — `calculate_kelly_size > 0` — not at all. Do not read this entry as a
case for loosening the sizing gauntlet.

**Owner decision, taken 2026-09-05.** Every option that moves the number by editing
`src/core/risk_manager.py` is **REJECTED**: seeding the win record from the genome's own
in-sample outcomes (it lets a modelled quantity license position size, inverting FR-0.6),
and lowering `MIN_WIN_SAMPLES` (non-monotone, and at 1 it is an absorbing state — the
first admissible trade lost, so the ceiling falls to 0.40 and never recovers; ~1 run in 5
to 1 in 8 stalls this way even at 20). Sizing from `p_win` directly is **held as a general
sizing-policy question**, to be decided on its own merits and applied to every genome if it
is adopted — never introduced to unblock a specific genome, which would be post-hoc
parameter selection wearing a research citation. Full option analysis, with the arithmetic:
`reports/factory/sizing_cold_start_2026-09-05.md`. Do not restate it here; read it.

**The shadow run is instrumentation, not a gate run.** What it IS producing is the
`KELLY_ZERO`/`GENOME_SHADOW` evidence that the sandbox will not size the promoted shape —
and that evidence is already complete, confirmed live: all four 2026-09-05T15:00:23Z EMITs
(prices 0.74–0.92, `p_win` 0.949–0.999, blended `p` 0.679–0.700) size to **0 contracts**,
and `GET /api/win_rates` shows no win record for the genome strategy at all. What it is
**NOT** producing is progress toward the FR-5.2 gate's ≥50 settled `target_date` units: it
books nothing, so it accrues zero units. Leave it running — it costs nothing — but nothing
in the record should describe it as accruing gate evidence.

**What the shape is actually worth, and how little gate margin it has.** The genome's
realized edge on the search frame is **+0.0750/contract per-trade** and **+0.0723
date-clustered** (§3 rule 3 prefers the clustered figure). An earlier draft quoted
+0.0705 as the full-set figure; that number is the **rejected** subset — the 117 trades the
runtime cannot size — not the full set. And under the corrected gate null the full genome
clears `n = 50` in-sample at **p = 0.047** against alpha 0.05, with a unit-win-rate CI of
**[0.667, 0.875]** against a null of **0.683** — i.e. the in-sample unit win rate is not
significantly above breakeven, on the data the genome was selected on. Any out-of-sample
shrinkage at all and it fails. Set beside the F2 verdict (family CLOSED, six of twelve
conditions failed: pooled OOS **+0.0308**, boot **[−0.0900, +0.1417]**, `p_RC` **0.886**,
Holm `p_adj` **0.2895**, `beats_every_control: false`), the prior on this genome is
"no edge", and spending governance capital to accelerate evidence collection on it is a
poor trade at any speed.

**Time to the gate at the measured rate: 287 days** (point estimate; 95 % band
**208–367 days**), from 12 sizable units over the frame's 69 days. That is mid-2027.

**Correction (2026-09-05, later the same day): there is no holdout-B deadline, and the
seal does not protect what everyone assumed it protects.** This paragraph originally read
"the holdout-B deadline is the one decision here with a hard external clock". Both halves
of that were wrong, and the second one matters more.

*There is no clock.* The ~2026-10-03 date governs **pulling** the ladders out of Kalshi
(~60-day API retention), not using ones already on disk — `FACTORY_ARCHITECTURE.md:193`
states it correctly as "must be **backfilled** before ~2026-10-03 retention expiry". The
backfill completed **2026-09-02** (`git log -1 -- data/ladders_holdout` → 9a8ed2e;
`manifest.json` `generated_at_utc 2026-09-02T21:31:14Z`, 1040 requests, 0 http failures),
and `sha256sum -c SHA256SUMS` returns **151 OK / 0 failed** today. What lapses in October
is only the option to **re-pull or repair** the root if it is ever found defective — an
insurance question, not a decision deadline. `kalshi_history.py:128` calls the retention
floor "Advisory only"; treat it as "sometime in October".

*The seal leaks its outcome labels.* The price seal is real — `load_ladders` and
`ev.load_search_ladders` both raise `SealedDataError` on the root, and
`tests/test_sealed_roots.py` is 20/20 green. But the **settlement labels are public**, in
two files the seal protocol itself lists as readable metadata:

- `data/ladders_holdout/manifest.json` carries `days[148] -> market_detail[6]`, each entry
  `{market_ticker, strike_type, floor_strike, cap_strike, result}` — **888 outcome labels
  with their bracket bounds**, i.e. every market in the root, from which each city-day's
  settled high is directly reconstructable. (Verified by reading KEY NAMES and counts only.)
- `data/ladders_holdout/RECONCILE.md` holds an 18-row dated table of settled
  CLI-high / Kalshi-`expiration_value` pairs across all four cities — **72 of 148 city-days**.

And the container mask is defeated by git: `deploy/spark/docker-compose.lab.yml:114` mounts
`../..:/app:ro` — the whole checkout including `.git` — then masks the sealed path with
tmpfs, but the 152 holdout CSVs are **tracked**, so `git show HEAD:data/ladders_holdout/...`
returns the sealed rows from the object store regardless. The compose comment "SEALED ROOTS
MUST NOT BE VISIBLE" is not true as written for the `factory` service.

**What this does and does not invalidate.** Family #1's F2 search is **not** retroactively
contaminated: the frame gate demonstrably kept those rows out of the search frame, and the
F2 run predates any of this. The exposure is **forward-looking** — any `.../v2` search
designed by a human or agent who has read those two files is no longer cleanly
out-of-sample on holdout-B, and "unsealed once, under a recorded unseal" protects roughly
half the information it appears to. If the root is ever scored, this leak must be recorded
alongside the result. Before the next search: decide whether to strip `result` from
`manifest.json` and the settled columns from `RECONCILE.md` (keeping them in a
separately-sealed sidecar), and whether the lab container should mount a `.git`-less export.

*How the wrong claim got here:* it was authored in `3b33b719` on **2026-09-05**, three days
after the backfill was already committed (`git merge-base --is-ancestor 9a8ed2e 3b33b71` →
yes), so it was a live authorial position rather than stale pre-F0 text. It is corrected
here rather than silently edited.

**The identified fix is a v2 family, and it is a separate phase.** Fold the runtime's
sizing law — the closed-form, state-free predicate `0.6*0.50 + 0.4*p_win > price_paid +
fee(price_paid)` — into the frame's `sandbox_admissible`, and register
`weather/gfs_mex/taker/v2` (the registry refuses to re-register a CLOSED family name by
design; a rerun is a new family). Then everything promoted is executable from trade 1 and
the pre-registration becomes true rather than worked around. Honest cost: it removes
**~40 %** of the executable NO-taker rows (830 -> 500 first-entry markets on this
frame), and the surviving universe realizes about
**−0.048/contract** unfiltered on this frame — so **budget for it returning CLOSED**, which
a documented "no" satisfies (§3 rule 6). This branch does **not** do it: it needs a new
frame freeze (new frame sha), a fresh search, a new parity run and a new gate registration.
The frame schema and `executable`/`sandbox_admissible` semantics stay exactly as they are
here, because the existing frames, the promoted specs' `frame_search_sha256` and FR-F3.4's
`n_discrepancies: 0` all depend on them.

**The F2 search is exactly reproducible — verified 2026-09-06, and never recorded before.**
An untracked `reports/factory/f2local/` turned out to be a full local re-run of the
canonical family run `run_2026-09-03b`: 60 generations, 96,000 evaluations, on a
different machine (x86-64 Windows vs alcyone aarch64) and a different commit
(`4a8b27be` vs `fb918982+dirty`). Every number matches to the last digit — verdict
CLOSED, `holm_p` 0.2895, `best_fit` 0.11143352272727274, pooled OOS mean 0.03083620689655171
over 29 dates / 49 trades — and **all four picked genomes are byte-identical**
(A `7d857b00d373`, B `7f1bc9234830`, C `03a5966189d0`, ALL69 `4b5acfa1055e`). A 96k-evaluation
stochastic search landing on the same four genomes across two architectures is the
strongest statement available that the factory's seeding and evaluation are deterministic,
which is what lets a verdict be re-derived rather than trusted.
The directory itself is now gitignored. It is a determinism check, **not** a second scoring
of family #1 — but a folder in `reports/factory/` that looks like an independent run is
precisely the ambiguity FR-F4's *"no second scoring of the same genome on the same root
exists anywhere"* cannot afford, so it stays out of the record by name.

Still open for F4, completely:
1. **Calibration-provider transfer gap (blocker — still open, but no longer invisible).**
   Parity proven under walk-forward, bot runs frozen; 60 discrepancies and `p_yes` off by
   up to 0.336 when the real provider is substituted.
   **2026-09-06:** the swap is now *detectable and refused*, and the number is evidenced.
   `factory_replay_parity.py --calibration frozen` reproduces it as a committed artifact
   (`reports/factory/replay_parity_bfcf94654a3a_frozen.json`, 60 disc / 0.3357) against a
   same-command walk-forward control (`..._frozen_control.json`, 0 / 0.0). The spec's
   calibration block gained `kind` (hash-covered; the six committed specs backfilled to
   `walk_forward`, verified byte-identical against a real re-promote), `factory.py promote`
   stamps it from the run that authorised the spec, and `GenomeStrategy`'s guard **refuses
   paper mode** on a mismatch or on a spec that names no kind, while shadow logs
   `CALIBRATION PROVIDER MISMATCH` and continues.
   **The 0.336 itself is untouched.** Serving walk-forward payloads live, or re-establishing
   parity under the frozen provider, is still owed and is still what unblocks paper. Do not
   read the guard as the blocker being lifted: it is the tripwire, not the fix.
2. **Fill realism measures the wrong hours.** Both runs cover 02Z/03Z; the genome's 130
   offline trades contain none there (15Z 34.6 %, 04Z 27.7 %, 16Z 16.2 %). The declared
   20-s primary window has n=0 in both runs — maia's per-market cadence is p50 35 s — so
   `adverse_fill = 0.01` is assumed, not measured. A daytime collector is owed.
3. **The maker gate is orphaned.** F3 replaced `measure_fill_realism.py` wholesale, and
   the new script reports no resting-fill rate of any kind, so the F5 condition that was
   meant to unblock maker genomes is un-instrumented (see `FACTORY_ROADMAP.md` F5).
4. **`gate_registration.json`** — the registration commit time must be filled in before
   the first paper trade, or `gate.py` has no pre-registered window to score.
5. **The fresh-deploy missed day** is the one missed-hour case the strategy cannot prove
   from its own state; the weekly reconcile is what catches it.
6. **The v2 re-search above.** The cold-start ceiling itself is no longer an open decision:
   editing `risk_manager.py` is rejected, and the fix is the `.../v2` family that folds the
   sizing law into `sandbox_admissible`. What is open is whether to spend that phase on a
   family whose prior is "no edge" — and, separately and with **no** clock on it (see the
   correction above), whether to spend a holdout-B unseal on a CLOSED family's seed genome.
7. **The R3 reserve overlaps sealed holdout-B by one date, and it already happened.**
   `deploy/spark/ladder_capture.sh` had a kill-date ceiling and **no floor**, so with the
   default `LOOKBACK=2` the first run on 2026-09-01 targeted 2026-08-30..08-31 and pulled
   **2026-08-31 into the R3 root**. Confirmed on alcyone:
   `data/ladders_2026-09/<series>/2026-08-31.csv` exists for all four cities, i.e. **4 of
   the reserve's 20 city-days are duplicates of the holdout's last date**. F4 wants
   Holm-adjusted significance on holdout-B **and** on Sept–Oct; a date in both roots is not
   two independent draws. A floor guard now refuses any start date <= `MP_HOLDOUT_LAST_DATE`
   (default 2026-08-31), verified to fire on exactly the historical case. **The four
   already-captured files are still there — decide whether to drop them from the R3 root
   before it is scored.**
   **RESOLVED 2026-09-06 (owner decision): dropped.** And the overlap was worse than
   recorded — the four files are not just the same city-day, they are **byte-identical**
   across the two roots (sha256 matches on all four: CHI `53ef51e4…`, LAX `99e35835…`,
   MIA `69a8ae77…`, NY `b5f2bb46…`). They are **moved, not deleted**, to
   `data/quarantine/r3_holdout_b_overlap/` on alcyone (commit `0ec72a6`, local and
   unpushed like the capture commits) — deliberately outside any `ladders_*` root so
   neither a walk of the R3 tree nor a glob of `data/ladders_2026-09*` can pick them up.
   Nothing is lost: the same bytes are still scored once, on holdout-B.
   **R3 now starts 2026-09-01 — 5 dates × 4 cities as of today.**
   `data/ladders_2026-09/manifests/2026-09-01.json` is left untouched as the record of
   the run that made the mistake; the top-level `manifest.json` never named the date.
   **The guard was not on the machine it guards.** `MP_HOLDOUT_LAST_DATE` shipped in the
   repo on 2026-09-05, but alcyone's checkout was **59 commits behind** (at its own
   capture commit `8808cd9`), so every capture 09-01..09-05 ran the unguarded script. The
   checkout was rebased to the branch head on 2026-09-06 — its 5 local capture commits
   replayed cleanly, both ladder roots intact — and the guard is now live there.
   **Correction (same day): the capture IS scheduled, and healthy.** An earlier note here
   claimed nothing scheduled it. That was wrong — a malformed `grep` over a truncated
   `systemctl list-timers` head. `mp-ladder-capture.timer` is **enabled and active** on
   alcyone (`deploy/spark/systemd/`, installed by `install_ladder_capture.sh`), fires
   `OnCalendar=*-*-* 12:30:00 UTC`, and its last run on **2026-09-06T12:30:48Z** committed
   `8808cd9` cleanly. Verified after the rebase: `MP_CAPTURE_DRY_RUN=1` plans
   2026-09-04..09-05 and the floor guard refuses the exact historical case
   (`MP_CAPTURE_TARGET_DATE=2026-08-31` → start 08-30 → *"inside sealed holdout-B"*).
   **The real loose end is the kill date.** `ladder_capture.sh` refuses any target date
   after `MP_CAPTURE_KILL_DATE`, **default 2026-09-15** (FR-F0.5). The guard is on the
   target date, so the last capture fires the morning of **2026-09-16** for target 09-15
   and every run after that dies. With 08-31 quarantined, R3 therefore tops out at
   **2026-09-01..09-15 — 15 dates × 4 cities = 60 city-days** and then stops growing.
   F4 must either score R3 at that size or extend `MP_CAPTURE_KILL_DATE` deliberately;
   it will not notice on its own, because a refusal after the kill date looks exactly
   like a healthy timer whose service exits non-zero once a day.
9. **Two F4 preconditions found in the review but never registered here.** Both are
   structural and would break a `.../v2` run exactly as they break this one:
   - **The sandbox cannot authorize paper mode at all.** `weather_bot.py`'s
     `_registry_status()` reads `reports/factory/registry.jsonl`, and its own comment says
     that file is "tracked, shipped in the image". `.dockerignore:5` excludes `reports/`
     from the build context and no bind restores it, so inside `mp-sandbox` the path does
     not exist, the `except OSError: return None` branch always fires, and paper mode can
     never be authorized. It fails CLOSED, so nothing is unsafe — but the gate is
     decorative, and in F4 it will refuse for a reason that looks like a registry problem
     and is not.
   - **FR-5.2's realistic-fills condition is unsatisfiable by the runtime.** The gate
     requires it; `RiskManager` constructs `SimulatedExchange` without it and
     `_save_state` never serialises the flag, so it resolves to `None` and drops out of
     the gating list into `not_applicable` while the verdict still reads PASS. The gate on
     this branch now REFUSES rather than silently downgrading, which is the honest
     behaviour — but it means a registration that requires realistic fills cannot pass
     until the runtime can evidence them, and that fix is inside a protected file.

8. **The holdout's outcome labels are public** (`manifest.json` `market_detail[].result`,
   888 markets; `RECONCILE.md`, 72 city-days; and the git object store defeats the lab
   container's tmpfs mask). Family #1's search is unaffected; any FUTURE search designed by
   someone who has read those files is not cleanly out-of-sample on holdout-B. Strip or
   re-seal before the v2 search, and record the exposure beside any score.
   **Examined 2026-09-06. "Strip" is not available, and the entry understates where the
   labels are — but the seal itself is real and enforced.** Three corrections:
   - **The labels are in the CSVs, not just the metadata.** Every
     `data/ladders_holdout/<series>/<date>.csv` carries `result`, `expiration_value`,
     `cli_high`, `recomputed_yes_expval`, `recomputed_yes_cli`, `payoff_matches_kalshi`
     and `truth_agrees` **on every row**. That is correct for a scoring set — you cannot
     score without truth — but it means the exposure is the whole root, and stripping the
     two metadata files would not make the root blind.
   - **Stripping HEAD would be cosmetic.** `manifest.json` is tracked and was committed at
     `9a8ed2e`, so `git show 9a8ed2e:data/ladders_holdout/manifest.json` still yields all
     888 (740 `no` / 148 `yes`). Removing them now would require a history rewrite of a
     sealed data root, which costs more than it buys.
   - **`RECONCILE.md`'s worst line is not a list of labels, it is one sentence:** *"exactly
     one YES market per city-day"*. That single structural fact — 1 of 6 markets settles
     YES — is a stronger prior than any individual outcome, and no strip of a `result`
     column removes it.
   **What actually protects the root is `src/backtest/sealed_roots.py`, and it works.**
   Verified: `data/ladders_holdout` and `data/ladders_2026-09` are both refused, as is any
   subdirectory of either (`data/ladders_holdout/KXHIGHNY` → *"lies inside the sealed
   root"*), while `data/ladders` stays searchable. The marker mechanism also travels — a
   `SEALED` file is honoured on the root **or any ancestor**, so a sealed root that is
   copied or moved stays refused. The item-7 quarantine had moved four holdout-B city-days
   out from under the R3 root's seal, so a `SEALED` marker was added there and verified
   (alcyone `4de0ee1`).
   That marker is defence in depth, **not** the thing that was standing between those rows
   and a search frame — an earlier draft of this entry said they were "protected by
   nothing", which is wrong. There is a second, independent **content** gate:
   `assert_frame_not_sealed` refuses any frame carrying `target_date > 2026-07-25`
   regardless of where the rows came from or whether pandas kept the loader's `attrs`.
   Checked against the quarantined bytes directly — a bare `read_csv` drops `attrs` to `{}`
   and the gate still refuses, *"226 row(s) carry target_date > 2026-07-25"*. So the marker
   buys an earlier and clearer refusal at the loader, and covers a hypothetical reader that
   never builds a frame; the date gate was already covering the frame path, and
   `test_frame_gate_catches_a_copied_holdout_csv_without_marker` has pinned exactly that
   since F0.
   **So the residual risk is not a code path, it is a pair of eyes.** No guard stops a human
   or an agent from opening those CSVs while designing a search. The honest disposition is
   the second half of the original sentence — **record the exposure beside any score** — and
   F4 should treat "was this search designed by someone who had read the holdout?" as a
   question to answer in writing, not one the seal answers for it.
