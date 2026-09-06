# F3 runbook — sandbox image, maia shadow deploy, dry run, cadence checks

Phase F3 of `PRD_STRATEGY_FACTORY.md` (FR-F3.1–F3.4; `docs/factory/FACTORY_ROADMAP.md`
§F3; `docs/factory/FACTORY_ARCHITECTURE.md` §1.2 / §9). This is the INFRA half of the
sprint: how the promoted-genome slot is built into the arm64 sandbox image, proven on
the dev box, deployed to maia in **shadow mode**, and verified over HTTP only.

Facts this runbook does not re-litigate:

- **No genome is PROPOSED** — family #1 is CLOSED (`reports/factory/registry.jsonl`).
  F3 proves the plumbing on the six gen-0 seeds / F2 picks in **shadow mode**; nothing
  paper-trades a CLOSED genome. Paper mode is F4, after ratification.
- The dev box is Windows with no Docker; images build on **alcyone** (arm64, same
  arch as maia). maia has **no ssh** for the orchestrator; every verification below is
  a `GET` on `http://maia.local:8050` (LAN only, pleiades trust boundary F1).
- Protected files: `src/core/risk_manager.py`, `src/bots/mixins.py`,
  `src/core/matching_engine.py` — unchanged against `38d5fdd` except the ONE
  owner-ratified settlement hunk in `matching_engine.py` (§1.1), which
  `tests/test_protected_files.py` allow-lists. That test passing is the gate; the raw
  `git diff` is not empty and is not meant to be. Shadow handling lives in
  `weather_bot.py`.

## 0. Dev-box gates (run before anything leaves the box)

```powershell
# from the repo root
python -m pytest tests/test_genome_dry_run.py tests/test_factory_isolation.py `
    tests/test_protected_files.py tests/test_weather_lifecycle.py tests/test_v3_risk_rules.py `
    -q -p no:cacheprovider
```

- `tests/test_factory_isolation.py` — the runtime entry points' import graph reaches
  only `src.factory.{genome,features,promoted}` (+ their numpy-only deps `columns`,
  `fees`); `src.strategies.genome_strategy` imports with `lightgbm/scipy/pyarrow/torch/
  xgboost` blocked in `sys.modules` (these were conditionally xfailed while the module
  was missing; STRATEGY has landed it, so they are hard assertions now); no
  `datetime.now|time.time|utcnow|from datetime import` in `genome_strategy.py`,
  `features.py`, `genome.py`.
- `tests/test_protected_files.py` — the three protected files match `38d5fdd` in the
  working tree, except the one allow-listed settlement hunk in `matching_engine.py`
  (§1.1). This test passing — not an empty `git diff` — is the gate.
- `tests/test_genome_dry_run.py` — the accelerated 24-h dry run (§1) on a real
  city-day, with a real position opened and settled.

## 1. The accelerated 24-h dry run (`scripts/genome_dry_run.py`)

Drives the **real** `WeatherBot` → strategy waterfall → `_process_signals` →
`RiskManager` → `SimulatedExchange` chain through every hourly candle of one archived
city-day, then past the settlement close, publishes the CLI truth and asserts that
every position left the book through `_settle_weather_position`.

```powershell
python scripts/genome_dry_run.py --city NY --date 2026-07-20
# once STRATEGY has landed a promoted spec:
python scripts/genome_dry_run.py --city NY --date 2026-07-20 `
    --genome-spec configs/factory/promoted/<id>.json --genome-mode shadow
