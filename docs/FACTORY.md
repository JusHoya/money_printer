# FACTORY.md -- operating the strategy factory (F4 runbook)

Roadmap deliverable 3 of Phase F4 (`docs/factory/FACTORY_ROADMAP.md`;
`PRD_STRATEGY_FACTORY.md` FR-F4.2). This is the operator's page: what the
registry allows, what the controls are, how a lane is added, and the
promotion path end to end -- PROPOSED -> holdout-B unseal -> R3 score ->
promote -> register-gate -> commit -> maia deploy -> weekly reconcile -> gate
-- with the KILL path beside it. The design record is
`docs/factory/FACTORY_ARCHITECTURE.md`; the per-phase runbooks
(`F2_RUNBOOK.md`, `F3_RUNBOOK.md`) hold the mechanics this page only names.

Everything here runs on the dev box or on **alcyone** (the lab) except the two
maia steps, which are marked. Set `$env:PYTHONPATH = "."` on Windows.

---

## 1. Registry discipline

`reports/factory/registry.jsonl` (`src/factory/registry.py`) is the
pre-commitment record, and it is append-only: a family's thresholds are
written **before** any result exists, and every later event is an appended
line.

| Rule | What it means in practice |
|---|---|
| **Append-only** | Nothing is rewritten. A wrong line is corrected by a later line that says so; `git log -p reports/factory/registry.jsonl` is the audit. |
| **Family cap 6** (before 2027) | `family_cap: 6` on the family line (the family cap); the sixth open family blocks the seventh. Holm across registered families uses this count, so every family opened costs every other family power. |
| **`CLOSED` is terminal; so is `HALT`** | `registry.py:35` `TERMINAL = ("CLOSED", "HALT")`. A CLOSED family can never become PROPOSED. Family #1 (`weather/gfs_mex/taker/v1`) is CLOSED (2026-09-04); every spec under `configs/factory/promoted/` today is therefore **shadow**. |
| **A rerun is a new family** | Re-registering a CLOSED name is refused by design. Folding a fix into the frame (e.g. the cold-start sizing law) is `weather/gfs_mex/taker/v2`: new frame freeze, new search, new parity, new gate registration. |
| **Statuses** | `OPEN` -> `PROPOSED` (F2 verdict) -> `RATIFIED` (owner) -> paper -> gate `PASS` or `HALT`; or `CLOSED` at any point a verdict says "no". A documented "no" satisfies the exit criterion (HANDOFF section 3 rule 6). |
| **Grouping unit** | `target_date` (`grouping_unit` on the family line, FR-F3.4): every bracket on a city-day settles against one CLI high, and the gate counts units, not fills. |

`factory.py status` prints `reports/factory/latest.json`; `factory.py board`
renders the board (section 5 adds the PAPER row).

## 2. Controls

The F2 verdict is a **conjunction** (`report.py:evaluate_verdict`); the
controls are the part of it that asks "would a random search have done this?"
`factory.py controls <run_id>` runs them, resumably, after `factory.py run`:

| Control | Question | Where |
|---|---|---|
| `snapshot` (20 replicates) | selection under a label-preserving reshuffle -- how often does a null search clear `boot_lo > 0`? KS of `p_RC` vs the real run | `src/factory/controls.py` |
| `residual` (20) | the real pick's paired rank against searches on residualised outcomes | same |
| `planted` (1) | does the procedure recover a planted edge of known size (capture ratio)? | same |
| White RC / Hansen SPA over the phenotype ledger | multiplicity inside the family | `src/factory/multiplicity.py` |
| Holm across families | multiplicity across the registry | `report.py` `holm` |

`beats_every_control: false` was one of family #1's six failing conditions;
the `F2_RUNBOOK.md` walks the numbers.

## 3. Adding a lane

A lane is a market family with its own frame builder (`src/factory/lanes/`),
fee regime row (`configs/fees/fee_regime.csv`) and coverage entry
(`src/factory/coverage.py`). The board already lists `gas`, `mention`,
`tweets`, `crypto_annual` with their independent-unit counts and the search
floor (40 units); a lane is **NOT_PROMOTABLE** until its coverage clears the
floor -- that is data-gated (Phase F5), not a code gap. To add one:

