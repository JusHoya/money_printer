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
  `src/core/matching_engine.py` — `git diff 38d5fdd -- <them>` must stay empty
  (`tests/test_protected_files.py`). Shadow handling lives in `weather_bot.py`.

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
  xgboost` blocked in `sys.modules` (xfail until STRATEGY lands the module, a hard
  assertion afterwards); no `datetime.now|time.time|utcnow|from datetime import` in
  `genome_strategy.py`, `features.py`, `genome.py`.
- `tests/test_protected_files.py` — the three protected files are byte-identical to
  `38d5fdd` in the working tree.
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

# runtime env (env_file). GENOME_STRATEGY_MODE here is documentation: the compose file
# pins shadow unless the compose SHELL says otherwise (see deploy/pi/docker-compose.yml).
sudo tee -a /srv/money_printer/.env >/dev/null <<'EOF'
GENOME_STRATEGY_ID=<seed id>
GENOME_STRATEGY_MODE=shadow
EOF

docker compose -f deploy/pi/docker-compose.yml up -d --build
curl -s http://localhost:8050/healthz            # {"status":"ok","uptime_s":...}
docker exec mp-sandbox env | grep -E 'GENOME_|MP_FORECAST'   # both GENOME_* present, mode=shadow
```

Env plumbing (what carries what):

| Variable | Carried by | Default in the container |
|---|---|---|
| `GENOME_STRATEGY_ID` | `/srv/money_printer/.env` via `env_file` | unset → no genome slot |
| `GENOME_STRATEGY_MODE` | compose `environment:` `${GENOME_STRATEGY_MODE:-shadow}` — the **invoking shell** wins, and this entry overrides the env_file value | `shadow` |
| `MP_FORECAST_CACHE_DIR` | compose `environment:` | `/app/data/forecast_cache` (bind → `/srv/money_printer/data/forecast_cache`) |

To roll back: remove `GENOME_STRATEGY_ID` from `.env` and `docker compose ... up -d`;
the waterfall is exactly what it was.

## 4. Verify the maia criterion over HTTP (`scripts/check_maia_emit_cadence.py`)

Criterion (PRD F3): `GenomeStrategy` EMIT lines appear at **:00 UTC only**, each with
**exactly one** `EXECUTED` or `REJECT` line — in shadow mode that line is
`[Risk] REJECT ... reason=GENOME_SHADOW` — and `limit_price = quote + 0.01`.

```powershell
# from any LAN host, stdlib only
python scripts/check_maia_emit_cadence.py                         # http://maia.local:8050
python scripts/check_maia_emit_cadence.py --url http://maia.local:8050 --lines 500 --json
python scripts/check_maia_emit_cadence.py --file money_printer_<stamp>.log   # a downloaded copy
```

What it reads: `GET /api/logs/tail?pattern=money_printer_*.log&lines=500` (the newest log,
500-line server cap; container `TZ=UTC`, so the log stamps are UTC) and
`GET /api/logs/data` (last 100 data-log rows) for the quote. Verdict JSON fields:
`n_emit`, `emit_off_hour` (minute ≠ 0), `emit_without_outcome`, `emit_multiple_outcomes`,
`outcome_codes` (expect `{"GENOME_SHADOW": n_emit}` in shadow), `limit_price`
(`verified_ok` / `verified_bad` / `unverified`, with examples). Exit 0 PASS, 1 FAIL, 3
`NO_EMIT` (no genome line in the window yet — wait for the next :00 UTC and re-run, or
raise `--lines`; at four cities × 24 candles a 500-line tail covers roughly one to two
hours of the sandbox log).

