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
    docker-compose.lab.yml offline lab/backtest container (isolates mp deps from other Spark projects)
    Dockerfile.lab         full-fat analysis image (offline ML stack)
    bootstrap_alcyone.sh   one-shot lab bring-up (run ON alcyone): docker check,
                           repo clone, archive-hash verify, lab image build + smoke run
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
