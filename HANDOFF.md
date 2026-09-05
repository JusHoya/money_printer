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
factory's offline trade set exactly: replay parity over the 1,656 archived ladder markets
is 0 discrepancies for the six gen-0 seeds and the four family-#1 picks, with `p_yes`
bit-identical. Because family #1 is CLOSED, only **shadow** specs exist
(`configs/factory/promoted/*.json`): the bot logs the EMIT line, then rejects
`GENOME_SHADOW`; nothing reaches the exchange. `scripts/gate.py` (FR-5.2, exact
Poisson-binomial over `target_date` units), `scripts/factory_paper_reconcile.py`,
`scripts/measure_fill_realism.py` (two maia :00 boundaries: next-poll p90 drift 0.00,
`adverse_fill` stays 0.01) and the accelerated dry run `scripts/genome_dry_run.py` are in.
Runbook: `docs/factory/F3_RUNBOOK.md`; one-command maia deploy: `deploy/pi/deploy_f3_shadow.sh`.

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

Still open for F4: a daytime fill-realism collector (maia polls each market every ~35 s,
so the 20-s window is empty), the registration commit time must be filled before the
first paper trade, and a fresh deploy's first hour is the one missed-hour case the
strategy cannot prove (the weekly reconcile flags it).
