"""
FastAPI web server for the Money Printer HTML dashboard.

Routes:
    GET  /           → static index.html
    GET  /healthz    → liveness probe (side-effect free; no snapshot, no writes)
    GET  /api/bots   → bot list with status
    POST /api/bots/{name}/start  → start a bot
    POST /api/bots/{name}/stop   → stop a bot
    GET  /api/portfolio_history  → merged equity curve from logs/portfolio_*.csv
    GET  /api/journal            → recent trade-journal entries
    GET  /api/training           → ML training state (offline-produced)
    GET  /api/win_rates          → raw per-strategy win-rate file
    GET  /api/stats/rolling      → rolling PnL/WR/EV over a window
    GET  /api/logs/tail          → tail of newest log matching a pattern
    WS   /ws         → broadcasts StateManager.snapshot() every second

The two POST bot-control routes optionally require an X-MP-Token header
matching the MP_CONTROL_TOKEN env var (open when the env var is unset);
GET routes never require it.

The /api/journal, /api/training, /api/win_rates, /api/stats/rolling and
/api/logs/tail routes exist for remote monitoring: the Hermes agent on
alcyone reads them over the LAN when the sandbox runs on maia, where the
data files are not on the agent's filesystem (deploy/README.md).
"""

import asyncio
import csv
import glob as globmod
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Set

from fastapi import FastAPI, Header, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("web_server")

# Resolve path to the static directory relative to this file
STATIC_DIR = Path(__file__).parent / "static"

# Repo root (overridable so tests and containers can relocate state)
PROJECT_ROOT = Path(os.getenv("MONEY_PRINTER_DIR", Path(__file__).parents[2]))

# Same rule the Hermes plugin applies to log tails: never serve a line that
# looks like it carries a secret.
SECRET_KEYWORDS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE")

# /healthz flips to 503 when the market loop's liveness stamp is older than
# this. Generous on purpose: cycle rollovers and slow discovery all run
# inside the loop body, so a healthy pass can legitimately take minutes.
LOOP_STALE_AFTER_S = 900


def _require_control_token(provided: str) -> None:
    """Bot-control POST guard. When MP_CONTROL_TOKEN is set (non-empty) the
    X-MP-Token header must match it; when unset the routes stay open. GET
    routes never require the token. Read per-request so tests and operators
    can toggle it without rebuilding the app."""
    expected = os.getenv("MP_CONTROL_TOKEN", "")
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="control token required")


def _read_journal_entries():
    """All parseable trade-journal entries, oldest first."""
    path = PROJECT_ROOT / "data" / "trade_journal.jsonl"
    trades = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return trades


