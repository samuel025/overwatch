"""Web console + JSON API + live event stream, including the setup flow.

The server can start without a configuration ("setup mode"): the console then shows the setup page,
which runs discovery, writes overwatch.yaml and .env, and starts watching without a restart.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .orchestrator import Overwatch

DIST = Path(__file__).parent / "ui" / "dist"


class AlertIn(BaseModel):
    source: str = "external"
    service: str = "unknown"
    title: str
    description: str = ""


class ChangeIn(BaseModel):
    service: str
    version: str = ""
    kind: str = "deploy"
    author: str = ""
    description: str = ""
    ts: str | None = None


class SymptomIn(BaseModel):
    symptom: str
    service: str = "unknown"


class DockerIn(BaseModel):
    enabled: bool = True
    project: str = ""
    service_label: str = ""


class DatabaseIn(BaseModel):
    name: str
    url: str


class DiscoverIn(BaseModel):
    docker: DockerIn = Field(default_factory=DockerIn)
    prometheus_url: str = ""
    databases: list[DatabaseIn] = Field(default_factory=list)


class AiIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    triage_model: str = ""


class ApplyIn(BaseModel):
    plan: dict[str, Any]
    ai: AiIn = Field(default_factory=AiIn)


LOCKED_PAGE = """<!doctype html><meta charset="utf-8"><title>Overwatch</title>
<body style="font:15px system-ui;background:#0a0c11;color:#e7e9ef;display:grid;place-items:center;height:100vh;margin:0">
<p>This console is protected. Open it with <code>?token=&lt;your token&gt;</code> once; the browser remembers it.</p>"""


class Runtime:
    """Holds the running Overwatch, or None while the system is not configured yet."""

    def __init__(self, ow: Overwatch | None, watch: bool, config_path: str | Path | None):
        self.ow = ow
        self.watch = watch
        self.config_path = Path(config_path or "overwatch.yaml")
        self.lock = threading.Lock()

    def launch(self) -> None:
        if self.ow and self.watch:
            threading.Thread(target=self.ow.run_forever, daemon=True, name="overwatch-watch").start()
            self.ow.start_listeners()

    def require(self) -> Overwatch:
        if self.ow is None:
            raise HTTPException(409, "setup required: open the console to connect a system")
        return self.ow

    def switch(self, ow: Overwatch) -> None:
        with self.lock:
            old, self.ow = self.ow, ow
        if old:
            old.stop()
        self.launch()


def create_app(ow: Overwatch | None, watch: bool = True, token: str | None = None,
               config_path: str | Path | None = None) -> FastAPI:
    rt = Runtime(ow, watch, config_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        rt.launch()
        yield
        if rt.ow:
            rt.ow.stop()

    app = FastAPI(title="Overwatch", lifespan=lifespan)
    app.state.runtime = rt
    if token is None:
        token = ow.cfg.console_token if ow else os.environ.get("OVERWATCH_TOKEN", "")

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

    # -- setup -------------------------------------------------------------------------------

    @app.get("/api/setup/state")
    def setup_state() -> dict:
        from .docker_api import DockerAPI

        docker = DockerAPI()
        available = docker.available()
        return {
            "configured": rt.ow is not None,
            "config_path": str(rt.config_path.resolve()),
            "docker_available": available,
            "docker_projects": docker.projects() if available else [],
            "ai_configured": bool(os.environ.get("LLM_BASE_URL") or os.environ.get("LLM_API_KEY")),
            "ai_base_url": os.environ.get("LLM_BASE_URL", ""),
            "ai_model": os.environ.get("OVERWATCH_MODEL", ""),
        }

    @app.post("/api/setup/discover")
    def setup_discover(body: DiscoverIn) -> dict:
        from .discovery import discover, render

        docker = (
            {"project": body.docker.project, "service_label": body.docker.service_label}
            if body.docker.enabled else None
        )
        plan = discover(docker, body.prometheus_url.strip(), {d.name.strip(): d.url.strip() for d in body.databases
                                                            if d.name.strip() and d.url.strip()})
        text, _ = render(plan)
        return {"plan": plan, "yaml": text}

    @app.post("/api/setup/test-ai")
    def setup_test_ai(body: AiIn) -> dict:
        from openai import OpenAI

        base = body.base_url or os.environ.get("LLM_BASE_URL", "")
        key = body.api_key or os.environ.get("LLM_API_KEY", "")
        if not base:
            return {"ok": False, "error": "Enter the AI provider's address."}
        try:
            models = [m.id for m in OpenAI(base_url=base, api_key=key or "none", timeout=15, max_retries=0).models.list().data]
        except Exception as e:
            return {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:200]}"}
        return {"ok": True, "models": models[:200]}

    @app.post("/api/setup/apply")
    def setup_apply(body: ApplyIn) -> dict:
        from .config import load_config
        from .discovery import write

        ai = {k: v for k, v in body.ai.model_dump().items() if v}
        result = write(body.plan, rt.config_path, ai)
        for key, value in {"LLM_BASE_URL": ai.get("base_url"), "LLM_API_KEY": ai.get("api_key"),
                           "OVERWATCH_MODEL": ai.get("model"), "OVERWATCH_TRIAGE_MODEL": ai.get("triage_model")}.items():
            if value:
                os.environ[key] = value  # so the new config picks them up without a restart
        try:
            rt.switch(Overwatch(load_config(rt.config_path)))
        except Exception as e:
            raise HTTPException(400, f"The configuration was written to {result['config']} but could not start: {e}")
        return {"ok": True, **result}

    # -- console -----------------------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        return rt.require().status()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-sent events: replays recent activity, then streams live."""
        current = rt.require()
        queue, backlog = current.events.subscribe()

        async def stream():
            try:
                for event in backlog:
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                while not await request.is_disconnected() and rt.ow is current:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                current.events.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/memory")
    def memory() -> dict:
        ow = rt.require()
        try:
            return ow.memory.overview()
        except Exception as e:
            return {"health": {"ok": False, "engine": ow.memory.name, "error": str(e)}, "memories": [], "ledger": []}

    @app.post("/api/check")
    def check_now() -> dict:
        inc = rt.require().check_once()
        return {"incident": inc.to_dict() if inc else None}

    @app.post("/api/investigate")
    def investigate(body: SymptomIn) -> dict:
        # Runs in the background; progress arrives over /api/events.
        ow = rt.require()
        threading.Thread(target=ow.investigate_symptom, args=(body.symptom, body.service), daemon=True).start()
        return {"started": True}

    @app.post("/api/ingest")
    def ingest(body: dict) -> dict:
        """Push an alert in: our own {source, service, title, description}, or a Prometheus Alertmanager webhook."""
        ow = rt.require()
        alerts = []
        if isinstance(body.get("alerts"), list):  # Alertmanager webhook payload
            for a in body["alerts"]:
                if a.get("status", "firing") != "firing":
                    continue
                labels, notes = a.get("labels", {}), a.get("annotations", {})
                alerts.append(AlertIn(source="alertmanager", service=labels.get("service", labels.get("job", "unknown")),
                                      title=labels.get("alertname", "alert"),
                                      description=notes.get("summary") or notes.get("description", "")))
        else:
            alerts.append(AlertIn(**body))
        opened = [ow.ingest_alert(a.source, a.service, a.title, a.description) for a in alerts]
        return {"incidents": sorted({i.id for i in opened if i})}

    @app.post("/api/changes")
    def record_change(body: ChangeIn) -> dict:
        """Report a release or config change, e.g. from a CI pipeline."""
        return rt.require().record_change(body.service, body.version, body.kind, body.author, body.description, body.ts)

    @app.post("/api/incidents/{incident_id}/approve")
    def approve(incident_id: str) -> dict:
        try:
            return {"receipts": rt.require().approve(incident_id)}
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/incidents/{incident_id}/resolve")
    def resolve(incident_id: str) -> dict:
        try:
            rt.require().resolve(incident_id)
        except KeyError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.post("/api/incidents/{incident_id}/dismiss")
    def dismiss(incident_id: str) -> dict:
        try:
            rt.require().dismiss(incident_id)
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
