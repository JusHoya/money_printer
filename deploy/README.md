# Deployment — Pleiades split topology (2026-09 revival)

The Money Printer moves from a single GCE VM onto the Pleiades home cluster
(see `W:\Hoya_Space\Projects\pleiades` — the operational source of truth for
node names, decisions, and trust boundaries). Two hosts share the work:

| Host | Star name | Role |
|---|---|---|
| DGX Spark (`spark-87a1.local`) | **alcyone** | Hermes Agent + local model (LM Studio/vLLM on loopback), offline lab/backtest container |
| Raspberry Pi 4 (`maia.local`) | **maia** | The trading **sandbox**: feed harvester + paper-trading runtime in Docker, dashboard API on `:8050` (LAN only) |

Hermes on alcyone monitors the sandbox over the LAN via the `hermes_plugin/`
`mp_*` tools (`MONEY_PRINTER_URL=http://maia.local:8050`) and talks to the
operator via the Discord gateway. **No strategy trades live capital until it
has proven itself in the sandbox with realized, settlement-true outcomes**
(HANDOFF.md house rules; capital gate ≈ $350 after sandbox proof).

## What the sandbox runs (2026-09 markets expansion)

Decision record: `docs/MARKETS_EXPANSION_2026_09.md`.

- **weather** — paper trading **ON** in the sandbox (read-only Kalshi creds,
  SimulatedExchange only). This is the settlement-leg exercise HANDOFF.md §2
  flags as untested — *"No weather position has ever been opened"* — not a
  cleared strategy; clearing still requires realized, settlement-true outcomes.
- **mention** — feed-only harvester over the Kalshi mention-market series
  (`MENTION_SERIES` env). The strategy scaffold is gated off pending a
  base-rate corpus.
- **crypto_annual** — feed-only harvester over the annual crypto ladders
  (KXBTCY/KXETHY). Harvesting only; the July crypto refutation stands.
- **tweets** — feed-only harvester over Kalshi's X-settled series
  (`TWEETS_SERIES`, default KXPOTUSTWEETS plus the dormant KXELONTWEETS) and
  the X timeline tape behind them, which polls only when `X_FEED_ENABLED=1`.

## Non-negotiables carried over from the VM era

- **UTC everywhere.** Kalshi symbols are ET; `parse_expiry()` converts ET→UTC.
  A host-timezone mismatch once silently produced 0 training samples for
  months. Containers pin `TZ=UTC`; `bootstrap_maia.sh` sets the host to UTC.
- **No ML training in the runtime process** (PRD FR-0.2). The Pi image ships
  no torch/xgboost/lightgbm at all — training happens in the Spark lab
  container against the harvested tape.
- **Storage**: continuous CSV/log writes destroy SD cards. The compose file
  binds `data/` and `logs/` to `/srv/money_printer/` — put that on USB-SSD
  storage, not the SD card (bootstrap script checks and warns).
- **Secrets**: `.env` lives on the host, is bind-mounted read-only, and never
  enters the image. The old VM's Anthropic key and Discord webhook are treated
  as exposed until rotated (snapshot MANIFEST §6) — use fresh ones here.
- **Bind policy** (pleiades trust boundaries F1): the dashboard binds the LAN
  only because Hermes must reach it; nothing on alcyone that Hermes owns may
  bind non-loopback. Never port-forward :8050 off the LAN. The compose port
  bind is `${MAIA_BIND_IP:-0.0.0.0}:8050:8050` — set `MAIA_BIND_IP` to the
  Pi's LAN address to pin the bind to one interface.

## Layout

```
deploy/
  pi/                      maia — the sandbox host
    Dockerfile             arm64 runtime image (slim: no torch/ML training deps)
    requirements-runtime.txt
    docker-compose.yml     sandbox + autoheal services, volumes, healthcheck
    bootstrap_maia.sh      one-shot host prep (run ON maia)
    systemd/               daily reconcile timers (host-level, call into the container)
  spark/
    docker-compose.lab.yml offline lab/backtest container (isolates mp deps from other Spark projects);
                           runs as the host uid via LAB_UID/LAB_GID, logs on tmpfs (FR-F0.6)
    Dockerfile.lab         full-fat analysis image (offline ML stack)
    requirements-lab.lock  pinned lab manifest: `pip freeze` inside the lab image (FR-F0.6);
                           regenerate when Dockerfile.lab or requirements.txt change
    .env                   gitignored; LAB_UID/LAB_GID written by bootstrap_alcyone.sh
    bootstrap_alcyone.sh   one-shot lab bring-up (run ON alcyone): docker check,
                           repo clone into ~/projects/money_printer, archive-hash verify,
                           .env with the host uid, lab image build + smoke run + uid check
    ladder_capture.sh      M0 daily ladder capture into data/ladders_2026-09 (FR-F0.5)
    install_ladder_capture.sh  copies the units below into /etc/systemd/system and enables the timer
    systemd/               mp-ladder-capture.{service,timer} — daily 12:30 UTC, Persistent=true
    hermes_model_swap.sh   PREPARED, NOT AUTO-RUN: swaps the mp-vllm serving model
                           35B→9B to free ~35GB for lab work. Run only after
                           side-by-side validation; the verbatim rollback command
                           is in its header.
```