```

Output: `reports/factory/dry_run_<CITY>_<date>.json` (timestamp-free; a re-run is
byte-identical) and one `PASS/FAIL` line per assertion. Exit 0 = every assertion
held; 1 = an assertion failed (the report says which); 2 = the run could not be set up
(no ladder for that day, genome module not importable, …).

How the clock and the truth are injected (the docstring at the top of the script is
the authoritative list of seams):

| Seam | Mechanism |
|---|---|
| Clock | `DryRunClock` (settable, tz-aware). `install_clock` rebinds the `datetime` / `date` / `time` names in the seven runtime modules that read wall-clock (`weather_bot`, `mixins`, `weather_strategy`, `ml_weather`, `matching_engine`, `risk_manager`, `weather_settlement`) to fakes answering from the clock; `time.sleep` becomes a no-op (that is the acceleration). The fake `datetime` is a subclass with an `__instancecheck__` accepting real datetimes because the engine does `isinstance(v, datetime)`. If `WeatherBot` exposes the FR-F3.3 `clock=` hook (constructor kwarg or a `clock` attribute) it receives `clock.now_et` too. Everything is undone in `finally`. |
| Market data | `bot.kalshi` is `ReplayKalshi`: `fetch_market_ladder` returns the ladder rows at the clock's candle from `data/ladders/<SERIES>/<date>.csv` (via `ev_analysis.load_search_ladders`, sealed roots refused); `fetch_orderbook` returns `{}` (no archived depth on the candle grid). |
| Forecast | `bot.nws` is `ReplayNWS`: the GFS-MEX vintage usable at the candle (`ev_analysis.forecast_vintage_table` over `load_forecast_archive`, `init + lag ≤ ts`, `--availability-lag-min` default 240 like the frame) shaped as one NWS daytime period. |
| Observations | **Synthetic** — no hourly METAR archive exists on disk. `bot.metar` is `SyntheticObservations`: a diurnal curve from the CLI daily low/high/high_time (`data/weather_truth/cli_daily_high_<station>.csv`), running max on the station's local day. Feeds only V2's winner-guard/velocity branches, never settlement. |
| Truth | `weather_settlement.SETTLEMENT_CACHE_PATH` → a temp file; the IEM client → an offline stub (never network). Truth is published into the cache **after** the clock passes `settlement_close_for(symbol)`, so the candle landing on the close exercises the `SETTLEMENT_TRUTH_PENDING` hold-and-retry branch first (the report's `truth.iem_network_calls` counts those stub hits). |
| State | `risk_manager._DEFAULT_STATE_FILE` / `WIN_RATES_PATH` → a temp dir; production `exchange_state.json` / win rates are never read or written. |

Assertions in the report (`assertions.<name>.ok`):

- `every_signal_has_settlement_close_expiration` — every emitted signal (captured by
  wrapping each strategy's `analyze`, so shadow-mode signals count too) carries a
  tz-aware `expiration_time` equal to `weather_settlement.settlement_close_for(symbol)`.
- `every_position_settled_via_settle_weather_position` — every opened position id
  appears in `closed_trades` with `reason == "EXPIRATION"`, went through the (instance-
  wrapped) `_settle_weather_position`, and carries `settlement_high`.
- `no_position_remains_open`, `held_open_until_truth_published`.
- `fr04_every_emit_has_one_outcome` — every `[Signal] EMIT` line resolved to exactly one
  `EXECUTED` / `REJECT` line (FR-0.4).
- `settlement_pnl_matches_contract_side` — see §1.1.
- `at_least_one_position_opened` only with `--require-position`.

### 1.1 Finding: NO-side settlements were booked with the wrong sign (FIXED in 724d93c)

The first dry run (NY 2026-07-20) opened **BUY NO** on `KXHIGHNY-26JUL20-B79.5` at 0.33 x
50; the bracket settled `"no"` (high 81 vs 79-80), i.e. the NO contract **won**, and the
engine booked `exit_price = 0.00`, `pnl = -16.50` instead of `+33.50`. Every executed V2
signal across eleven scanned city-days was a NO position and every one was sign-flipped.

Cause: `SimulatedExchange._close_position` set `exit_price = 1.00 if outcome_is_yes else
0.00` -- a **YES-side** price -- and booked `(exit_price - entry_price) * qty` without
consulting `contract_side`, while the mark-to-market sweep does invert for NO.

**Fix (commit 724d93c, the one allowed hunk in `matching_engine.py`):** at binary
settlement a NO position's exit price is `1 - <YES payoff>`; the `SETTLEMENT_UNRESOLVED`
flat close is excluded. `tests/test_weather_settlement_semantics.py` covers a winning and a
losing NO; `tests/test_genome_dry_run.py::test_settlement_pnl_matches_contract_side` is a
hard assertion.

**What the owner must do on maia, in this order, before any gate or reconcile run:**

1. Confirm the running sandbox image includes 724d93c (`git log --oneline -1` in the
   checkout the image was built from; rebuild + `docker compose up -d --build` if not).
2. Dry run the repair on BOTH files:
   `python scripts/repair_no_settlement_pnl.py --state data/exchange_state.json --journal data/trade_journal.jsonl`
   (lists every stale NO-side settlement in `closed_trades` and in the journal; exit 1 while
   repairs are pending; nothing is written).
3. Apply it: the same command with `--apply` (writes `exchange_state.json.bak-N` and
   `trade_journal.jsonl.bak-N`, rewrites only the stale rows, marks them
   `repaired_no_side_settlement: true`, rebuilds the cumulative ledger).
4. Only then run `scripts/gate.py` / `scripts/factory_paper_reconcile.py`. The gate
   REFUSES (exit 3) while any unrepaired stale row remains, whether it sits in
   `closed_trades` or in a journal row whose state entry was cleared by a cycle reset.

The engine fix is a deliberate, owner-ratified deviation from the F3 "engine diff empty"
rule (`tests/test_protected_files.py` allow-lists exactly that hunk).

## 2. alcyone: build the arm64 sandbox image and run the in-image import checks

Same arch as maia, so an image that imports on alcyone imports on the Pi. From
`~/projects/money_printer` on alcyone (checkout fast-forwarded to the F3 merge):

```bash
docker build -f deploy/pi/Dockerfile -t money-printer-sandbox:f3 .

# 1. the strategy imports in the runtime image (no lightgbm/scipy/torch/xgboost installed)
docker run --rm --entrypoint python money-printer-sandbox:f3 \
    -c "import src.strategies.genome_strategy, src.factory.promoted; print('genome_strategy OK')"

# 2. the blocked-modules variant: pyarrow IS in the image (the harvester writes parquet),
#    so prove the strategy does not need it either
docker run --rm --entrypoint python money-printer-sandbox:f3 -c "
import sys
for m in ('lightgbm', 'scipy', 'pyarrow', 'torch', 'xgboost'):
    sys.modules[m] = None
import src.strategies.genome_strategy, src.factory.promoted
bad = sorted(k for k in sys.modules if k.startswith('src.factory') and sys.modules[k]
             and k.split('.')[-1] not in ('genome', 'features', 'promoted', 'columns', 'fees'))
assert not bad, bad
print('genome_strategy imports with lab libs blocked; factory modules:',
      sorted(k for k in sys.modules if k.startswith('src.factory') and sys.modules[k]))
"

# 3. the runtime entry points do not reach the lab side of src/factory
docker run --rm --entrypoint python money-printer-sandbox:f3 -c "
import sys, scripts.run_dashboard, scripts.run_web_dashboard
print(sorted(k for k in sys.modules if k.startswith('src.factory')))
"

