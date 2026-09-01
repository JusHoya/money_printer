"""
FastAPI web server for the Money Printer HTML dashboard.

Routes:
    GET  /           → static index.html
    GET  /api/bots   → bot list with status
    POST /api/bots/{name}/start  → start a bot
    POST /api/bots/{name}/stop   → stop a bot
    GET  /api/journal            → recent trade-journal entries
    GET  /api/training           → ML training state (offline-produced)
    GET  /api/win_rates          → raw per-strategy win-rate file
    GET  /api/stats/rolling      → rolling PnL/WR/EV over a window
    GET  /api/logs/tail          → tail of newest log matching a pattern
    WS   /ws         → broadcasts StateManager.snapshot() every second

The /api/journal, /api/training, /api/win_rates, /api/stats/rolling and
/api/logs/tail routes exist for remote monitoring: the Hermes agent on
alcyone reads them over the LAN when the sandbox runs on maia, where the
data files are not on the agent's filesystem (deploy/README.md).
"""

import asyncio
import glob as globmod
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
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
    app = FastAPI(title="Money Printer Dashboard", docs_url=None, redoc_url=None)

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
    async def start_bot(name: str):
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
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
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
                dt = datetime.fromisoformat(et.replace("Z", ""))
            except ValueError:
                continue
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
    async def stop_bot(name: str):
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

    # ------------------------------------------------------------------ #
    # Background broadcast task
    # ------------------------------------------------------------------ #

    @app.on_event("startup")
    async def start_broadcast_loop():
        asyncio.create_task(_broadcast_loop())

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

    return app