1. `src/factory/lanes/<lane>.py`: a `build_frames(config)` that produces the
   parity/search frames with the shared column schema (`src/factory/columns.py`)
   and a provenance block with the calibration-dir hash.
2. A family YAML under `configs/factory/` (copy `weather_gfs_mex_taker_v1.yaml`;
   the sha256 of that file is the registry's `config_sha256`).
3. `coverage.py`: the lane's independent unit and how it is counted.
4. `factory.py freeze-frame --lane <lane>` -> `factory.py gen0 --frames ...`
   -> `factory.py run` -> `controls` -> `report` (writes the registry line).

## 4. The promotion path, end to end

Every step is a command whose refusals are the gate. Names are the PRD's
(FR-F4.1/F4.2); `holdout` and `score` are being implemented concurrently --
this page describes their **contract**, not their internals.

### 4.1 PROPOSED

`factory.py report <run_id>` appends the PROPOSED (or CLOSED) transition from
the F2 verdict. Nothing below is reachable for a CLOSED family.

### 4.2 Holdout-B unseal (once per family, <= 3 finalists)

```bash
python scripts/factory.py holdout --finalists --unseal RATIFIED-<date>
```

Scores the finalists **once** on `data/ladders_holdout` (2026-07-26..08-31),
Holm-adjusted, and appends to `reports/factory/unseal_log.jsonl`. Exactly one
entry per family; the search-frame builder must still refuse the root. Read
the seal caveat first: the root's `manifest.json` `result` labels and
`RECONCILE.md` are readable metadata, so an unseal by someone who has read
them is not clean (HANDOFF 2026-09-05 correction; owner decision 9).

### 4.3 R3 score (once per genome per root)

```bash
python scripts/factory.py score --genome <id> --ladders data/ladders_2026-09
```

Prints the **result sha256 before the numbers**, appends the R3 checks to
the registry as PASS or HALT #3. R3 tops out at 2026-09-01..09-15 (60
city-days) unless `MP_CAPTURE_KILL_DATE` is extended on purpose; 2026-08-31 is
quarantined out of it (HANDOFF item 7). No second scoring of the same genome on
the same root may exist anywhere.

### 4.4 Promote (shadow first, then paper)

```bash
python scripts/factory.py promote <id> --from-seed <name> --mode shadow     # or --from-pick RUN_ID:CAMPAIGN
```

Refuses: wrong id; a **maker** genome (never); a genome the runtime cannot
size (`src/factory/sizing.py`, `reports/factory/sizing_cold_start_2026-09-05.md`);
any replay-parity discrepancy. Writes `configs/factory/promoted/<id>.json`
with `spec_hash` over every field and `calibration.kind` = the provider parity
ran under. **Before paper**: the calibration-provider gap (F4 blocker 1) must
be closed -- `GenomeStrategy` refuses paper on a `kind` mismatch.

### 4.5 Register the gate, commit, stamp (FR-F4.2)

```bash
python scripts/factory.py register-gate <id>            # template + spec -> configs/factory/gate_registration.json
git add configs/factory/gate_registration.json && git commit -m "gate: register <id> (FR-F4.2)"
git log --diff-filter=A --format=%cI -- configs/factory/gate_registration.json
python scripts/factory.py register-gate --fill-commit-time   # fills registration_commit_utc from that command
git add configs/factory/gate_registration.json && git commit -m "gate: registration commit time"
```

The registration names the **paper** spec's hash (derived from the shadow spec
with `mode=paper` and the family's current status), so it can be committed
before the paper spec is written. `--fill-commit-time` refuses while the file
is untracked or carries uncommitted edits: the stamp is the commit that added
it, never a typed date. A registration is re-issued (`--force`, re-commit,
re-stamp), never edited.

### 4.6 Paper promote