# 4. the lab libraries are genuinely absent from the image
docker run --rm --entrypoint python money-printer-sandbox:f3 -c "
import importlib.util as u
print({m: u.find_spec(m) is not None for m in ('lightgbm', 'scipy', 'torch', 'xgboost', 'pyarrow')})
"   # expect lightgbm/scipy/torch/xgboost False, pyarrow True
```

Do not add anything to `deploy/pi/requirements-runtime.txt` for the genome slot unless
check 1 fails on a *missing* module; the design intent is numpy-only.

## 3. maia: shadow deploy of a promoted genome

**One command (2026-09-05):** `bash deploy/pi/deploy_f3_shadow.sh <genome_id>` on maia does every step below
plus the NO-side settlement repair of §1.1 with the sandbox stopped. Flags:

| Flag | Effect |
|---|---|
| `--no-repair` | skip the §1.1 NO-side state repair |
| `--any-time` | deploy off-boundary, explicitly accepting the forfeited market-day (§3.1) |
| `--at-boundary` | the default: wait for the `:00`-aligned launch window |
| `--plan` | print the pre-flight and exit. Touches nothing — no git, no sudo, no docker |
| `--max-wait N` | refuse rather than wait longer than `N` s for the boundary (default 3600) |
| `--repair-budget N` | how long step 6 takes on this host (default 240 s); it is subtracted from the wait |

Run `--plan` first; it is side-effect free and prints both the cost of deploying now and
the exact schedule the run will follow (how long it will sleep, and why).
The manual steps below stay as the reference.

### 3.1 Deploy on a `:00` boundary or lose the market-day

`GenomeStrategy` evaluates at the top of the hour with a **120 s** tolerance
(`DEFAULT_TOP_OF_HOUR_TOLERANCE_S`). A container whose first tick lands later than that
inside an hour records the hour as *missed*; at the next evaluated hour the missed-hour
rule (§7) closes **every visible city-day** with `GENOME_MISSED_HOUR` for the rest of the
market-day, and the persisted state remembers it for `STATE_KEEP_DAYS`. Nothing recovers it.

This is not hypothetical: the 2026-09-05T03:49:28Z deploy landed 49 minutes past a
boundary and cost all four cities that day — at 19:00Z the tape still read
`GENOME_MISSED_HOUR` x 24 for `target_date=2026-09-05`. In F4 every such restart is one
fewer of the **≥50 settled `target_date`s** the FR-5.2 gate needs, so an off-hour redeploy
directly delays the gate.

The script therefore **waits for the aligned window by default** and prints the cost up
front. Deploy just after a `:00` when you can; use `--any-time` only when you have read
the cost and accept it.

Two details the first version of this gate got wrong (fixed 2026-09-05):

- **The wait is budgeted for the work that follows it.** The repair (step 6 — `compose
  stop` plus two to four `compose run` container starts) happens *between* the wait and
  `up -d`. A wait sized as if only `up -d` followed could sleep most of an hour and still
  launch past the tolerance. The wait now ends `LAUNCH_LEAD_S + REPAIR_BUDGET_S` before a
  `:00`, and a short **capped** top-up runs immediately before `up -d`. The top-up is
  capped on purpose: the sandbox is stopped there, so sleeping to a boundary an hour away
  would take the other four bots down with it — the script launches late instead and says so.
- **"Aligned" means near the *nearest* `:00`, on either side.** Measuring only
  seconds-into-the-hour made everything past +45 s "LATE", so a deploy 50 s past the hour
  got an unrecoverable-cost warning it did not deserve, and the spot the wait itself parks
  in (75 s *before* a `:00`) was reported LATE after a *successful* wait.

The wait is announced (duration and target UTC time), chunked with a progress line every
five minutes, and safe to `Ctrl-C`: nothing has been stopped when it runs.

### 3.2 Which configuration layer actually sets the mode

`deploy/pi/docker-compose.yml` declares `GENOME_STRATEGY_MODE` under `environment:`, and
Compose resolves `environment:` **above** `env_file:`. So:

| Layer | Wins? | Notes |
|---|---|---|
| invoking shell (`GENOME_STRATEGY_MODE=paper docker compose ...`) | **yes** | how F4 flips to paper on purpose |
| compose `environment:` default `${GENOME_STRATEGY_MODE:-shadow}` | yes, absent a shell value | pins shadow |
| `/srv/money_printer/.env` (`env_file:`) | **never** | **dead config** — it can only mislead |

The deploy script no longer writes `GENOME_STRATEGY_MODE` to the env file, comments out any
line an earlier run left there, refuses to start when a non-shadow value is exported into
its own shell, and asserts the **value** from the container
(`docker exec … printenv GENOME_STRATEGY_MODE` must equal `shadow`, and the id must equal
the requested genome). The old check — `docker exec … env | grep -E 'GENOME_'` — was
satisfied by a lone `GENOME_STRATEGY_ID`, so it would have passed a `paper` deploy and then
printed "loaded in shadow mode".

### 3.3 The loaded / REFUSED line is NOT on container stdout

`src/utils/logger.py` attaches only a `FileHandler` and forces any console `StreamHandler`
to WARNING+, and the shared logger has `propagate=False`. **Nothing the bot logs reaches
container stdout**, so `docker compose logs … | grep 'GenomeStrategy'` is empty on a
correct deploy exactly as readily as on a broken one. Read the file log instead:

```bash
docker exec mp-sandbox sh -c 'grep GenomeStrategy $(ls -t /app/logs/money_printer_*.log | head -1) | tail -5'
```

Expect `[Weather] GenomeStrategy … loaded (genome_id=… mode=shadow …)`. A
`[Weather] GenomeStrategy REFUSED: …` line means the bot is running V2 only — the genome
was not loaded, and the reason is on that line. The deploy script now distinguishes the
three cases (loaded / REFUSED / no line at all) instead of `die`ing on all of them, and
**polls** for the line to `MP_LOADED_DEADLINE_S` (default 120 s) instead of reading once
after a blind `sleep 10` — on a Pi 4 the bot can write it well after `/healthz` answers,
and a single read turned a correct deploy into a reported failure.

Prerequisite: a promoted spec committed under `configs/factory/promoted/<id>.json`
(STRATEGY: `scripts/factory.py promote <id> --from-seed <name> --mode shadow`, which
refuses on any replay-parity discrepancy). In F3 that is a gen-0 seed or an F2 pick —
**shadow only**.

```bash
# ON maia, from ~/money_printer (deploy/README.md redeploy runbook)
git pull --ff-only

# forecast-vintage cache bind (compose refuses to write into a root-owned auto-created dir)
sudo mkdir -p /srv/money_printer/data/forecast_cache
sudo chown 1000:1000 /srv/money_printer/data/forecast_cache

# runtime env (env_file) -- the ID only. Do NOT put GENOME_STRATEGY_MODE here: compose's
# `environment:` beats `env_file:`, so a line here can never change the mode (§3.2).
sudo tee -a /srv/money_printer/.env >/dev/null <<'EOF'
GENOME_STRATEGY_ID=<seed id>
EOF

# land this inside 120 s of a :00 UTC boundary, or today's city-days are forfeited (§3.1)
docker compose -f deploy/pi/docker-compose.yml up -d --build
curl -s http://localhost:8050/healthz            # {"status":"ok","uptime_s":...}
docker exec mp-sandbox printenv GENOME_STRATEGY_MODE   # must print exactly: shadow
docker exec mp-sandbox printenv GENOME_STRATEGY_ID     # must print the requested genome id
docker exec mp-sandbox sh -c 'grep GenomeStrategy $(ls -t /app/logs/money_printer_*.log | head -1) | tail -5'
```

Env plumbing (what carries what):

| Variable | Carried by | Default in the container |
|---|---|---|
| `GENOME_STRATEGY_ID` | `/srv/money_printer/.env` via `env_file` | unset → no genome slot |
| `GENOME_STRATEGY_MODE` | compose `environment:` `${GENOME_STRATEGY_MODE:-shadow}` — the **invoking shell** wins, and this entry overrides the env_file value, which is dead config (§3.2) | `shadow` |
| `MP_FORECAST_CACHE_DIR` | compose `environment:` | `/app/data/forecast_cache` (bind → `/srv/money_printer/data/forecast_cache`) |

To roll back: remove `GENOME_STRATEGY_ID` from `.env` and `docker compose ... up -d`;
the waterfall is exactly what it was.

## 4. Verify the maia criterion over HTTP (`scripts/check_maia_emit_cadence.py`)

Criterion (PRD F3): `GenomeStrategy` EMIT lines appear at **:00 UTC only**, each with
**exactly one** `EXECUTED` or `REJECT` line — in shadow mode that line is
`[Risk] REJECT ... reason=GENOME_SHADOW` — and `limit_price = quote + 0.01`.

```powershell
# from any LAN host, stdlib only. --url is the BASE url; the client appends the path.
python scripts/check_maia_emit_cadence.py                         # http://maia.local:8050
python scripts/check_maia_emit_cadence.py --url http://maia.local:8050 --json
python scripts/check_maia_emit_cadence.py --file money_printer_<stamp>.log   # a downloaded copy
```

`--url http://maia.local:8050/api/logs/tail` is **wrong** — the client appends
`/api/logs/tail` itself, so that requests the path twice and 404s. It now reports a
readable error and exit 2 instead of a traceback.

