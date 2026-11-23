"""FastAPI local Web server, security middleware, REST API, and SSE stream."""
import asyncio
import json
import os
import secrets
import threading
import time
import urllib.request
import webbrowser
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path

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

from .backends import Edit
from .config import Verdict
from .process import redact
from .protocol import TaskSubmission
from .services import (
    AISYS_VERSION,
    AgentProfile,
    AIsysService,
    ConflictError,
    NotFoundError,
    ServerConfig,
)

STATIC_ASSETS = {
    "styles.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "graph.js": "application/javascript; charset=utf-8",
}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class ConfigSaveBody(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    max_concurrent_runs: int = Field(default=2, ge=1, le=8)
    default_project: str = Field(default="", max_length=500)
    expected_revision: str | None = None


class ValidateProjectBody(BaseModel):
    path: str


class AgentMutateBody(BaseModel):
    profile: AgentProfile
    expected_revision: str | None = None


class WorkflowValidateBody(BaseModel):
    yaml: str | None = None
    workflow: dict | None = None


class WorkflowSaveBody(BaseModel):
    yaml: str | None = None
    workflow: dict | None = None
    expected_revision: str | None = None


class WorkflowDuplicateBody(BaseModel):
    new_id: str
    new_name: str | None = None


class WorkflowImportBody(BaseModel):
    workflow_id: str
    yaml: str


class StartRunBody(BaseModel):
    workflow_id: str
    task: str = Field(min_length=1, max_length=12_000)
    category: str = "general"
    project: str | None = None


class CancelRunBody(BaseModel):
    terminate_active: bool = False


class InterruptDecisionBody(BaseModel):
    task_id: str | None = None
    accept: bool | None = None
    status: str = "completed"
    result: str = ""
    verdict: Verdict = "none"
    files: list[Edit] = Field(default_factory=list)
    review_feedback: str = ""
    error: str = ""
    human_confirmed: bool = False


class ShutdownBody(BaseModel):
    confirm_active: bool = False
    cancel_active: bool = False


def create_app(
    service: AIsysService,
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
            service.write_server_info(host, port)
        service.start_watcher()
        service.audit.record("service", f"AIsys Web service listening on http://{host}:{port}")
        try:
            yield
        finally:
            service.stop_watcher()
            if manage_server_file:
                service.clear_server_info()
            service.audit.record("service", "AIsys Web service stopped")

    app = FastAPI(title="AIsys Local Workbench", version=AISYS_VERSION, lifespan=lifespan)
    app.state.service = service
    app.state.csrf_token = token
    app.state.host = host
    app.state.port = port

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
        active = service.active_runs_for_shutdown()
        return {
            "app": "aisys",
            "version": AISYS_VERSION,
            "status": "ok",
            "pid": os.getpid(),
            "instance_id": service.instance_id,
            "host": host,
            "port": port,
            "data_dir": str(service.data),
            "project_root": str(service.project_root),
            "uptime_seconds": round(time.time() - service.started_at, 1),
            "active_runs": len(active),
        }

    @app.get("/api/dashboard")
    async def dashboard(refresh: bool = False):
        return service.dashboard(force_backend_check=refresh)

    @app.get("/api/backends")
    async def backends(refresh: bool = False):
        runs = service.list_runs_summary()
        active_codex = any(
            r["status"] in {"running", "stopping"} and r.get("active_backend") == "codex"
            for r in runs
        )
        waiting_google = any(
            r["status"] == "waiting" and "handoff" in r.get("interrupt_kinds", [])
            for r in runs
        )
        return service.inspector.inspect(
            active_codex=active_codex, waiting_google=waiting_google, force=refresh,
        )

    @app.get("/api/config")
    async def get_config():
        cfg, rev = service.config.get()
        return {"config": cfg.model_dump(mode="json"), "revision": rev, "data_dir": str(service.data)}

    @app.put("/api/config")
    async def save_config(body: ConfigSaveBody):
        cfg = ServerConfig(
            host=body.host,
            port=body.port,
            max_concurrent_runs=body.max_concurrent_runs,
            default_project=body.default_project,
        )
        saved, rev = service.config.save(cfg, expected_revision=body.expected_revision)
        service.publish("config_changed", {"kind": "server", "revision": rev})
        return {"config": saved.model_dump(mode="json"), "revision": rev}

    @app.post("/api/config/validate-project")
    async def validate_project(body: ValidateProjectBody):
        return service.config.validate_project_path(body.path)

    @app.get("/api/agents")
    async def list_agents():
        return service.agents.list_agents()

    @app.get("/api/agents/templates")
    async def agent_templates():
        return {"templates": service.agents.templates()}

    @app.post("/api/agents", status_code=201)
    async def create_agent(body: AgentMutateBody):
        result = service.agents.create_agent(body.profile, expected_revision=body.expected_revision)
        service.publish("config_changed", {"kind": "agent", "id": body.profile.id})
        return result

    @app.put("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, body: AgentMutateBody):
        result = service.agents.update_agent(agent_id, body.profile, expected_revision=body.expected_revision)
        service.publish("config_changed", {"kind": "agent", "id": agent_id})
        return result

    @app.delete("/api/agents/{agent_id}")
    async def delete_agent(agent_id: str, expected_revision: str | None = Query(default=None)):
        result = service.agents.delete_agent(agent_id, expected_revision=expected_revision)
        service.publish("config_changed", {"kind": "agent", "id": agent_id})
        return result

    @app.get("/api/workflows")
    async def list_workflows():
        return {"workflows": service.workflows.list_workflows()}

    @app.post("/api/workflows/validate")
    async def validate_workflow(body: WorkflowValidateBody):
        return service.workflows.validate(yaml_text=body.yaml, workflow_data=body.workflow)

    @app.post("/api/workflows/import", status_code=201)
    async def import_workflow(body: WorkflowImportBody):
        result = service.workflows.save(body.workflow_id, yaml_text=body.yaml)
        service.publish("config_changed", {"kind": "workflow", "id": body.workflow_id})
        return result

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str):
        return service.workflows.get_workflow(workflow_id)

    @app.put("/api/workflows/{workflow_id}")
    async def save_workflow(workflow_id: str, body: WorkflowSaveBody):
        result = service.workflows.save(
            workflow_id,
            yaml_text=body.yaml,
            workflow_data=body.workflow,
            expected_revision=body.expected_revision,
        )
        service.publish("config_changed", {"kind": "workflow", "id": workflow_id})
        return result

    @app.post("/api/workflows/{workflow_id}/duplicate", status_code=201)
    async def duplicate_workflow(workflow_id: str, body: WorkflowDuplicateBody):
        result = service.workflows.duplicate(workflow_id, body.new_id, new_name=body.new_name)
        service.publish("config_changed", {"kind": "workflow", "id": body.new_id})
        return result

    @app.delete("/api/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str):
        result = service.workflows.delete(workflow_id)
        service.publish("config_changed", {"kind": "workflow", "id": workflow_id})
        return result

    @app.get("/api/workflows/{workflow_id}/export", response_class=PlainTextResponse)
    async def export_workflow(workflow_id: str):
        item = service.workflows.get_workflow(workflow_id)
        return PlainTextResponse(
            item["yaml"],
            media_type="text/yaml; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{workflow_id}.yaml"'},
        )

    @app.get("/api/runs")
    async def list_runs_api(status: str = Query(default="all")):
        return {"runs": service.list_runs_summary(status_filter=status)}

    @app.post("/api/runs", status_code=201)
    async def start_run(body: StartRunBody):
        return service.start_run(
            workflow_id=body.workflow_id,
            task=body.task,
            category=body.category,
            project=body.project,
        )

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return service.get_run_detail(run_id)

    @app.get("/api/runs/{run_id}/diff")
    async def get_run_diff(run_id: str, space: str = Query(default="main")):
        return service.get_run_diff(run_id, space=space)

    @app.get("/api/runs/{run_id}/report")
    async def get_run_report(run_id: str):
        return service.get_run_report(run_id)

    @app.post("/api/runs/{run_id}/pause")
    async def pause_run(run_id: str):
        return service.pause_run(run_id)

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str):
        return service.resume_run(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, body: CancelRunBody | None = None):
        return service.cancel_run(run_id, terminate_active=bool(body and body.terminate_active))

    @app.post("/api/runs/{run_id}/interrupts/{interrupt_id}")
    async def submit_interrupt(run_id: str, interrupt_id: str, body: InterruptDecisionBody):
        submission = TaskSubmission(
            task_id=body.task_id,
            interrupt_id=interrupt_id,
            accept=body.accept,
            status=body.status,
            result=body.result,
            verdict=body.verdict,
            files=body.files,
            review_feedback=body.review_feedback,
            error=body.error,
            human_confirmed=body.human_confirmed,
        )
        return service.submit_interrupt(run_id, submission, interrupt_id=interrupt_id)

    @app.get("/api/logs")
    async def list_logs(
        run_id: str | None = Query(default=None),
        category: str = Query(default="all"),
        level: str = Query(default="all"),
        q: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ):
        return {
            "logs": service.audit.query(
                run_id=run_id, category=category, level=level, q=q, limit=limit,
            )
        }

    @app.get("/api/events")
    async def event_stream(request: Request):
        loop = asyncio.get_running_loop()
        queue = service.subscribe(loop)

        async def generator():
            try:
                hello = {"type": "connected", "ts": round(time.time(), 3), "data": {"instance_id": service.instance_id}}
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
                service.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/server/shutdown")
    async def shutdown_server(body: ShutdownBody | None = None):
        opts = body or ShutdownBody()
        return service.request_shutdown(
            confirm_active=opts.confirm_active,
            cancel_active=opts.cancel_active,
        )

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
    service = AIsysService(data, project_root=project_root)
    saved_cfg, _ = service.config.get()
    bind_host = host or saved_cfg.host or "127.0.0.1"
    bind_port = port if port is not None else saved_cfg.port
    if bind_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("AIsys web server only binds to loopback (127.0.0.1 or localhost)")

    app = create_app(
        service,
        host=bind_host,
        port=bind_port,
        manage_server_file=True,
    )
    config = uvicorn.Config(
        app,
        host=bind_host,
        port=bind_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    service._shutdown_callback = lambda: setattr(server, "should_exit", True)

    if open_browser:
        threading.Thread(
            target=_open_when_ready,
            args=(f"http://{bind_host}:{bind_port}",),
            daemon=True,
        ).start()

    print(f"AIsys Web Workbench running at http://{bind_host}:{bind_port}", flush=True)
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