```bash
python scripts/factory.py promote <id> --from-seed <name> --mode paper
```

Refuses unless the family is PROPOSED/RATIFIED **and** `gate_registration.json`
exists, names this genome / `Genome <id8>` / the spec's `adverse_fill`, and has
`registration_commit_utc` filled -- then, after parity, refuses if the spec it
is about to write hashes to anything but the registered `spec_hash`. Commit the
spec: that plus the registration is the **promotion commit**.

### 4.7 maia deploy (ON maia)

```bash
bash deploy/pi/deploy_f3_shadow.sh <id> --plan                      # side-effect free pre-flight
bash deploy/pi/deploy_f3_shadow.sh <id>                             # shadow, waits for a :00 boundary
GENOME_STRATEGY_MODE=paper docker compose -f deploy/pi/docker-compose.yml up -d   # paper: from the shell, only
```

`GENOME_STRATEGY_ID` in `/srv/money_printer/.env`; `GENOME_STRATEGY_MODE`
only from the invoking shell (compose `environment:` beats `env_file`,
F3_RUNBOOK section 3.2). `ML_WEATHER_ENABLED=False` is confirmed by the loaded line.
Land inside 120 s of a `:00` or the day's city-days are forfeited (section 3.1).
The bot's own registry check needs the `reports/factory` bind the compose file
now carries. Verify: `python scripts/check_maia_emit_cadence.py`, and
`GET /api/genome` shows `execution_mode`.

### 4.8 Settlement and the weekly reconcile

* maia: `mp-reconcile-weather.timer` (daily 13:30Z) cross-checks the sandbox's
  settlements against CLI truth. Evidence the criterion asks for --
  *positions settle within 3 days* -- comes from the journal, per strategy:

  ```bash
  python scripts/check_settlement_latency.py --url http://maia.local:8050
  ```

  (2026-09-06: PASS, 9/9 V2 + ML-Weather positions within 0.8 d; the genome
  line reads `NO EVIDENCE (0 settled; shadow books nothing)`.)

