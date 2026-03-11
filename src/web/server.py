"""
FastAPI web server for the Money Printer HTML dashboard.

Routes:
    GET  /           → static index.html
    GET  /api/bots   → bot list with status
    POST /api/bots/{name}/start  → start a bot
    POST /api/bots/{name}/stop   → stop a bot
    WS   /ws         → broadcasts StateManager.snapshot() every second
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("web_server")

# Resolve path to the static directory relative to this file
STATIC_DIR = Path(__file__).parent / "static"


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
            raise HTTPException(status_code=501, detail="start_bot not implemented on orchestrator")
        try:
            orchestrator.start_bot(name)
            return JSONResponse(content={"status": "ok", "bot": name, "action": "started"})
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/bots/{name}/stop")
    async def stop_bot(name: str):
        if not hasattr(orchestrator, "stop_bot"):
            raise HTTPException(status_code=501, detail="stop_bot not implemented on orchestrator")
        try:
            orchestrator.stop_bot(name)
            return JSONResponse(content={"status": "ok", "bot": name, "action": "stopped"})
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
