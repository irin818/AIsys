"""Minimalist FastAPI local server for the 5-Stage Dual-Model Co-Creation Studio."""
import asyncio
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from .collab import (
    AISYS_VERSION,
    CollabStore,
    ConflictError,
    Mode,
    NotFoundError,
    Owner,
    validate_project_path,
)
from .process import redact

STATIC_ASSETS = {
    "styles.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class ValidateProjectBody(BaseModel):
    path: str


class CreateSessionBody(BaseModel):
    task: str = Field(min_length=1, max_length=12_000)
    mode: Mode = "dev"
    project: str | None = None
    auto_start_codex: bool = True


class DecideBody(BaseModel):
    choice: Literal["gemini", "codex", "merge"]
    base_model: Owner | None = None
    fusion_notes: str = ""
    subtasks_override: list[dict] | None = None


class GeminiSubmitBody(BaseModel):
    action: Literal["propose", "critique", "merge", "execute_subtask", "review_peer"]
    payload: dict


class ShutdownBody(BaseModel):
    confirm_active: bool = False


def create_app(
    store: CollabStore,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    csrf_token: str | None = None,
    allow_testserver: bool = False,
    manage_server_file: bool = False,
) -> FastAPI:
    token = csrf_token or secrets.token_urlsafe(32)
    allowed_hosts = {
        "127.0.0.1", "localhost",
        f"127.0.0.1:{port}", f"localhost:{port}",
    }
    allowed_origins = {
        f"http://127.0.0.1:{port}", f"http://localhost:{port}",
        "http://127.0.0.1", "http://localhost",
    }
    if allow_testserver:
        allowed_hosts.add("testserver")
        allowed_origins.add("http://testserver")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if manage_server_file:
            store.write_server_info(host, port)
        store.start_watcher()
        try:
            yield
        finally:
            store.stop_watcher()
            if manage_server_file:
                store.clear_server_info()

    app = FastAPI(title="AIsys Dual-Model Studio", version=AISYS_VERSION, lifespan=lifespan)
    app.state.store = store
    app.state.csrf_token = token

    @app.middleware("http")
    async def local_security_middleware(request: Request, call_next):
        req_host = (request.headers.get("host") or "").strip().lower()
        if req_host and req_host not in allowed_hosts:
            return JSONResponse({"detail": "Disallowed Host header"}, status_code=403)

        if request.method in MUTATING_METHODS:
            origin = (request.headers.get("origin") or "").strip().lower()
            if origin and origin not in allowed_origins:
                return JSONResponse({"detail": "Cross-origin write request rejected"}, status_code=403)
            sec_fetch = (request.headers.get("sec-fetch-site") or "").strip().lower()
            if sec_fetch and sec_fetch not in {"same-origin", "none"}:
                return JSONResponse({"detail": "Cross-site write request rejected"}, status_code=403)
            supplied = request.headers.get("x-aisys-token", "")
            if not supplied or not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "Missing or invalid X-AIsys-Token"}, status_code=403)

        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotFoundError)
    async def handle_not_found(_: Request, exc: NotFoundError):
        return JSONResponse({"detail": redact(str(exc))}, status_code=404)

    @app.exception_handler(ConflictError)
    async def handle_conflict(_: Request, exc: ConflictError):
        return JSONResponse({"detail": redact(str(exc))}, status_code=409)

    @app.exception_handler(ValueError)
    async def handle_value_error(_: Request, exc: ValueError):
        return JSONResponse({"detail": redact(str(exc))}, status_code=400)

    @app.exception_handler(OSError)
    async def handle_os_error(_: Request, exc: OSError):
        return JSONResponse({"detail": redact(str(exc))}, status_code=500)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        raw_html = files("aisys.web").joinpath("index.html").read_text(encoding="utf-8")
        return HTMLResponse(raw_html.replace("__AISYS_CSRF_TOKEN__", token))

    @app.get("/static/{asset_name}")
    async def static_asset(asset_name: str):
        media_type = STATIC_ASSETS.get(asset_name)
        if not media_type:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        content = files("aisys.web").joinpath(asset_name).read_bytes()
        return Response(content=content, media_type=media_type)

    @app.get("/api/health")
    async def health():
        return {
            "app": "aisys",
            "version": AISYS_VERSION,
            "status": "ok",
            "pid": os.getpid(),
            "instance_id": store.instance_id,
            "host": host,
            "port": port,
            "data_dir": str(store.data),
            "project_root": str(store.project_root),
            "uptime_seconds": round(time.time() - store.started_at, 1),
            "active_runs": store.active_sessions_count(),
        }

    @app.get("/api/backends")
    async def backends(refresh: bool = False):
        sessions = store.list_sessions()
        active_codex = any(s["codex_busy"] for s in sessions)
        waiting_gemini = any(
            s["stage"] in {"proposing", "critiquing", "merging", "executing", "reviewing"}
            for s in sessions
        )
        return store.inspector.inspect(
            active_codex=active_codex, waiting_gemini=waiting_gemini, force=refresh,
        )

    @app.post("/api/validate-project")
    async def check_project(body: ValidateProjectBody):
        return validate_project_path(body.path)

    @app.get("/api/sessions")
    async def list_sessions():
        return {"sessions": store.list_sessions()}

    @app.post("/api/sessions", status_code=201)
    async def create_session(body: CreateSessionBody):
        return store.create_session(
            task=body.task,
            mode=body.mode,
            project=body.project,
            auto_start_codex=body.auto_start_codex,
        )

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        return store.get_session_view(session_id)

    @app.post("/api/sessions/{session_id}/decide")
    async def decide_session(session_id: str, body: DecideBody):
        return store.decide(
            session_id,
            choice=body.choice,
            base_model=body.base_model,
            fusion_notes=body.fusion_notes,
            subtasks_override=body.subtasks_override,
        )

    @app.post("/api/sessions/{session_id}/gemini")
    async def submit_gemini(session_id: str, body: GeminiSubmitBody):
        match body.action:
            case "propose":
                return store.submit_gemini_proposal(session_id, body.payload)
            case "critique":
                return store.submit_gemini_critique(session_id, body.payload)
            case "merge":
                return store.submit_merged_plan(session_id, body.payload)
            case "execute_subtask":
                return store.submit_gemini_subtask(
                    session_id,
                    task_id=body.payload["task_id"],
                    summary=body.payload.get("summary", ""),
                    files=body.payload.get("files", []),
                )
            case "review_peer":
                return store.submit_gemini_review(session_id, body.payload)
        raise ValueError(f"unsupported action: {body.action}")

    @app.post("/api/sessions/{session_id}/run-codex")
    async def run_codex(session_id: str):
        store.trigger_codex_async(session_id)
        return store.get_session_view(session_id)

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str):
        return store.cancel_session(session_id)

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(session_id: str, limit: int = Query(default=150, ge=1, le=500)):
        return {
            "session_id": session_id,
            "events": store.list_events(session_id, limit=limit),
            "active_process": store.get_active_process(session_id),
        }

    @app.get("/api/sessions/{session_id}/diff")
    async def session_diff(session_id: str):
        return store.get_diff(session_id)

    @app.get("/api/sessions/{session_id}/study-guide")
    async def session_study_guide(session_id: str, download: bool = Query(default=False)):
        guide = store.get_study_guide(session_id)
        if download:
            return PlainTextResponse(
                guide["markdown"],
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="aisys-study-{session_id[:8]}.md"'},
            )
        return guide

    @app.post("/api/focus-antigravity")
    async def focus_antigravity():
        candidates = [
            Path("/Applications/Antigravity.app"),
            Path("/Applications/Gemini.app"),
        ]
        found = next((p for p in candidates if p.exists()), None)
        if allow_testserver:
            return {"focused": False, "app": found.stem if found else None, "reason": "test_mode"}
        if sys.platform != "darwin" or not found:
            return {"focused": False, "app": None, "reason": "not_installed"}
        try:
            subprocess.Popen(
                ["open", "-a", str(found)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"focused": True, "app": found.stem}
        except OSError:
            return {"focused": False, "app": found.stem, "reason": "launch_error"}

    @app.get("/api/events")
    async def event_stream(request: Request):
        loop = asyncio.get_running_loop()
        queue = store.subscribe(loop)

        async def generator():
            try:
                hello = {"type": "connected", "ts": round(time.time(), 3), "data": {"instance_id": store.instance_id}}
                yield f"event: connected\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield (
                            f"event: {event['type']}\n"
                            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                        )
                    except TimeoutError:
                        yield f"event: ping\ndata: {json.dumps({'ts': round(time.time(), 3)})}\n\n"
            finally:
                store.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/server/shutdown")
    async def shutdown_server(body: ShutdownBody | None = None):
        opts = body or ShutdownBody()
        return store.request_shutdown(confirm_active=opts.confirm_active)

    return app


