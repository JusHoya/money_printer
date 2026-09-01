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

## Layout

```
deploy/
  pi/                      maia — the sandbox host
    Dockerfile             arm64 runtime image (slim: no torch/ML training deps)
    requirements-runtime.txt
    docker-compose.yml     sandbox service + volumes + healthcheck
    bootstrap_maia.sh      one-shot host prep (run ON maia)
    systemd/               daily reconcile timers (host-level, call into the container)
  spark/
    docker-compose.lab.yml offline lab/backtest container (isolates mp deps from other Spark projects)
    bootstrap_alcyone.sh   one-shot Spark prep for the lab container
```

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
  bind non-loopback. Never port-forward :8050 off the LAN.

## Bring-up order

1. **maia**: get SSH key access, run `bootstrap_maia.sh`, fill `.env`, then
   `docker compose up -d` in `deploy/pi/`.
2. **alcyone**: serving layer per pleiades cluster Phase 3 (LM Studio :1234,
   vLLM :8000 fallback — DEC-007), then Hermes per cluster Phase 4 (custom
   endpoint path, loopback binds, tmux), then the SSH backend to maia
   (cluster Phase 5) and the Discord gateway (cluster Phase 8).
3. Point the Hermes money-printer plugin at maia:
   `MONEY_PRINTER_URL=http://maia.local:8050`.
