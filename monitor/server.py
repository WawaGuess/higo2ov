"""MonitorServer — FastAPI routes for the Higo2OV monitor dashboard."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)


def mount_monitor(app: FastAPI, data_dir: str | None = None) -> None:
    from .collector import TurnCollector

    collector = TurnCollector.get_instance(data_dir=data_dir)

    router = APIRouter(prefix="/monitor")

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def monitor_page() -> HTMLResponse:
        static_dir = Path(__file__).resolve().parent / "static"
        html_path = static_dir / "index.html"
        if html_path.exists():
            return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Monitor page not found</h1>", status_code=404)

    @router.get("/api/sessions")
    async def api_sessions() -> dict:
        return {"sessions": collector.list_sessions()}

    @router.get("/api/sessions/{session_id}")
    async def api_session(session_id: str) -> JSONResponse:
        data = collector.get_session(session_id)
        if data is None:
            return JSONResponse(status_code=404, content={"error": "Session not found"})
        return JSONResponse(content=data)

    @router.get("/api/sessions/{session_id}/turns")
    async def api_session_turns(session_id: str) -> JSONResponse:
        data = collector.get_session(session_id)
        if data is None:
            return JSONResponse(status_code=404, content={"error": "Session not found"})
        return JSONResponse(content={"turns": data.get("turns", [])})

    @router.get("/api/turns/latest")
    async def api_latest_turn() -> JSONResponse:
        turn = collector.get_latest_turn()
        if turn is None:
            return JSONResponse(status_code=404, content={"error": "No turns yet"})
        return JSONResponse(content=turn)

    app.include_router(router)
    logger.info("[Monitor] Mounted routes at /monitor")
