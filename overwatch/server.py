"""Web UI + JSON API + live event stream. The Watcher loop runs in a background thread."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .orchestrator import Overwatch

DIST = Path(__file__).parent / "ui" / "dist"


class SymptomIn(BaseModel):
    symptom: str
    service: str = "unknown"


LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>Overwatch</title>
<body style="font:15px system-ui;background:#0a0c11;color:#e7e9ef;display:grid;place-items:center;height:100vh;margin:0">
<p>This console is protected. Open it with <code>?token=&lt;your token&gt;</code> once; the browser remembers it.</p>"""


def create_app(ow: Overwatch, watch: bool = True, token: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if watch:
            threading.Thread(target=ow.run_forever, daemon=True, name="overwatch-watch").start()
        yield
        ow.stop()

    app = FastAPI(title="Overwatch", lifespan=lifespan)
    token = ow.cfg.console_token if token is None else token

    if token:
        @app.middleware("http")
        async def require_token(request: Request, call_next):
            header = request.headers.get("authorization", "")
            supplied = (
                request.query_params.get("token")
                or request.cookies.get("ow_token")
                or (header[7:] if header.lower().startswith("bearer ") else "")
            )
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return HTMLResponse(LOCKED_PAGE, status_code=401)
            if "token" in request.query_params:  # swap the token in the URL for a cookie
                response = RedirectResponse(request.url.path, status_code=303)
                response.set_cookie("ow_token", token, httponly=True, samesite="strict")
                return response
            return await call_next(request)

    @app.get("/api/status")
    def status() -> dict:
        return ow.status()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-sent events: replays recent activity, then streams live."""
        queue, backlog = ow.events.subscribe()

        async def stream():
            try:
                for event in backlog:
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                ow.events.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/memory")
    def memory() -> dict:
        try:
            return ow.memory.overview()
        except Exception as e:
            return {"health": {"ok": False, "engine": ow.memory.name, "error": str(e)}, "memories": [], "ledger": []}

    @app.post("/api/check")
    def check_now() -> dict:
        inc = ow.check_once()
        return {"incident": inc.to_dict() if inc else None}

    @app.post("/api/investigate")
    def investigate(body: SymptomIn) -> dict:
        # Runs in the background; progress arrives over /api/events.
        threading.Thread(target=ow.investigate_symptom, args=(body.symptom, body.service), daemon=True).start()
        return {"started": True}

    @app.post("/api/incidents/{incident_id}/approve")
    def approve(incident_id: str) -> dict:
        try:
            return {"receipts": ow.approve(incident_id)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/incidents/{incident_id}/resolve")
    def resolve(incident_id: str) -> dict:
        try:
            ow.resolve(incident_id)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.post("/api/incidents/{incident_id}/dismiss")
    def dismiss(incident_id: str) -> dict:
        try:
            ow.dismiss(incident_id)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    if DIST.exists():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            file = DIST / path
            return FileResponse(file if path and file.is_file() else DIST / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def missing_ui() -> HTMLResponse:
            return HTMLResponse("<p>UI not built. Run <code>npm install && npm run build</code> in <code>web/</code>.</p>")

    return app