The quote for the limit-price check comes from a `quote=<x>` field on the EMIT line or
on a same-second line for the same symbol (the strategy's decision line), else from the
data-log row for that symbol nearest at-or-before the EMIT stamp (traded-side ask, or
`1 - yes_bid` for NO when no NO-ask column is logged). An EMIT with no quote source is
listed under `unverified` and is never counted as a pass. If everything is unverified,
pull a longer data-log window from the bind mount on maia and pass it with `--data-log`.

Manual spot check (what the script automates):

```bash
curl -s 'http://maia.local:8050/api/logs/tail?pattern=money_printer_*.log&lines=500' \
  | python -c "import json,sys; print(json.load(sys.stdin)['content'])" \
  | grep -E 'strategy=Genome' | grep -E 'EMIT|EXECUTED|REJECT'
```

## 5. Weekly reconcile and gate cadence (GATE-owned scripts)

| When | What | Script (GATE workstream) |
|---|---|---|
| daily 13:30Z (existing timer) | settle sandbox weather positions against CLI truth | `scripts/reconcile_weather.py` (`deploy/pi/systemd/mp-reconcile-weather.timer`) |
| weekly (Monday, after the daily reconcile) | lab-vs-paper: every sandbox fill re-priced at `quote + adverse_fill`, C=20 taker, held to settlement; the sandbox trade set ⊆ the lab trade set with REJECT codes for the difference | `scripts/factory_paper_reconcile.py` |
| once after ≥ 1 day of shadow tape, then after any `adverse_fill` change | intra-cadence bid/ask drift at :00 decision points on the 14-s maia tape (`/api/logs/data` or a local CSV); p90 → recommended `adverse_fill` | `scripts/measure_fill_realism.py` → `reports/factory/fill_realism_<date>.json` |
| after ≥ 50 settled `target_date`s of **paper** (F4) | FR-5.2 gate: exact binomial p vs fee-adjusted breakeven, net PnL > 0, spec hash unchanged vs `gate_registration.json` | `scripts/gate.py --registration configs/factory/gate_registration.json` → `reports/factory/gate_<id>.json` |

In shadow mode the reconcile has no fills to re-price; run it anyway once a week so the
"sandbox ⊆ lab" report exists with an empty sandbox set and the lab set from the promoted
genome — that is the parity evidence for the F3 exit. All of these read `closed_trades`
and the journal, never equity. Any state/journal written by an image older than 724d93c
must go through `scripts/repair_no_settlement_pnl.py --apply` first (§1.1); the gate
refuses otherwise. Before the first paper trade, copy
`configs/factory/gate_registration.template.json` to `gate_registration.json`, commit it,
then fill `registration_commit_utc` from
`git log --diff-filter=A --format=%cI -- configs/factory/gate_registration.json` and
commit again -- the gate fails while it is null. Pass `--realistic-fills true|false` to
`gate.py` (the exchange state does not record the flag).

## 6. F3 exit checklist (INFRA items)

- [ ] `pytest tests/test_genome_dry_run.py tests/test_factory_isolation.py tests/test_protected_files.py tests/test_weather_lifecycle.py tests/test_v3_risk_rules.py` green on the dev box (xfails: the NO-side sign defect, and the genome import checks until STRATEGY lands).
- [ ] `reports/factory/dry_run_NY_2026-07-20.json` committed; the four lifecycle assertions `ok: true`; `settlement_pnl_matches_contract_side` recorded (false until the engine fix).
- [ ] alcyone §2 checks 1–4 pass on `money-printer-sandbox:f3`.
- [ ] maia §3 deployed with `GENOME_STRATEGY_ID=<seed>`; `docker exec mp-sandbox env` shows `GENOME_STRATEGY_MODE=shadow`.
- [ ] `scripts/check_maia_emit_cadence.py` → `PASS` with `outcome_codes == {"GENOME_SHADOW": n_emit}` and `verified_ok ≥ 1`.
- [ ] `git diff 38d5fdd -- src/core/risk_manager.py src/bots/mixins.py src/core/matching_engine.py` empty on the merge commit.

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
  first appear during a gap are marked missed too (conservative).
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
- **Empty-ask sentinel.** A missing/zero ask from the poll is treated as no quote
  (non-executable) instead of a 0.00 quote.