What it reads: `GET /api/logs/tail?pattern=money_printer_*.log&lines=500` (the newest log;
container `TZ=UTC`, so the log stamps are UTC) and `GET /api/logs/data` (last 100 data-log
rows) for the quote. Verdict JSON fields: `n_emit`, `no_emit_reason`, `boundaries_sampled`,
`n_strategy_lines`, `emit_off_hour` (minute ≠ 0), `emit_without_outcome`,
`emit_multiple_outcomes`, `emit_executed`, `outcome_codes` (expect
`{"GENOME_SHADOW": n_emit}` in shadow), `strategy_skip_codes`, `limit_price`
(`verified_ok` / `verified_bad` / `unverified`, with examples).

Exit codes: **0** PASS, **1** FAIL, **2** the check could not be run (bad URL, HTTP error,
unreadable file), **3** `NO_EMIT` or `UNVERIFIED`.

**In shadow, an `EXECUTED` outcome is a FAIL.** An EMIT resolved by `[Signal] EXECUTED`
satisfies "exactly one outcome line", so the verdict used to call it a PASS — even though
nothing may reach the exchange in shadow, which is the whole claim under audit. It is now a
hard FAIL; `--allow-executed` lifts it for F4 paper runs, where EXECUTED is expected.

**Observed on 2026-09-05 at the 15:00Z boundary** (recorded here as evidence, not as a
tick on §6): `PASS` with `outcome_codes == {"GENOME_SHADOW": 4}` and `verified_ok 4` —
four EMITs, each paired with exactly one `GENOME_SHADOW` reject, `limit = quote + 0.01`,
nothing executed. At 18:00Z those same four markets answered `GENOME_ALREADY_TRADED`,
i.e. `entries_per_market: 1` held across a state reload.

### 4.1 How wide is the window, really

**A 500-line tail is 8–16 minutes of sandbox log, not one to two hours.** Measured on
2026-09-05 (six samples over the day, three of them consecutive): 14.0 / 14.2 / 14.3 min,
range across the day 8.1–16.3 min. The tail is all five bots' interleaved output, not the
genome's alone, so a busy minute shrinks it further.

Consequences, both of which the old remedies got wrong:

- **`--lines` cannot widen it.** Both the client (`min(max(lines,1), 500)`) and the server
  (`all_lines[-min(max(lines,1),500):]`, `src/web/server.py`) clamp at 500. Asking for
  more is silently the same request.
- **There is no ssh to maia,** so "pull a longer window from the bind mount" is not a thing
  anyone can do. `GET /api/logs/data` (last 100 data-log rows) is the only wider source,
  and the checker already uses it automatically.

The working procedure is therefore: **run the checker within ~10 minutes after a `:00` UTC
boundary.** Outside that, the tail has already scrolled past the decision points.

### 4.2 `NO_EMIT` is the NORMAL verdict — read `no_emit_reason`

The genome enters each market **once ever** (`entries_per_market: 1`), so the four markets
that emitted at 15:00Z answered `GENOME_ALREADY_TRADED` at 18:00Z and `n_emit` was 0. A bare
`NO_EMIT` cannot tell "no boundary sampled" from "genome silent" from "genome not loaded",
so the checker now reports which:

| `no_emit_reason` | Means | Do |
|---|---|---|
| `GENOME_ALIVE_NO_EMIT` | the genome logged skip codes (`GENOME_ALREADY_TRADED` / `MISSED_HOUR` / `MASK_FALSE` / `SIGMA_CAP` …) | nothing — it is loaded, deciding, and legitimately silent. `strategy_skip_codes` is the proof of life, and it outranks the other two reasons |
| `NO_GENOME_LINES` | the tail **covered a whole decision interval** (`:00` … `:00 + 120 s`) and the strategy logged nothing in it | the genome is probably not loaded — read the REFUSED line from the container's file log (§3.3) |
| `NO_BOUNDARY_IN_WINDOW` | no decision interval is fully inside the tail: it never spans a `:00`, **or it starts after one** (§4.1) | re-run within ~10 min of the next `:00 UTC`. `--lines` cannot help |

`boundaries_sampled` lists the **covered** boundaries; `boundaries_seen` lists the raw
`:00` stamps observed. Only the first supports a claim about the genome's silence: a tail
beginning at 19:00:30 contains `:00` stamps but cannot see a decision taken at 19:00:07,
and calling that "boundary sampled, genome silent" sent the operator hunting for a
`REFUSED` line that was never written.

### 4.3 Where the quote comes from