def create_app(state_manager, orchestrator) -> FastAPI:
    """
    Factory function.  Call once from the entry-point script with a
    fully-initialised StateManager and OrchestratorEngine.
    """
    async def _broadcast_loop():
        """Push a fresh snapshot to every connected WebSocket every second."""
        while True:
            await asyncio.sleep(1.0)
            clients = app.state.connected
            if not clients:
                continue
            try:
                snapshot = state_manager.snapshot()
                payload = json.dumps(snapshot, default=str)
            except Exception as e:
                log.error(f"[WS] Snapshot error: {e}")
                continue

            dead: Set[WebSocket] = set()
            for ws in list(clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)

            clients -= dead

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Replaces the deprecated @app.on_event("startup") hook
        task = asyncio.create_task(_broadcast_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(
        title="Money Printer Dashboard",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------ #
    # Connected WebSocket clients
    # ------------------------------------------------------------------ #
    # Store on app.state so nested async functions can access it
    app.state.connected = set()

    # ------------------------------------------------------------------ #
    # Static files
    # ------------------------------------------------------------------ #
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------ #
    # HTTP routes
    # ------------------------------------------------------------------ #

    @app.get("/")
    async def index():
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        # Fallback: inline minimal page so the server is still useful
        # even if the static directory hasn't been populated yet.
        html = (
            "<!doctype html><html><head><title>Money Printer</title></head>"
            "<body><h1>Money Printer Dashboard</h1>"
            "<p>Static files not found. Place index.html in src/web/static/</p>"
            "</body></html>"
        )
        from fastapi.responses import HTMLResponse

        return HTMLResponse(content=html)

    @app.get("/healthz")
    async def healthz():
        """Liveness probe for the Pi compose healthcheck. MUST stay free of
        side effects: no snapshot(), no CSV writes — it is hit every few
        seconds by infrastructure, not humans.

        The harvester runs in a separate daemon thread, so uvicorn being
        alive proves nothing about the market loop. When the orchestrator
        carries a numeric ``_last_loop_pass_monotonic`` stamp (written once
        per market_loop pass) older than LOOP_STALE_AFTER_S, return 503 so
        autoheal can restart a hung loop. Absent the attribute (test stubs,
        TUI-less contexts) the probe stays 200."""
        uptime_s = float(getattr(orchestrator, "uptime_seconds", 0.0))
        stamp = getattr(orchestrator, "_last_loop_pass_monotonic", None)
        if isinstance(stamp, (int, float)):
            loop_age_s = time.monotonic() - stamp
            if loop_age_s > LOOP_STALE_AFTER_S:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "stale",
                        "uptime_s": uptime_s,
                        "loop_age_s": loop_age_s,
                    },
                )
        return JSONResponse(content={"status": "ok", "uptime_s": uptime_s})

    @app.get("/api/portfolio_history")
    async def get_portfolio_history(hours: int = 24):
        """Merged equity curve across ALL portfolio_*.csv sessions — the live
        copies in logs/ plus the ones the startup/cycle/shutdown sweeps moved
        into logs/_archive/<...>/, deduped by basename (logs/ copy preferred)
        so history survives restarts and cycle rollovers.

        Window clamped to 1..2160 hours (90 days), rows sorted ascending and
        downsampled server-side to ~1000 points (the newest row is always
        kept). Each point is {"ts": <epoch seconds float>, "equity", "cash",
        "exposure"} — same ts shape as snapshot.pnl_history. Source rows are
        written by Dashboard.log_portfolio (Timestamp, Equity, Cash,
        Exposure, ...); naive Timestamps are server-local wall clock, which
        is exactly what .timestamp() assumes (TZ=UTC in the container)."""
        hours = min(max(hours, 1), 2160)
        cutoff = datetime.now() - timedelta(hours=hours)
        logs_dir = PROJECT_ROOT / "logs"
        # Archive first, logs/ second: the same session file can sit in
        # several archive subdirs (shutdown_*/, startup_*/, cycle_*/), and
        # the dict overwrite keeps exactly one copy, preferring logs/.
        by_name = {}
        for path in sorted(
            globmod.glob(
                str(logs_dir / "_archive" / "**" / "portfolio_*.csv"),
                recursive=True,
            )
        ):
            by_name[os.path.basename(path)] = path
        for path in globmod.glob(str(logs_dir / "portfolio_*.csv")):
            by_name[os.path.basename(path)] = path
        rows = []
        for path in by_name.values():
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        try:
                            ts = datetime.fromisoformat(row["Timestamp"])
                            if ts.tzinfo is not None:
                                ts = ts.astimezone().replace(tzinfo=None)
                            if ts < cutoff:
                                continue
                            rows.append(
                                (
                                    ts,
                                    {
                                        "ts": ts.timestamp(),
                                        "equity": float(row["Equity"]),
                                        "cash": float(row["Cash"]),
                                        "exposure": float(row["Exposure"]),
                                    },
                                )
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
            except Exception as e:
                log.error(f"[api] portfolio history read error ({path}): {e}")
        rows.sort(key=lambda r: r[0])
        history = [r[1] for r in rows]
        if len(history) > 1000:
            step = -(-len(history) // 1000)  # ceil division
            sampled = history[::step]
            # [::step] keeps indices 0, step, 2*step, ... — the newest row
            # is silently dropped unless (len-1) % step == 0. Re-append it.
            if sampled[-1] is not history[-1]:
                sampled.append(history[-1])
            history = sampled
        return JSONResponse(content={"history": history, "count": len(history)})

    @app.get("/api/bots")
    async def get_bots():
        bots = getattr(orchestrator, "bots", [])
        active_set = getattr(orchestrator, "active_bots", None)
        result = []
        for bot in bots:
            if active_set is not None:
                active = bot.name in active_set
            else:
                active = True
            result.append({"name": bot.name, "active": active})
        return JSONResponse(content=result)

    @app.post("/api/bots/{name}/start")
    async def start_bot(
        name: str, x_mp_token: str = Header(default="", alias="X-MP-Token")
    ):
        _require_control_token(x_mp_token)
        if not hasattr(orchestrator, "start_bot"):
            raise HTTPException(
                status_code=501, detail="start_bot not implemented on orchestrator"
            )
        try:
            orchestrator.start_bot(name)
            return JSONResponse(
                content={"status": "ok", "bot": name, "action": "started"}
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/status")
    async def get_status():
        """Full state snapshot — portfolio, positions, strategies, market data."""
        try:
            snapshot = state_manager.snapshot()
            return JSONResponse(content=snapshot)
        except Exception as e:
            log.error(f"[api] status error: {e}")
            return JSONResponse(content={"error": str(e)}, status_code=500)

    @app.get("/api/logs/data")
    async def get_data_log():
        dashboard = getattr(orchestrator, "dashboard", None)
        if not dashboard or not hasattr(dashboard, "data_log_path"):
            return JSONResponse(content=[])
        try:
            path = Path(dashboard.data_log_path)
            if not path.exists():
                return JSONResponse(content=[])
            import csv

            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            return JSONResponse(content=rows[-100:])
        except Exception as e:
            log.error(f"[api] data log error: {e}")
            return JSONResponse(content=[])

    @app.get("/api/logs/session")
    async def get_session_log():
        dashboard = getattr(orchestrator, "dashboard", None)
        if not dashboard or not hasattr(dashboard, "session_log_path"):
            return JSONResponse(content=[])
        try:
            path = Path(dashboard.session_log_path)
            if not path.exists():
                return JSONResponse(content=[])
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return JSONResponse(content=[line.rstrip("\n") for line in lines[-50:]])
        except Exception as e:
            log.error(f"[api] session log error: {e}")
            return JSONResponse(content=[])

    @app.get("/api/journal")
    async def get_journal(last_n: int = 50, strategy: str = ""):
        """Recent closed trades from data/trade_journal.jsonl."""
        try:
            trades = _read_journal_entries()
        except FileNotFoundError:
            return JSONResponse(
                content={"ok": False, "error": "trade_journal.jsonl not found"},
                status_code=404,
            )
        if strategy:
            trades = [t for t in trades if t.get("strategy_name", "") == strategy]
        trades = trades[-min(max(last_n, 1), 500):]
        return JSONResponse(content={"ok": True, "count": len(trades), "trades": trades})

    @app.get("/api/training")
    async def get_training():
        """Offline-produced ML training state (FR-0.2: written by lab jobs only)."""
        path = PROJECT_ROOT / "data" / "training_state.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                return JSONResponse(content={"ok": True, "data": json.load(f)})
        except FileNotFoundError:
            return JSONResponse(
                content={"ok": False, "error": "training_state.json not found"},
                status_code=404,
            )
        except Exception as e:
            return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/win_rates")
    async def get_win_rates():
        """Raw strategy_win_rates.json — clients normalize window/legacy formats."""
        path = PROJECT_ROOT / "data" / "strategy_win_rates.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                return JSONResponse(content={"ok": True, "data": json.load(f)})
        except FileNotFoundError:
            return JSONResponse(
                content={"ok": False, "error": "strategy_win_rates.json not found"},
                status_code=404,
            )
        except Exception as e:
            return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/stats/rolling")
    async def get_rolling_stats(hours: int = 24):
        """Rolling PnL, win rate, EV and per-strategy breakdown over a window."""
        hours = min(max(hours, 1), 24 * 90)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        try:
            entries = _read_journal_entries()
        except FileNotFoundError:
            entries = []

        trades = []
        for t in entries:
            et = t.get("exit_time") or t.get("entry_time", "")
            if not et:
                continue
            try:
                # Journal timestamps arrive both as 'Z'-suffixed and as
                # '+00:00'-offset strings; fromisoformat (pre-3.11) only
                # accepts the offset form. Naive stamps are treated as UTC.
                dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > cutoff:
                trades.append(t)

        pnls = [t.get("pnl", 0) or 0 for t in trades]
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        n = len(trades)

        by_strat = defaultdict(list)
        for t in trades:
            by_strat[t.get("strategy_name", "?")].append(t.get("pnl", 0) or 0)
        strat_stats = {}
        for name, strat_pnls in sorted(by_strat.items(), key=lambda x: -sum(x[1])):
            sn = len(strat_pnls)
            sw = sum(1 for p in strat_pnls if p > 0)
            strat_stats[name] = {
                "trades": sn,
                "pnl": round(sum(strat_pnls), 2),
                "win_rate": round(sw / sn * 100, 1) if sn else 0,
            }

        return JSONResponse(
            content={
                "ok": True,
                "hours": hours,
                "trades": n,
                "pnl": round(total_pnl, 2),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / n * 100, 1) if n else 0,
                "ev_per_trade": round(total_pnl / n, 4) if n else 0,
                "by_strategy": strat_stats,
            }
        )

    @app.get("/api/logs/tail")
    async def get_log_tail(pattern: str = "*.log", lines: int = 100):
        """Tail of the most recently modified logs/ file matching the pattern."""
        if "/" in pattern or "\\" in pattern or ".." in pattern:
            raise HTTPException(status_code=400, detail="pattern must be a bare filename glob")
        logs_dir = PROJECT_ROOT / "logs"
        matches = sorted(
            globmod.glob(str(logs_dir / pattern)),
            key=os.path.getmtime,
            reverse=True,
        )
        if not matches:
            return JSONResponse(
                content={"ok": False, "error": f"No files matching '{pattern}' in logs/"},
                status_code=404,
            )
        path = matches[0]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)
        wanted = all_lines[-min(max(lines, 1), 500):]
        filtered = [
            line for line in wanted
            if not any(kw in line.upper() for kw in SECRET_KEYWORDS)
        ]
        return JSONResponse(
            content={
                "ok": True,
                "file": os.path.basename(path),
                "lines": len(filtered),
                "content": "".join(filtered),
            }
        )

    @app.post("/api/bots/{name}/stop")
    async def stop_bot(
        name: str, x_mp_token: str = Header(default="", alias="X-MP-Token")
    ):
        _require_control_token(x_mp_token)
        if not hasattr(orchestrator, "stop_bot"):
            raise HTTPException(
                status_code=501, detail="stop_bot not implemented on orchestrator"
            )
        try:
            orchestrator.stop_bot(name)
            return JSONResponse(
                content={"status": "ok", "bot": name, "action": "stopped"}
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ------------------------------------------------------------------ #
    # WebSocket broadcast
    # ------------------------------------------------------------------ #

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        app.state.connected.add(ws)
        log.info(f"[WS] Client connected. Total: {len(app.state.connected)}")
        try:
            while True:
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.debug(f"[WS] Client error: {e}")
        finally:
            app.state.connected.discard(ws)
            log.info(f"[WS] Client disconnected. Total: {len(app.state.connected)}")

    return app