* alcyone: `mp-factory-reconcile.timer` (weekly, **Monday 14:30Z**, after the
  daily reconcile) runs `deploy/spark/factory_reconcile.sh` ->
  `scripts/factory_paper_reconcile.py --url http://maia.local:8050 --from <mon>
  --to <sun> --frames <the spec's frame>` inside the `lab` container. It is on
  alcyone because the lab trade set needs the **frozen search frame**
  (`data/factory/frames/*_<frame_search_sha256[:12]>`, gitignored, lab-only),
  and it reads maia over the read-only API (`/api/journal`, `/api/closed_trades`,
  `/api/logs/tail`). Install: `bash deploy/spark/install_factory_reconcile.sh`.
  A shadow week outside the frame is `REFUSED` (exit 3) -- not a pass; the
  Hermes watch `hermes_plugin/scripts/mp_factory_reconcile.sh` posts only on
  DISCREPANCY/REFUSED or an overdue week.

  **Frame caveat (2026-09-06):** the deployed spec `0c4b20502f2daf65` records
  `frame_search_sha256 bfcf94654a3a...`, the dev-box frame; alcyone holds
  `weather_2026-07-25_0fdf39ea506b` (the gen-0/F2 run's frame). The wrapper
  resolves the spec's frame by sha and **warns** when it must fall back. Copy
  the `bfcf` frame dir to alcyone (or re-promote on the lab frame) before the
  weekly report can claim the promoted spec's lab trade set.

### 4.9 The board

```bash
python scripts/factory.py board --paper-url http://maia.local:8050
python scripts/factory.py board --paper-state exchange_state.json --paper-journal trade_journal.jsonl --genome <id> --mode paper
```

The PAPER row: `<mode> k/n_min` (settled `target_date`s, the FR-5.2 unit),
sandbox c/contract from `closed_trades` (never equity; fees recomputed at the
taker rate when the ledger is unavailable and the row says so), the factory's
prediction for the same genome (pooled OOS for a pick, date-clustered
search-frame realized for a seed), and the honest note
`shadow run: 0 units (instrumentation, not gate evidence)` while nothing
settles.

### 4.10 The gate (after >= 50 settled `target_date`s of PAPER)

```bash
python scripts/gate.py --registration configs/factory/gate_registration.json \
    --journal <journal> --state <exchange_state.json> [--fill-config data/fill_config.jsonl]
python scripts/factory.py gate -- --journal ... --state ...     # same, --registration defaulted
```

Writes `reports/factory/gate_<genome_id>.json` by default (`--out` still
wins) carrying `grouped_count`, `p_exact`, `net_pnl`, `spec_hash_unchanged`
at the top level beside the full `conditions`. Below `n_min` it **refuses**
(exit 3) and still writes the file with `refused: true`. Realistic fills must
be evidenced by the run's own `fill_config.jsonl` (F3_RUNBOOK section 5) --
turning `MP_REALISTIC_FILLS` on is owner decision 4.

## 5. The KILL path

A gate `FAIL` (or an R3 `HALT #3`) is the family's verdict:

1. `factory.py report`/`score` appends the `HALT` transition (terminal).
2. `factory.py board` shows the PAPER row as **`KILLED:HALT`** (registry) or
   `KILLED:GATE_FAIL` (a scored, un-refused `gate_<id>.json` that reads FAIL).
3. ON maia: remove `GENOME_STRATEGY_ID` from `/srv/money_printer/.env` and
   `docker compose ... up -d`; the waterfall is exactly what it was.
4. `tests/test_factory_no_live_capital.py` keeps the criterion's last clause
   true on every commit: no file under `src/factory/` references a live-capital
   flag (`read_only`, `place_order`, `LiveKalshiGateway`, the `*_TRADING_ENABLED`
   module flags, the production host), and no factory module imports the
   exchange, the provider or the gateway.
5. HANDOFF.md gets the verdict entry either way -- skeleton in
   `reports/factory/HANDOFF_F4_draft.md`.

## 6. Standing owner decisions (PRD_STRATEGY_FACTORY section 9)

| # | Decision | Standing |
|---|---|---|
| 7 | May a PROPOSED genome paper-trade before the R3 verdict? | Default **yes** (replaces an inadmissible shape, touches no capital; the factory never reads maia's tape back until RATIFIED). |
| 9 | Spend a holdout-B unseal on a CLOSED family's seed genome? | **No default, no clock.** The ~2026-10-03 date is a backfill horizon (done 2026-09-02), not a deadline; the seal leaks its outcome labels. Recommendation on record: hold the unseal for a v2 finalist; close family #1 under section 3 rule 6. |
| 10 | Relieve the cold-start sizing ceiling by editing `src/core/risk_manager.py`? | **REJECTED (2026-09-05)**, do not reopen without new evidence. The fix is the `.../v2` family that folds the sizing law into `sandbox_admissible`; budget for it returning CLOSED. |

Also standing: decision 4 (realistic fills), 6 (unit `target_date`, family
cap 6, cutoff 2026-07-25). Full list and the registered deviations: the PRD's
section 9 and Phase F4.

## 7. Quick reference

| Need | Command |
|---|---|
| board with the live PAPER row | `python scripts/factory.py board --paper-url http://maia.local:8050` |
| settle-within-3-days evidence | `python scripts/check_settlement_latency.py --url http://maia.local:8050` |
| register the gate | `python scripts/factory.py register-gate <id>` then commit, then `--fill-commit-time`, then commit |
| paper promote | `python scripts/factory.py promote <id> --from-seed <name> --mode paper` |
| weekly reconcile by hand (alcyone) | `bash deploy/spark/factory_reconcile.sh` (`MP_RECONCILE_DRY_RUN=1` to plan) |
| gate verdict file | `python scripts/gate.py --registration configs/factory/gate_registration.json --journal ... --state ...` -> `reports/factory/gate_<id>.json` |
| live-capital grep | `python -m pytest tests/test_factory_no_live_capital.py -q` |