def _open_when_ready(url: str) -> None:
    health_url = f"{url}/api/health"
    for _ in range(40):
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("app") == "aisys":
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.25)


def run_server(
    data: Path,
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = False,
    project_root: Path | None = None,
) -> int:
    store = CollabStore(data, project_root=project_root)
    saved_cfg = store.get_server_config()
    bind_host = host or saved_cfg.host or "127.0.0.1"
    bind_port = port if port is not None else saved_cfg.port
    if bind_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("AIsys web server only binds to loopback (127.0.0.1 or localhost)")

    app = create_app(store, host=bind_host, port=bind_port, manage_server_file=True)
    config = uvicorn.Config(
        app,
        host=bind_host,
        port=bind_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    store._shutdown_callback = lambda: setattr(server, "should_exit", True)

    if open_browser:
        threading.Thread(
            target=_open_when_ready,
            args=(f"http://{bind_host}:{bind_port}",),
            daemon=True,
        ).start()

    print(f"AIsys Dual-Model Studio running at http://{bind_host}:{bind_port}", flush=True)
    server.run()
    return 0


def inspect_server_status(data: Path) -> dict:
    """Verify whether an AIsys server registered in data/server.json is genuinely alive."""
    info_path = data.resolve() / "server.json"
    if not info_path.is_file():
        return {"running": False, "reason": "no server.json found"}
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
        host = info["host"]
        port = int(info["port"])
        pid = int(info["pid"])
        instance_id = info["instance_id"]
    except (KeyError, OSError, ValueError):
        return {"running": False, "reason": "corrupt server.json"}

    try:
        os.kill(pid, 0)
    except OSError:
        return {"running": False, "stale": True, "pid": pid, "reason": "recorded PID is not running"}

    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            live = json.loads(resp.read().decode("utf-8"))
        if live.get("app") == "aisys" and live.get("instance_id") == instance_id:
            return {"running": True, "url": f"http://{host}:{port}", **live}
        return {"running": False, "reason": "port listener does not match AIsys instance_id"}
    except Exception as error:
        return {"running": False, "pid": pid, "reason": f"health check failed: {error}"}
