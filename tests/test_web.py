import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from fastapi.testclient import TestClient

from aisys.backends import BackendResult, Edit, GoogleBackend, Reply
from aisys.cli import invoke, parser
from aisys.services import AIsysService, ServerConfig
from aisys.web_server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "AIsys.command"


class StubCodex:
    name = "codex"
    handoff_reason = None

    def __init__(self, handler=None, health_status="ok", health_error=""):
        self.handler = handler or (
            lambda prompt: BackendResult(
                status="ok",
                reply=Reply(
                    summary="Implemented cleanly",
                    decision="none",
                    files=[Edit(path="solution.py", content="def ok():\n    return 42\n")],
                ),
            )
        )
        self.health_status = health_status
        self.health_error = health_error
        self.prompts = []

    def health(self):
        return BackendResult(status=self.health_status, error=self.health_error)

    def run(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.handler(prompt)


def make_client(tmp_path, *, codex=None, sync=True):
    providers = {"codex": codex or StubCodex(), "google": GoogleBackend()}
    service = AIsysService(tmp_path, backends=providers, sync_execution=sync)
    app = create_app(service, csrf_token="test-csrf-token", allow_testserver=True)
    client = TestClient(app)
    headers = {"X-AIsys-Token": "test-csrf-token"}
    return client, service, headers


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_security_csrf_origin_host_and_project_validation(tmp_path):
    client, _, headers = make_client(tmp_path)

    # Static assets & HTML shell load cleanly with injected CSRF token
    html = client.get("/")
    assert html.status_code == 200
    assert "test-csrf-token" in html.text
    for asset in ("styles.css", "app.js", "graph.js"):
        assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/static/../config.py").status_code == 404

    # Missing or bad CSRF token rejected on mutating endpoints
    assert client.post("/api/runs", json={"workflow_id": "blank", "task": "hi"}).status_code == 403
    assert client.post(
        "/api/runs",
        json={"workflow_id": "blank", "task": "hi"},
        headers={"X-AIsys-Token": "wrong"},
    ).status_code == 403

    # Cross-origin and cross-site requests rejected even with token
    assert client.post(
        "/api/runs",
        json={"workflow_id": "blank", "task": "hi"},
        headers={**headers, "Origin": "https://evil.example"},
    ).status_code == 403
    assert client.post(
        "/api/runs",
        json={"workflow_id": "blank", "task": "hi"},
        headers={**headers, "Sec-Fetch-Site": "cross-site"},
    ).status_code == 403

    # Disallowed Host header rejected (DNS rebinding protection)
    assert client.get("/api/health", headers={"Host": "evil.local"}).status_code == 403

    # Sensitive or non-git project directories rejected
    assert client.post(
        "/api/config/validate-project", json={"path": "/etc"}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/config/validate-project", json={"path": str(tmp_path)}, headers=headers
    ).status_code == 400


def test_honest_backend_status_reporting_and_unrouted_guard(tmp_path):
    # CLI present but unauthenticated MUST NOT be reported as callable
    unauth = StubCodex(health_status="auth", health_error="Not logged in")
    client, _, headers = make_client(tmp_path, codex=unauth)
    backends = client.get("/api/backends?refresh=true").json()
    assert backends["codex"]["state"] == "installed"
    assert backends["codex"]["installed"] is True
    assert backends["codex"]["authenticated"] is False
    assert backends["codex"]["callable"] is False
    assert backends["google"]["state"] == "restricted"
    assert backends["google"]["callable"] is False

    # Launching a Codex workflow when Codex is not callable is rejected cleanly
    resp = client.post(
        "/api/runs",
        json={"workflow_id": "local_coding", "task": "Write answer.py"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "not callable" in resp.json()["detail"].lower()


def test_agent_crud_persistence_and_conflict_detection(tmp_path):
    client, _, headers = make_client(tmp_path)
    initial = client.get("/api/agents").json()
    assert len(initial["agents"]) >= 6
    rev = initial["revision"]

    new_profile = {
        "id": "codex_architect",
        "name": "Codex Architect",
        "role": "Architect",
        "backend": "codex",
        "instructions": "Design modular Python APIs.",
        "write_paths": ["api.py"],
        "reuse_session": False,
        "on_unavailable": "stop",
        "default_space": "main",
        "default_timeout": 90,
        "default_retries": 1,
        "template": "architect",
        "description": "Custom architecture role",
    }
    created = client.post(
        "/api/agents", json={"profile": new_profile, "expected_revision": rev}, headers=headers
    )
    assert created.status_code == 201
    rev2 = created.json()["revision"]

    # Stale revision returns 409 Conflict
    stale = client.put(
        "/api/agents/codex_architect",
        json={"profile": {**new_profile, "role": "Lead Architect"}, "expected_revision": rev},
        headers=headers,
    )
    assert stale.status_code == 409

    # Valid update persists across fresh service instance (simulating restart/reload)
    updated = client.put(
        "/api/agents/codex_architect",
        json={"profile": {**new_profile, "role": "Lead Architect"}, "expected_revision": rev2},
        headers=headers,
    )
    assert updated.status_code == 200
    reloaded_client, _, _ = make_client(tmp_path)
    agents = reloaded_client.get("/api/agents").json()["agents"]
    found = next(a for a in agents if a["id"] == "codex_architect")
    assert found["role"] == "Lead Architect"
    assert found["write_paths"] == ["api.py"]

    deleted = client.delete(
        f"/api/agents/codex_architect?expected_revision={updated.json()['revision']}",
        headers=headers,
    )
    assert deleted.status_code == 200


def test_workflow_crud_validation_and_web_cli_consistency(tmp_path, capsys):
    client, _, headers = make_client(tmp_path)

    # Duplicate blank into a custom workflow and edit it via Web API
    dup = client.post(
        "/api/workflows/blank/duplicate",
        json={"new_id": "custom_sync", "new_name": "Web CLI Sync Flow"},
        headers=headers,
    )
    assert dup.status_code == 201
    rev = dup.json()["revision"]

    wf_obj = dup.json()["workflow"]
    wf_obj["name"] = "Updated via Web"
    wf_obj["nodes"]["gate"] = {
        "kind": "approval",
        "instruction": "Confirm from Web or CLI",
        "next": "report",
    }
    wf_obj["start"] = "gate"

    saved = client.put(
        "/api/workflows/custom_sync",
        json={"workflow": wf_obj, "expected_revision": rev},
        headers=headers,
    )
    assert saved.status_code == 200

    # Stale save rejected with 409
    assert client.put(
        "/api/workflows/custom_sync",
        json={"workflow": wf_obj, "expected_revision": rev},
        headers=headers,
    ).status_code == 409

    # Invalid workflow rejected cleanly by validator
    bad = client.post(
        "/api/workflows/validate",
        json={"yaml": "version: 1\nname: Broken\nstart: missing\nnodes:\n  a: {kind: report}\n"},
        headers=headers,
    )
    assert bad.status_code == 200
    assert bad.json()["valid"] is False

    # CLI immediately sees the exact Web-saved workflow (single source of truth!)
    assert invoke(parser().parse_args(["--data", str(tmp_path), "validate", "custom_sync"])) == 0
    out = capsys.readouterr().out
    assert "Updated via Web · start: gate" in out

    # Start a run via Web -> verify CLI sees identical waiting state
    run_resp = client.post(
        "/api/runs",
        json={"workflow_id": "custom_sync", "task": "Verify Web/CLI unity"},
        headers=headers,
    )
    assert run_resp.status_code == 201
    run = run_resp.json()
    assert run["status"] == "waiting"
    run_id = run["id"]
    interrupt_id = run["interrupts"][0]["id"]

    assert invoke(parser().parse_args(["--data", str(tmp_path), "show", run_id, "--json"])) == 0
    cli_view = json.loads(capsys.readouterr().out)
    assert cli_view["status"] == "waiting"
    assert cli_view["node"] == "gate"

    # Approval without human_confirmed rejected; with human_confirmed advances to complete
    assert client.post(
        f"/api/runs/{run_id}/interrupts/{interrupt_id}",
        json={"accept": True, "status": "completed", "human_confirmed": False},
        headers=headers,
    ).status_code == 400
    done = client.post(
        f"/api/runs/{run_id}/interrupts/{interrupt_id}",
        json={"accept": True, "status": "completed", "human_confirmed": True},
        headers=headers,
    )
    assert done.status_code == 200
    assert done.json()["status"] == "complete"

    # CLI immediately reflects completed status too
    assert invoke(parser().parse_args(["--data", str(tmp_path), "show", run_id, "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"


def test_web_dual_auto_handoff_rework_cancel_and_logs(tmp_path):
    def codex_handler(prompt):
        if "Review feedback:" in prompt:
            return BackendResult(
                status="ok",
                reply=Reply(
                    summary="Fixed per feedback",
                    decision="none",
                    files=[Edit(path="solution.py", content="VALUE = 2\n")],
                ),
            )
        return BackendResult(
            status="ok",
            reply=Reply(
                summary="Initial draft",
                decision="none",
                files=[Edit(path="solution.py", content="VALUE = 1\n")],
            ),
        )

    client, _, headers = make_client(tmp_path, codex=StubCodex(codex_handler))
    started = client.post(
        "/api/runs",
        json={"workflow_id": "dual_auto", "task": "Create VALUE in solution.py"},
        headers=headers,
    ).json()
    run_id = started["id"]
    assert started["status"] == "waiting"
    assert started["node"] == "plan"

    # Submit plan -> Codex runs + Seatbelt check passes -> pauses at review
    after_plan = client.post(
        f"/api/runs/{run_id}/interrupts/{started['interrupts'][0]['id']}",
        json={"accept": True, "status": "completed", "result": "Set VALUE=2 in solution.py"},
        headers=headers,
    ).json()
    assert after_plan["status"] == "waiting"
    assert after_plan["node"] == "review"

    # Reject with fail verdict + review_feedback -> Codex reworks automatically -> pauses at review v2
    after_fail = client.post(
        f"/api/runs/{run_id}/interrupts/{after_plan['interrupts'][0]['id']}",
        json={
            "accept": True,
            "status": "completed",
            "result": "Wrong constant",
            "verdict": "fail",
            "review_feedback": "Change VALUE from 1 to 2.",
        },
        headers=headers,
    ).json()
    assert after_fail["node"] == "review"

    # Pass review -> complete
    final = client.post(
        f"/api/runs/{run_id}/interrupts/{after_fail['interrupts'][0]['id']}",
        json={
            "accept": True,
            "status": "completed",
            "result": "Looks good",
            "verdict": "pass",
            "review_feedback": "VALUE == 2 verified.",
        },
        headers=headers,
    ).json()
    assert final["status"] == "complete"

    diff_resp = client.get(f"/api/runs/{run_id}/diff").json()
    assert "VALUE = 2" in diff_resp["diff"]
    report_resp = client.get(f"/api/runs/{run_id}/report").json()
    assert "VALUE == 2 verified." in report_resp["report"]
    logs_resp = client.get(f"/api/logs?run_id={run_id}").json()
    assert len(logs_resp["logs"]) >= 4

    # Cancel a waiting run cleanly
    to_cancel = client.post(
        "/api/runs",
        json={"workflow_id": "dual_auto", "task": "Run to cancel"},
        headers=headers,
    ).json()
    cancelled = client.post(f"/api/runs/{to_cancel['id']}/cancel", json={}, headers=headers).json()
    assert cancelled["status"] == "cancelled"


def test_launcher_one_click_duplicate_and_foreign_port_protection(tmp_path):
    data_dir = tmp_path / "aisys data with spaces"
    port = free_port()
    env = {
        **os.environ,
        "AISYS_DATA": str(data_dir),
        "AISYS_PORT": str(port),
        "AISYS_NO_BROWSER": "1",
        "AISYS_NO_PAUSE": "1",
    }

    # Test 1: One-click start boots healthy verified server
    first = subprocess.run([str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stdout + first.stderr
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["app"] == "aisys"
        first_pid = health["pid"]

        # HTML and JS assets load cleanly from live uvicorn server
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            assert "AIsys" in resp.read().decode("utf-8")

        # Test 2: Duplicate launch detects verified running instance & keeps same PID
        second = subprocess.run([str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=15)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "正在运行" in second.stdout
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            assert json.loads(resp.read().decode("utf-8"))["pid"] == first_pid
    finally:
        stop = subprocess.run(
            [str(REPO_ROOT / ".venv/bin/aisys"), "--data", str(data_dir), "stop"],
            capture_output=True, text=True, timeout=10,
        )
        assert stop.returncode == 0
        # Wait briefly for graceful exit
        for _ in range(25):
            if not (data_dir / "server.json").is_file():
                break
            time.sleep(0.1)

    # Foreign process on explicit port MUST NOT be killed and MUST report port conflict
    foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    foreign.bind(("127.0.0.1", 0))
    foreign.listen(16)
    foreign_port = foreign.getsockname()[1]
    try:
        conflict_env = {**env, "AISYS_DATA": str(tmp_path / "conflict-data"), "AISYS_PORT": str(foreign_port)}
        conflict = subprocess.run([str(LAUNCHER)], env=conflict_env, capture_output=True, text=True, timeout=15)
        assert conflict.returncode != 0
        assert "已被其他非 AIsys 程序占用" in (conflict.stdout + conflict.stderr)

        # Foreign listener is still alive & accepting!
        probe = socket.create_connection(("127.0.0.1", foreign_port), timeout=1)
        probe.close()

        # When default port in server.yaml is occupied by foreign process, auto-fallback cleanly without killing it
        fb_data = tmp_path / "fallback-data"
        svc = AIsysService(fb_data)
        svc.config.save(ServerConfig(port=foreign_port))
        fb_env = {**env, "AISYS_DATA": str(fb_data)}
        fb_env.pop("AISYS_PORT", None)
        fb = subprocess.run([str(LAUNCHER)], env=fb_env, capture_output=True, text=True, timeout=30)
        assert fb.returncode == 0, fb.stdout + fb.stderr
        assert "改用空闲端口" in fb.stdout
        fb_info = json.loads((fb_data / "server.json").read_text(encoding="utf-8"))
        assert fb_info["port"] != foreign_port
        subprocess.run(
            [str(REPO_ROOT / ".venv/bin/aisys"), "--data", str(fb_data), "stop"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    finally:
        foreign.close()


def test_shutdown_requires_confirmation_when_runs_are_active_and_sse_connects(tmp_path):
    from aisys.config import configuration_text, parse_config
    from aisys.state import Run

    client, service, headers = make_client(tmp_path)
    # Prepare a queued ("ready") run on disk without driving it
    Run.create(tmp_path, parse_config(configuration_text("blank")), "Queued job", "general", None)

    # Shutdown without confirm_active is blocked with 409 Conflict
    blocked = client.post("/api/server/shutdown", json={"confirm_active": False}, headers=headers)
    assert blocked.status_code == 409

    # Shutdown with confirm_active + cancel_active cancels queued runs cleanly
    allowed = client.post(
        "/api/server/shutdown",
        json={"confirm_active": True, "cancel_active": True},
        headers=headers,
    )
    assert allowed.status_code == 200
    assert allowed.json()["shutting_down"] is True
    assert allowed.json()["cancelled_active_runs"] is True
    assert service.list_runs_summary()[0]["status"] == "cancelled"

