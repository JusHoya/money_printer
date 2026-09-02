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