## Health, self-healing, and runtime knobs (maia)

- **Healthcheck** probes `GET /healthz` — a zero-side-effect liveness endpoint
  returning `{"status":"ok","uptime_s":...}`. The old probe target
  `/api/status` wrote a portfolio CSV row per probe (one every 60s, straight
  into SD-wear territory); never point the healthcheck back at it.
- **Autoheal**: plain Docker marks a container unhealthy but never restarts
  it. The `willfarrell/autoheal` companion service (arm64-compatible) restarts
  any container labeled `autoheal=true` — the sandbox carries the label — when
  its healthcheck fails, closing the hung-but-alive gap the VM watchdog used
  to cover.
- **`SIM_BALANCE`** (compose interpolation, default 3000): starting simulated
  balance, applied via the compose `command:` override — no image rebuild to
  change it. M2's $500 run:
  `SIM_BALANCE=500 docker compose -f deploy/pi/docker-compose.yml up -d`.
- **`MAIA_BIND_IP`** (compose interpolation, default 0.0.0.0): host interface
  for the `:8050` bind — see the bind policy above.
- **`MP_CONTROL_TOKEN`** (runtime env in `/srv/money_printer/.env`): when set,
  `POST /api/bots/{name}/start|stop` requires a matching `X-MP-Token` header
  (else 401). GET routes never require it. Set it: :8050 is LAN-visible and
  the control routes change runtime state.
- **X provider envs** (runtime, `/srv/money_printer/.env`): `X_FEED_ENABLED`,
  `X_BEARER_TOKEN`, `X_TRACK_HANDLES` — the official pay-per-use X API poller
  the `tweets` bot runs behind Kalshi's X-settled markets. **Off** until an X
  API account exists (`docs/MARKETS_EXPANSION_2026_09.md` §c); the bot still
  harvests the Kalshi side. `TWEETS_SERIES` selects those series and
  `MENTION_SERIES` the mention series the mention harvester tracks.

## Bring-up order

1. **maia**: get SSH key access, run `bootstrap_maia.sh`, fill `.env`, then
   `docker compose up -d` in `deploy/pi/`.
2. **alcyone**: serving layer per pleiades cluster Phase 3 (LM Studio :1234,
   vLLM :8000 fallback — DEC-007), then Hermes per cluster Phase 4 (custom
   endpoint path, loopback binds, tmux), then the SSH backend to maia
   (cluster Phase 5) and the Discord gateway (cluster Phase 8). For the lab
   container, run `bootstrap_alcyone.sh` ON alcyone.
3. Point the Hermes money-printer plugin at maia:
   `MONEY_PRINTER_URL=http://maia.local:8050`.

## Redeploy runbook (maia)

From the repo checkout ON maia (`~/money_printer`):

```bash
git pull --ff-only
docker compose -f deploy/pi/docker-compose.yml up -d --build
curl -s http://localhost:8050/healthz    # {"status":"ok","uptime_s":...}
```

`up -d --build` rebuilds only when the image inputs changed and recreates only
changed containers; bind-mounted state under `/srv/money_printer/` survives.
If `git pull` refuses (non-fast-forward), stop and reconcile by hand — never
reset the sandbox host's checkout blindly while the harvester is writing.

## Lab container on alcyone (FR-F0.6)

The lab (`deploy/spark/docker-compose.lab.yml`) runs as the **host uid**, so a
run never leaves root-owned files in the checkout, and `/app/logs` is a tmpfs.
The uid is interpolated from `LAB_UID`/`LAB_GID`:

```bash
# once — bootstrap_alcyone.sh writes deploy/spark/.env with your uid/gid (gitignored)
bash deploy/spark/bootstrap_alcyone.sh
# or per-invocation, no .env needed (the shell wins over .env):
LAB_UID=$(id -u) LAB_GID=$(id -g) docker compose -f deploy/spark/docker-compose.lab.yml run --rm lab id -u
```

`docker compose -f deploy/spark/docker-compose.lab.yml run --rm lab id -u`
must print your uid (F0 exit criterion). `deploy/spark/requirements-lab.lock`
is the pinned lab manifest — `pip freeze` captured inside the image — and is
regenerated whenever `Dockerfile.lab` or `requirements.txt` changes.