In order: `quote=<x>` on the EMIT line itself; else the strategy's own
`[Genome] DECIDE … quote= limit=` line for the same symbol within ±2 s (paper mode relies on
this, because the protected mixin's EMIT carries no quote); else the data-log row for that
symbol nearest at-or-before the EMIT stamp (traded-side ask, or `1 - yes_bid` for NO when no
NO-ask column is logged). An EMIT with no quote source is listed under `unverified` and is
never counted as a pass.

**A REJECT or EXECUTED line is never a quote source.** `GENOME_MASK_FALSE` and
`GENOME_NOT_EXECUTABLE` rejects carry a `quote=` of their own and land in the same log
second as the EMIT, so accepting them let file order decide the limit-price clause —
producing a FAIL and a PASS from the same content in two orderings. Only the EMIT's own
field and a `[Genome] DECIDE` line count.

Manual spot check (what the script automates):

```bash
curl -s 'http://maia.local:8050/api/logs/tail?pattern=money_printer_*.log&lines=500' \
  | python -c "import json,sys; print(json.load(sys.stdin)['content'])" \
  | grep -E 'strategy=Genome' | grep -E 'EMIT|EXECUTED|REJECT'
```

## 4.5 Calibration-provider check (before any paper promotion)

**Status 2026-09-06: the transfer gap is CLOSED in code; maia needs a redeploy (below).**

### 4.5.1 What the gap was

Replay parity is served the frame's **walk-forward** payloads
(`ev_analysis.WalkForwardCalibrator.calibration_as_of(city, T)`: refit per target date on
paired days with `target_date <= T - 1`); until 2026-09-06 `weather_bot.py` built the
**frozen** `<CITY>_gfs_mex_v1.json` payloads. Both read the same directory and report the
same `sha256`, so the directory hash cannot see the substitution -- and it is not cosmetic.
Diagnosed, the 0.336 is calibration CONTENT, not a data-file difference, a regime, a window
or a bug:

* the frozen payloads' `by_month_day_of` blocks are fitted on the **whole** month
  (n = 31 / 30 / 24 for 2026-05 / 06 / 07 -- May-18's block already contains May 19-31), and
  the engine resolves `by_month_day_of` first, so the frozen provider prices **every** ladder
  day with an in-sample month block;
* the walk-forward fit at the same date has only the days before it (May-18: 17 May days,
  under `MIN_BUCKET_N = 20`), so 43 of the 69 ladder dates per city fall to the season (23)
  or the pooled `day_of` block (20); only the last ~10 days of each month resolve to the
  same block under both, and even then with fewer days;
* the result, per city over 2026-05-18..07-25: sigma up to **2.2x** apart (LAX 2.16, CHI
  1.96, NY 1.58, MIA 1.48) and bias up to **3.2 F** apart (CHI 3.24, NY 2.37, MIA 1.11,
  LAX 0.31) -- on 53,545 of 54,159 compared rows, all four cities, all three months;
* the archives on disk hash to the frame's pins (`forecast_series_gfs_mex.csv` `2c836703...`,
  truth `e54c7a3d/bf420368/7ffca75b/2d169e59...`), and the walk-forward fit **at 2026-07-25**
  reproduces the frozen July block exactly (n = 24, same bias/sigma) -- the frozen artifact
  is simply the fit as of its last day, applied to every earlier day.

### 4.5.2 What changed

* `src/strategies/genome_strategy.py` gained `WalkForwardCalibrationProvider`
  (`kind = "walk_forward"`): the frame's `calibration_as_of` reproduced from the SAME two
  archives through the SAME `forecast_calibration` functions -- stdlib only, no pandas
  harness in the sandbox (`tests/test_walk_forward_provider.py` pins it payload-for-payload,
  `content_hash` included, against the real calibrator; 276/276 city-days identical). Its
  `sha256` is still the calibration-DIR identity the spec carries; the archives it prices
  from are reported as `forecast_sha256` / `truth_sha256` (whole-file sha256 = the frame
  provenance's `forecast_csv.sha256` / `truth_files[city].sha256`).
* `build_calibration_provider(spec, dir)` returns the provider `spec.calibration.kind` names;
  `WeatherBot.build_calibration_provider` is the bot's single entry to it, used by
  `_build_genome_strategy`, by `scripts/genome_dry_run.py`, and by
  `factory_replay_parity.py --calibration live`. A spec that says `frozen` (none committed)
  still gets the frozen provider; a spec naming no kind gets frozen + the guard's refusal /
  warning, exactly as before.
* The bot logs one `GenomeStrategy calibration provider for <id>: {...}` line at load with
  the kind, the archive shas and each city's `archive_last_target_date`.
* **The spec pins the archives (red team, 2026-09-06).** The dir sha and the kind together
  still could not see a walk-forward provider pointed at a *different* archive: +15 F on 61
  NY truth rows under a redirected root built the genome with `calibration_kind_ok: true`
  and priced a different fit. `promoted.CalibrationRef` now carries
  `calibration.forecast_sha256` and a per-station `calibration.truth_sha256`
  (`KNYC/KMDW/KLAX/KMIA`), inside `spec_hash`; `factory.py promote` stamps them from the
  parity run that authorised the spec (== the frame provenance's `forecast_csv.sha256` /
  `truth_files[city].sha256`); the six committed specs were backfilled from their own
  per-genome reports and the backfill verified by re-promoting `09fca4bc5ac55470` through
  the real path: **byte-identical**, `spec_hash 2612fdfb1416 -> c542c0475aaa`. The guard
  compares the provider's `describe()` shas to the pins: `mode: paper` **refuses** a
  mismatch (or a walk-forward spec that pins nothing), `mode: shadow` logs
  `ARCHIVE PIN MISMATCH` and continues, and `/api/genome` shows `archive_pins_ok` /
  `archive_pins_detail` / `archive_forecast_sha12_{spec,live}` next to the kind fields. A
  frozen-kind spec is unaffected (`archive_pins_ok: null`). Every archive sha in this
  chain -- provider, load-log line, spec pins, frame provenance, parity pin check, the table
  below -- is the **CRLF-normalised** `fees.sha256_file`, so an LF and a CRLF checkout of
  the same content agree (raw-byte hashing had made an LF checkout + CRLF root abort parity
  with `forecast archive sha db0911f30c45 != frame provenance 2c8367037cbf`).

Measure it -- the gating evidence, the diagnostic, and its control, same command:

```bash
# parity under THE provider weather_bot.py constructs (via WeatherBot.build_calibration_provider)
PYTHONPATH=. python scripts/factory_replay_parity.py --only fr31a_taker --calibration live \
    --out reports/factory/replay_parity_bfcf94654a3a_0c4b20502f2daf65_live.json
# the frozen diagnostic (still available; still 60 / 0.3357 -- nothing was papered over)
PYTHONPATH=. python scripts/factory_replay_parity.py --only fr31a_taker --calibration frozen
# the frame's own calibrator, the FR-F3.4 control
PYTHONPATH=. python scripts/factory_replay_parity.py --only fr31a_taker --calibration walk_forward \
    --out reports/factory/replay_parity_bfcf94654a3a_frozen_control.json
```

For `0c4b20502f2daf65`: **live = 0 discrepancies / p_yes_max_abs_diff 0.0 / 130 = 130
trades / 54,159 rows compared / `column_mismatches: {}` / `kind: "replay_parity"`**
(`reports/factory/replay_parity_bfcf94654a3a_0c4b20502f2daf65_live.json`, whose
`calibration.builder` names the bot's function, whose `inputs.calibration_provider`
carries the archive shas, and whose `genomes.fr31a_taker.spec_used` says the COMMITTED
`configs/factory/promoted/0c4b20502f2daf65.json` -- `spec_hash a48957d13a13` -- was the spec
handed to the bot's builder, not one synthesised from the frame). The frozen diagnostic
still reads **60 / 0.3357** (`_frozen.json`, `kind: "replay_parity_diagnostic"`). A
`--calibration live` run aborts if the provider's archive shas or embargo differ from the
frame provenance, or if the committed spec disagrees with the frame on kind / dir sha /
pins / frame sha, and is `replay_parity` only when the served kind equals the kind the frame
was proven under.

The spec records which provider proved it (`calibration.kind`, inside `spec_hash`) and
`GenomeStrategy`'s construction guard reads it:

* `mode: paper` + mismatch, **or a spec naming no kind at all** -> `GenomeSpecMismatch`,
  the bot logs `GenomeStrategy REFUSED` and runs V2 only. Silence is not proof.
* `mode: shadow` -> `CALIBRATION PROVIDER MISMATCH` on the runtime logger, run continues.
  **The shadow genome deployed 2026-09-05 logs this line until it is redeployed on this
  code** -- until then it is the accurate description of that run.

### 4.5.3 What maia needs (operator, before the redeploy)

The walk-forward provider reads two archives that are tracked in git but, like
`data/calibration`, are NOT in the image (the `/srv/money_printer/data` bind shadows
`/app/data`). `deploy/pi/deploy_f3_shadow.sh` step 2b now copies them into the bind and
prints their sha256; without them the genome is **REFUSED at load** with a message naming
that step (V2 keeps running -- the sandbox never crash-loops on it):

| file (repo path, copied to `/srv/money_printer/data/...`) | sha256 the frame AND the spec pin (CRLF-normalised `fees.sha256_file`; equals plain `sha256sum` on an LF checkout) |
|---|---|
| `data/forecast_archive/forecast_series_gfs_mex.csv` | `2c8367037cbf...` |
| `data/weather_truth/cli_daily_high_KNYC.csv` | `e54c7a3db1bf...` |
| `data/weather_truth/cli_daily_high_KMDW.csv` | `bf4203687d1e...` |
| `data/weather_truth/cli_daily_high_KLAX.csv` | `7ffca75bb594...` |
| `data/weather_truth/cli_daily_high_KMIA.csv` | `2d169e590df8...` |

Re-running `bash deploy/pi/deploy_f3_shadow.sh 0c4b20502f2daf65` on a `:00` boundary is the
whole procedure (it pulls, copies, rebuilds, relaunches). Then confirm on maia that the
loaded genome prices with the walk-forward provider and the mismatch line is gone:

```bash
curl -s 'http://maia.local:8050/api/logs/tail?pattern=money_printer_*.log&lines=500'   | python -c "import json,sys; print(json.load(sys.stdin)['content'])"   | grep -E 'GenomeStrategy calibration provider|CALIBRATION PROVIDER MISMATCH|GenomeStrategy REFUSED'
```

Expected: one `GenomeStrategy calibration provider for 0c4b20502f2daf65: {... "kind":
"walk_forward", "forecast_sha256": "2c8367037cbf..." ...}` line, and neither of the other
two. `/api/status`'s genome block reports the same as `calibration_kind_live` /
`calibration_kind_ok: true`.

**The archive on maia is a static copy.** The forecast series and CLI truth the
walk-forward provider prices from are copied into the `/srv` data bind by
`deploy_f3_shadow.sh` step 2b and never updated afterwards -- there is no re-sync job. Their
`archive_last_target_date` is **2026-09-01**, so a payload for any later target date is the
fit as of 2026-09-01: walk-forward-valid (nothing dated after T-1 can be in it) but stale,
and it stays stale until an operator re-syncs the archives on purpose. The provider's
`describe()` -- printed once at load as the `GenomeStrategy calibration provider for <id>`
line, with `archive_last_target_date` per city and the archive shas -- is where an operator
sees it. A re-sync is a data-provenance event, not housekeeping: newer archives change the
payloads for the dates the new rows touch (the frame's own rule -- the F0 backfill did
exactly this to July 2026), and the spec's `calibration.forecast_sha256` /
`truth_sha256` pins will then REFUSE paper and log `ARCHIVE PIN MISMATCH` in shadow until
the genome is re-promoted (or a new frame frozen) on the new files and parity re-run with
`--calibration live`.

### 4.6 Collecting a fill-realism tape that means something

Two ways this study has produced a number that was not what it claimed to be. Both
are now fixed in the script, but they decide how you collect.

**Collect over the hours the genome trades.** The 2026-09-05 runs covered 02:00Z and
03:00Z only; the deployed genome's 130 offline trades contain **zero** there (15Z
34.6 %, 04Z 27.7 %, 16Z 16.2 %). Aim at a `:00` boundary in the 14Z-16Z band:

```bash
PYTHONPATH=. python scripts/measure_fill_realism.py   --url http://maia.local:8050/api/logs/data   --cache data/fill_realism/maia_tape_<date>_daytime.csv   --collect-seconds 12000 --poll-interval 3.0 --date <date>
```

`data/fill_realism/` is gitignored scratch; commit a trimmed tape beside the report
as `reports/factory/fill_realism_<date>_tape.csv`, per the 2026-09-05 pair.

**The 20-second primary window is not measurable at maia's cadence.** Per-market poll
gap is p50 ~35-41 s / p90 ~74-79 s (the bot rotates cities), so the 20 s window has
n = 0 and the reported p90 is a *next-poll* number at a ~35-40 s lag — a conservative
upper bound, and the report says so in `p90_basis`. The table's older "14-s maia tape"
was wrong; nothing polls that fast.

**Analyse from the tape, not from the collecting process.** `--collect-seconds N` runs
one process for N seconds and then analyses, using the module it imported at *launch*.
On 2026-09-06 the fix below landed while a 3.5-hour collection was already running, and
that run's own end-of-run report still printed the pre-fix number. Collect, then re-run
the study over `--csv <cache>` at HEAD; that second pass is the artifact to commit.

**A zero ask is an empty book, not a cheap contract.** `_parse_price` returns `0.0`
for a missing ask, and a next-day ladder sits at ask 0.00 / volume 0 until it opens,
so the first real quote used to score as adverse drift of the entire ask. On the
2026-09-06 daytime tape that alone read p90 = **0.06** — six times `adverse_fill`,
which by the FR-5.2 exit criterion would have forced a registry change and a re-score
of family #1. `measure_fill_realism._ask` now drops non-positive asks, matching
`genome_strategy.py`'s own rule ("a zero ask is not a quote"), and the same tape reads
**0.00**. If you ever see a p90 in the tens of cents, check for `ask 0.00 / volume 0`
rows at the decision poll before believing it.

## 5. Weekly reconcile and gate cadence (GATE-owned scripts)

| When | What | Script (GATE workstream) |
|---|---|---|
| daily 13:30Z (existing timer) | settle sandbox weather positions against CLI truth | `scripts/reconcile_weather.py` (`deploy/pi/systemd/mp-reconcile-weather.timer`) |
| weekly (Monday 14:30Z, after the daily reconcile) — `mp-factory-reconcile.timer` **on alcyone** (F4; it needs the frozen search frame, which only the lab has, and reads maia over HTTP) | lab-vs-paper: every sandbox fill re-priced at `quote + adverse_fill`, C=20 taker, held to settlement; the sandbox trade set ⊆ the lab trade set with REJECT codes for the difference | `deploy/spark/factory_reconcile.sh` → `scripts/factory_paper_reconcile.py --url http://maia.local:8050` (`docs/FACTORY.md` §4.8) |
| once after ≥ 1 day of shadow tape, then after any `adverse_fill` change | intra-cadence bid/ask drift at :00 decision points on the maia tape (`/api/logs/data` or a local CSV); p90 → recommended `adverse_fill`. **Collect over the hours the genome trades** (15Z/16Z/04Z), not a quiet overnight boundary — see below | `scripts/measure_fill_realism.py` → `reports/factory/fill_realism_<date>.json` |
| after ≥ 50 settled `target_date`s of **paper** (F4) | FR-5.2 gate: exact binomial p vs fee-adjusted breakeven, net PnL > 0, spec hash unchanged vs `gate_registration.json` | `scripts/gate.py --registration configs/factory/gate_registration.json` → `reports/factory/gate_<id>.json` |

In shadow mode the reconcile has no fills to re-price; run it anyway once a week so the
"sandbox ⊆ lab" report exists with an empty sandbox set and the lab set from the promoted
genome — that is the parity evidence for the F3 exit. All of these read `closed_trades`
and the journal, never equity. Any state/journal written by an image older than 724d93c
must go through `scripts/repair_no_settlement_pnl.py --apply` first (§1.1); the gate
refuses otherwise. Before the first paper trade, copy
`configs/factory/gate_registration.template.json` to
`gate_registration_<genome_id>.json` (F4: `python scripts/factory.py register-gate <id>`
does this -- one file per genome, added to git once), commit it, then fill
`registration_commit_utc` from
`git log --diff-filter=A --format=%cI -- configs/factory/gate_registration_<genome_id>.json`
(`register-gate --fill-commit-time <id>`) and commit again -- the gate fails while it is
null. See `docs/FACTORY.md` section 4.5.

**Realistic fills (FR-5.2, updated 2026-09-05).** The exchange state still does not record
the flag, but the run can now record it for itself. The sandbox reads `MP_REALISTIC_FILLS`
(**default OFF** -- unset, today's fills are unchanged) and, either way, appends the
exchange's *effective* fill configuration to `data/fill_config.jsonl` at startup, on every
book movement, on a 300 s heartbeat and on shutdown. `gate.py --fill-config
data/fill_config.jsonl` (that is the default path) matches each settled fill's `entry_time`
against those run windows and passes the condition only when every scored fill was made
inside a window that recorded `realistic_fills=true`; an absent, stale or non-covering log
REFUSES. `--realistic-fills true|false` remains as the operator's own assertion for a run
with no log, and is now the *lowest*-precedence source: an assertion that contradicts the
log refuses instead of overriding it.

*How far this evidence goes — corrected 2026-09-05, after an adversarial review found the
opposite of what this paragraph used to claim.* **Every edge of a run window is now pinned
to an instant the run actually stamped**, because every field in the file is text somebody
could type: the window closes at the run's `stop` record, or at its last stamp plus its own
declared heartbeat grace (capped at 900 s), when it crashed; it opens at the claimed
`run_started_utc` *clamped* to no earlier than the run's first stamp minus that same cap;
a record whose `observed_utc` is in the future is dropped, because it claims a live process
stamped it at a time nobody has reached; and a run evidences nothing at all until it
carries a `start` record **and** a strictly later one. Before those bounds landed, this one
line made the gate report "all 60 admitted fill(s) fall inside a run window that recorded
realistic_fills=true" and print **PASS**, with no operator assertion:

```json
{"record":"fill_config","schema_version":1,"run_id":"FORGED","event":"stop",
 "run_started_utc":"1970-01-01T00:00:00+00:00","observed_utc":"2030-01-01T00:00:00+00:00",
 "heartbeat_sec":300.0,"realistic_fills":true}
```

**What it does NOT defend against, stated plainly: this log is written by the same host
that writes the journal, so it is not tamper-proof and cannot be.** A forger who writes
*two* coherent, past-dated lines — a `start` and a later `stop` bracketing the record —
produces a file shaped exactly like the one a genuine short run writes, and the gate cannot
tell them apart; nothing short of a signature or an off-host witness could. What the bounds
buy is that a forgery must now be a *coherent run history* rather than a single line --
a line appended under a genuine run's `run_id` can still shift that run's window, but by at
most the capped grace (900 s) on either edge, and the shift raises `start_clamped` in the
verdict -- and
that the log's sha256 is printed in the verdict (`inputs.fill_config_log_sha256`) so the
exact bytes that were scored can be re-hashed later. Read the condition as "the run that
made these trades recorded this fill configuration", never as "these trades provably had
realistic fills". **Turning the switch on is an owner decision** (it
changes what the paper record means -- PRD_STRATEGY_FACTORY owner decision #4); the
tooling only makes it possible and records what was actually done. Note the promoted
genome is a *taker*, and the modelled effect is a penny-floor **resting** order that may
not fill, so enabling it changes family #1's record very little -- that makes the condition
cheap to satisfy here, it is not a reason to register `requires_realistic_fills: false`.

## 6. F3 exit checklist (INFRA items)

Ticking these is the **owner's** call, not the tooling's. The wording below was
corrected where it stated something false (see the notes); the boxes are left as the
owner set them.

- [ ] `pytest tests/test_genome_dry_run.py tests/test_factory_isolation.py tests/test_protected_files.py tests/test_weather_lifecycle.py tests/test_v3_risk_rules.py` green on the dev box. **No xfails remain** to allow for: the earlier parenthetical excused two of them, and neither survives — the NO-side sign defect is fixed (724d93c, §1.1) and `src/strategies/genome_strategy.py` has landed, so the conditionally-xfailed isolation checks are hard assertions now.
- [ ] `reports/factory/dry_run_NY_2026-07-20.json` committed; the four lifecycle assertions
      `ok: true`; `settlement_pnl_matches_contract_side` recorded. The committed copy still
      records it **false**, i.e. it predates the 724d93c engine fix. The *code* is fixed and
      asserted hard by `tests/test_genome_dry_run.py::test_settlement_pnl_matches_contract_side`
      (green), so this is a stale artifact rather than a live defect: re-run
      `scripts/genome_dry_run.py --city NY --date 2026-07-20` and commit the regenerated report.
- [ ] alcyone §2 checks 1–4 pass on `money-printer-sandbox:f3`.
- [ ] maia §3 deployed with `GENOME_STRATEGY_ID=<seed>`; the container's own
      `printenv GENOME_STRATEGY_MODE` prints `shadow`. (Was `docker exec mp-sandbox env |
      grep GENOME_`, which a lone `GENOME_STRATEGY_ID` satisfied — see §3.2.)
- [ ] `scripts/check_maia_emit_cadence.py` → `PASS` with `outcome_codes == {"GENOME_SHADOW": n_emit}`
      and `verified_ok ≥ 1`. Run it within ~10 min of a `:00` (§4.1); the 2026-09-05 15:00Z
      observation is recorded in §4.
- [ ] `git diff 38d5fdd -- src/core/risk_manager.py src/bots/mixins.py src/core/matching_engine.py`
      reviewed on the merge commit. It is **not** empty and is not supposed to be: it is exactly
      the owner-ratified settlement hunk of §1.1 in `matching_engine.py`, which
      `tests/test_protected_files.py` allow-lists. The gate is that test passing. (An earlier
      revision of this line demanded an *empty* diff, which contradicted §1.1 of this same
      document and was never true.)

## 7. Live-conditions parity: missed hours, restarts, authorization (F3 red team, 2026-09-05)

The replay parity script visits every hourly candle, so it cannot see what a
live process does when it misses an hour. `GenomeStrategy` now enforces:

- **Missed-hour rule.** Per city it remembers the last evaluated hour. An hour is
  missed when the strategy had the chance and lost it: a tick outside the 120-s
  top-of-hour tolerance (recorded per city-hour), or a restart whose persisted
  last hour is more than one hour old. Then every city-day visible at the next
  evaluated hour is marked missed and rejects `GENOME_MISSED_HOUR` for the rest
  of the market-day. An hour with no ladder poll for the city is a DATA gap (the
  frame has no row either) and never a miss -- that keeps replay parity exact.
  The offline trade set is the FIRST masked executable snapshot per market; a
  strategy that skipped an hour cannot claim it. A market evaluated at every
  earlier hour (mask false / book empty / not executable) and masked-executable
  now emits normally -- that is the offline rule. Newly tracked city-days that
  first appear during a gap are marked missed too (conservative). A late tick
  counts as a lost chance even when it is the very first tick after a fresh
  deploy (no persisted state). A Kalshi poll that FAILS on the Pi (`Market
  Fetch Fail`) is reported to the strategy (`record_poll_failure`) and counts
  as a miss like a late tick -- only an hour with no candle in the archive is a
  data gap. **This is what makes an off-hour deploy expensive: see §3.1 — deploy
  inside the `:00` window or the restart itself forfeits every visible city-day.**
- **Persisted state.** `<MP_FORECAST_CACHE_DIR>/genome_state_<genome_id>.json`
  holds last hour per city, missed city-days and traded (target_date, symbol);
  rewritten atomically on every change, loaded at construction. A restarted
  process therefore never re-emits an already-traded market. Delete the file only
  when you deliberately want a fresh start (the first city-days after a FRESH
  deploy may emit later than the offline first hour; the weekly
  `factory_paper_reconcile.py` flags them). A state file of another genome_id is
  refused at construction.
- **EMIT lines carry the quote.** The shadow EMIT and GENOME_SHADOW lines carry
  `quote=` and `limit=`; in paper mode the strategy logs
  `[Genome] DECIDE ... quote= limit=` in the same second as the mixin's EMIT.
  `scripts/check_maia_emit_cadence.py` verifies `limit == quote + 0.01` from those
  fields and now exits 3 (`UNVERIFIED`) when no EMIT has a verifiable quote;
  `--allow-unverified` downgrades that for dry runs only. A `quote=` that
  contradicts the EMIT price is a FAIL.
- **Authorization.** `GENOME_STRATEGY_MODE=paper` on a shadow spec is REFUSED (an
  ERROR line, the bot runs V2 only) instead of silently tightening. Paper mode
  also requires the family's current status in the tracked
  `reports/factory/registry.jsonl` to be PROPOSED or RATIFIED and equal to the
  spec's `registry_status`; `spec_hash` is integrity, not authorization.
  **The sandbox needs a `reports/factory` bind to read that file at all.**
  `.dockerignore` keeps `reports/` out of the build context on purpose — an image
  is a build-time snapshot, and a family closed after the build would still read
  PROPOSED — so `deploy/pi/docker-compose.yml` mounts
  `../../reports/factory:/app/reports/factory:ro` (the DIRECTORY, not
  `registry.jsonl`: a `git pull` renames a new inode over the file and a
  single-file bind would go stale). Recreate the container after pulling a branch
  that adds it. Without the bind, paper mode refuses with **DEPLOYMENT
  MISCONFIGURED** — that is not a verdict on the family. Shadow mode is
  unaffected either way and only logs a warning; `deploy_f3_shadow.sh` reports
  whether `/app/reports/factory/registry.jsonl` is readable without gating on it.
- **Empty-ask sentinel.** A missing/zero ask from the poll is treated as no quote
  (non-executable) instead of a 0.00 quote.