## M0 ladder capture (alcyone, FR-F0.5)

`PRD_STRATEGY_FACTORY.md` FR-F0.5 wants a daily capture of the four KXHIGH
ladders into the **sealed** root `data/ladders_2026-09/` (the Sept–Oct R3
reserve) until the kill date **2026-09-15**. maia has no headroom for it, so it
runs on alcyone through the lab container (network on):

```bash
# ON alcyone, from ~/projects/money_printer (checkout fast-forwarded, lab image built)
bash deploy/spark/install_ladder_capture.sh        # installs + enables mp-ladder-capture.timer
MP_CAPTURE_DRY_RUN=1 bash deploy/spark/ladder_capture.sh   # prints the plan, no network
sudo systemctl start mp-ladder-capture.service     # one real run now
journalctl -u mp-ladder-capture.service -n 80 --no-pager
```

What one run does (`deploy/spark/ladder_capture.sh`):

- target date = **yesterday in ET** (KXHIGH target dates are ET settlement
  days); with the default two-day lookback it re-pulls D-2 too so `result` /
  `expiration_value` settle into the tape;
- the timer fires at **12:30 UTC** — after LAX's 07:59Z close (NY/MIA 04:59Z,
  CHI 05:59Z) and after the NWS CLI publication Kalshi settles on;
  `Persistent=true` catches up a missed day;
- `docker compose ... run --rm lab python scripts/backfill_ladders.py --start D-2 --end D-1 --out data/ladders_2026-09`
  writes `data/ladders_2026-09/<SERIES>/<date>.csv`, `manifest.json` (last
  run) and a per-run copy `manifests/<date>.json`, plus a `SEALED` marker;
- commits **explicit paths only** (`git add -- <files>`, never `-A`), message
  `data(ladders_2026-09): capture <D-1>`, **no push**. Push by hand when
  convenient; maia's `git pull --ff-only` keeps working because only dated
  artifacts under one root are added;
- refuses to run once the target date passes the kill date; override only on
  purpose with `MP_CAPTURE_KILL_DATE=YYYY-MM-DD` (or `Environment=` in the unit).

The unit runs as `jushoya` (must be in the `docker` group) with
`WorkingDirectory=/home/jushoya/projects/money_printer`; edit the `.service`
if the checkout lives elsewhere. The search-frame loader refuses this root
and `data/ladders_holdout/` by path identity and by the `SEALED` marker
(`src/backtest/sealed_roots.py`, `tests/test_sealed_roots.py`).

## Factory (F1) — offline strategy search on alcyone

`PRD_STRATEGY_FACTORY.md` FR-F1.6 / `docs/factory/FACTORY_ARCHITECTURE.md` §7.1.
Two services in `deploy/spark/docker-compose.lab.yml` share the lab image:
`factory` (no network, no GPU, `cpuset 0-3,5-9,10-11,15-19`, `nice -n 10`,
`mem_limit 24g`, checkout `:ro`, the sealed ladder roots hidden behind empty
read-only tmpfs mounts) and `factory-holdout` (identical, plus
`data/ladders_holdout` and `data/ladders_2026-09` read-only — the only place
`factory.py holdout` / `factory.py score` may run). Only `data/factory/` and
`reports/factory/` are writable.

```bash
# ON alcyone, from ~/projects/money_printer
mkdir -p logs data/factory data/ladders_holdout data/ladders_2026-09   # bind sources + nested mountpoints (see the compose header)
docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory python scripts/factory.py freeze-frame
docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory python scripts/factory.py gen0 --bench
docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory python scripts/factory.py board
docker compose -f deploy/spark/docker-compose.lab.yml run --rm factory python scripts/factory.py coverage
```

`freeze-frame` writes `data/factory/frames/<lane>_<cutoff>_<sha12>/` (frames,
`provenance.json`, `frame.sha256`, `run.json`); `gen0` writes
`data/factory/runs/<run_id>/run.json` and `reports/factory/<run_id>/{summary.json,
summary.md,board.md,status.json}` plus `reports/factory/latest.json`. `run.json`
carries the git rev (abort if empty), the `requirements-lab.lock` sha, the frame
sha256s, the fee-regime sha and the host uid. `run/resume/controls/report/
holdout/score/promote` exit 2 until F2/F4.

## Factory (F2) — the evolutionary run on alcyone

**Runbook: `docs/factory/F2_RUNBOOK.md`** (the exact alcyone sequence, what to
commit, how the Discord evidence is collected). The pieces:

- `deploy/spark/systemd/mp-factory@.service` — user unit template, `%i` = run id,
  oneshot + `Restart=on-failure`, journald. Installed by
  `bash deploy/spark/install_factory_unit.sh` (copies into
  `~/.config/systemd/user`, `daemon-reload`, warns about docker group / linger).
- `deploy/spark/mp_factory_run.sh <run_id>` — the host wrapper the unit runs:
  `factory.py run --run-id <run_id>` (auto-`--resume` when
  `data/factory/runs/<run_id>/run.json` exists) → `controls` → `report`, with a
  60-s `free -g` sampler into `reports/factory/<run_id>/resources.log`
  (gitignored), a bench.json merge of the factory throughput + host numbers,
  then `deploy/spark/mp_factory_notify.sh <run_id> DONE|FAILED`, which always
  writes `reports/factory/<run_id>/completion.txt` and tries
  `hermes send --to discord:… --subject … --file completion.txt`.
- `scripts/factory_bench_coexist.py` — host-side (stdlib) mp-vllm latency bench:
  `--label idle` before the run, `--label running` mid-run, `--compare` (pass =
  |Δ p50 inter-token| ≤ 10 %), all merged into `reports/factory/<run_id>/bench.json`.
- `hermes_plugin/scripts/mp_factory_monitor.sh` — 10-min no-agent cron: one
  compact progress line from the active run's `status.json`, posted only on
  sha change, plus `completion.txt` once when it appears.

```bash
# ON alcyone, from ~/projects/money_printer (see the runbook for the full sequence)
bash deploy/spark/install_factory_unit.sh
cp hermes_plugin/scripts/mp_factory_monitor.sh ~/.hermes/scripts/
~/.local/bin/hermes cron create 10m --name mp-factory-monitor --no-agent \
    --script mp_factory_monitor.sh --deliver discord:1491982736989093961 \
    --provider custom --model ykarout/Qwen3.5-9B-NVFP4
RUN_ID=run_$(date -u +%F)
python3 scripts/factory_bench_coexist.py --label idle --out reports/factory/$RUN_ID/bench.json
systemctl --user start mp-factory@$RUN_ID
journalctl --user -u mp-factory@$RUN_ID -f
```

**Hermes.** The plugin gains `mp_factory_status` and `mp_factory_board`, which
read `$MONEY_PRINTER_FACTORY_DIR/latest.json` (default
`$MONEY_PRINTER_DIR/reports/factory`; on alcyone set it explicitly in
`~/.hermes/.env` to `/home/jushoya/projects/money_printer/reports/factory` —
the plugin's `~/money_printer` default does not exist there). The hourly board
post is a no-agent cron whose script dedupes on the sha256 of `board.md`
(timestamp-free by construction), in the same style as the `mp-watchdog`
`mp_watch.sh` job:

```bash
cp hermes_plugin/scripts/mp_factory_board.sh ~/.hermes/scripts/
~/.local/bin/hermes cron create 60m --name mp-factory-board --no-agent \
    --script mp_factory_board.sh --deliver discord:1491982736989093961 \
    --provider custom --model ykarout/Qwen3.5-9B-NVFP4
~/.local/bin/hermes cron list
```

Flags verified 2026-09-02 against `hermes cron add --help` on alcyone (the
schedule is positional; `add` aliases `create`; `--monitor-script` is the
agent-gating byte-hash mode and is incompatible with `--no-agent`, hence the
in-script hash). The state file is
`${MP_FACTORY_STATE:-~/.hermes/state/mp_factory_board.sha}`.

## Factory (F3) — promoted-genome slot, shadow deploy on maia

**Runbook: `docs/factory/F3_RUNBOOK.md`.** F3 adds the promoted-genome slot to
the sandbox image without touching a gate: `GENOME_STRATEGY_ID` (in
`/srv/money_printer/.env`, via `env_file`) names a `configs/factory/promoted/<id>.json`
spec and inserts `GenomeStrategy` ahead of V2 in the weather waterfall;
`GENOME_STRATEGY_MODE` is pinned to **shadow** by `deploy/pi/docker-compose.yml`
(`${GENOME_STRATEGY_MODE:-shadow}`, overriding `.env` — flip it only from the
compose shell, and only in F4 after ratification); `MP_FORECAST_CACHE_DIR`
points at the new `/srv/money_printer/data/forecast_cache` bind (create it and
`chown 1000:1000` before `up`). The runbook has the alcyone arm64 image build
plus in-image import checks, the maia `git pull --ff-only` / `.env` /
`compose up -d --build` sequence, `scripts/check_maia_emit_cadence.py` (verifies
the EMIT-at-:00 / one-REJECT-per-EMIT / `limit_price = quote + 0.01` criterion
over `GET /api/logs/tail` — no ssh), the dev-box dry run
`scripts/genome_dry_run.py`, and the weekly reconcile / gate cadence. It also
records the NO-side settlement sign defect the dry run found in
`matching_engine._close_position` (protected in F3; must be fixed before F4's
first paper trade).
