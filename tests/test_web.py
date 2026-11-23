import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from fastapi.testclient import TestClient

from aisys.backends import BackendResult, Edit, GoogleBackend, Reply
from aisys.cli import invoke, parser
from aisys.collab import CollabStore, ServerConfig, SubTask, validate_division
from aisys.process import cli_env, run_process
from aisys.web_server import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "AIsys.command"


class StubCollabCodex:
    name = "codex"
    handoff_reason = None

    def __init__(self, *, health_status="ok", fail_first_review=False):
        self.health_status = health_status
        self.fail_first_review = fail_first_review
        self.review_calls = 0
        self.calls = []

    def health(self):
        return BackendResult(status=self.health_status)

    def run_json(self, *, prompt, schema, cwd, timeout, model=None):
        self.calls.append(("json", prompt[:80]))
        if "阶段 1" in prompt:
            return BackendResult(status="ok", duration=0.2, usage={"input_tokens": 100, "output_tokens": 50}), {
                "summary": "Codex initial practical plan",
                "approach": "Focus on runnable examples and edge-case exercises.",
                "strengths": ["Concrete code examples"],
                "risks": ["May need deeper conceptual framing"],
            }
        if "阶段 2" in prompt:
            return BackendResult(status="ok", duration=0.3, usage={"input_tokens": 150, "output_tokens": 80}), {
                "critique_of_peer": "Gemini proposal lacked concrete runnable pitfalls.",
                "adopted_from_peer": "Adopted Gemini's intuitive mental model outline.",
                "summary": "Codex upgraded dual-model learning plan",
                "approach": "Combine Gemini concept foundations with Codex runnable exercises.",
                "subtasks": [
                    {
                        "id": "T1",
                        "title": "Core mental model",
                        "owner": "gemini",
                        "objective": "Explain core concepts clearly.",
                        "owned_paths": ["sections/01_concepts.md"],
                        "depends_on": [],
                        "acceptance": "Clear conceptual overview.",
                    },
                    {
                        "id": "T2",
                        "title": "Runnable examples & pitfalls",
                        "owner": "codex",
                        "objective": "Write runnable examples and pitfall drills.",
                        "owned_paths": ["sections/02_examples.md"],
                        "depends_on": ["T1"],
                        "acceptance": "Includes verified examples.",
                    },
                ],
            }
        if "阶段 5" in prompt:
            self.review_calls += 1
            if self.fail_first_review and self.review_calls == 1:
                return BackendResult(status="ok", duration=0.2), {
                    "verdict": "fail",
                    "summary": "Gemini section T1 needs a concrete analogy.",
                    "findings": ["Missing analogy in sections/01_concepts.md"],
                    "fix_requests": [{"task_id": "T1", "feedback": "Add a concrete library-book analogy."}],
                }
            return BackendResult(status="ok", duration=0.2), {
                "verdict": "pass",
                "summary": "Gemini sections verified accurate and clear.",
                "findings": ["Mental model and analogy are consistent."],
                "fix_requests": [],
            }
        raise AssertionError(f"Unexpected prompt: {prompt[:120]}")

    def run(self, *, prompt, agent, cwd, timeout, session_id=None):
        self.calls.append(("run", prompt[:80]))
        path = agent.write_paths[0]
        content = (
            "# Runnable Examples\n\n```python\ndef demo():\n    return 'updated'\n```\n"
            if "修复意见" in prompt
            else "# Runnable Examples\n\n```python\ndef demo():\n    return 'v1'\n```\n"
        )
        return BackendResult(
            status="ok",
            duration=0.2,
            reply=Reply(summary=f"Completed {path}", decision="none", files=[Edit(path=path, content=content)]),
        )


def make_client(tmp_path, *, codex=None):
    providers = {"codex": codex or StubCollabCodex(), "google": GoogleBackend()}
    store = CollabStore(tmp_path, backends=providers, sync_codex=True)
    app = create_app(store, csrf_token="test-csrf-token", allow_testserver=True)
    return TestClient(app), store, {"X-AIsys-Token": "test-csrf-token"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_mechanical_anti_overlap_validator():
    # Disjoint owners & acyclic dependencies -> valid with 2 waves
    ok = validate_division([
        SubTask(id="T1", title="Concepts", owner="gemini", objective="Explain",
                owned_paths=["sections/01.md"], depends_on=[], acceptance="Clear"),
        SubTask(id="T2", title="Examples", owner="codex", objective="Code",
                owned_paths=["sections/02.md"], depends_on=["T1"], acceptance="Runs"),
    ])
    assert ok.valid is True
    assert ok.waves == [["T1"], ["T2"]]

    # Cross-owner overlapping path -> HARD CONFLICT
    overlap = validate_division([
        SubTask(id="T1", title="A", owner="gemini", objective="A",
                owned_paths=["solution.py"], depends_on=[], acceptance="ok"),
        SubTask(id="T2", title="B", owner="codex", objective="B",
                owned_paths=["solution.py"], depends_on=["T1"], acceptance="ok"),
    ])
    assert overlap.valid is False
    assert any("跨模型所有权冲突" in c for c in overlap.conflicts)

    # Same-owner parallel overlapping path -> HARD CONFLICT
    same_parallel = validate_division([
        SubTask(id="T1", title="A", owner="codex", objective="A",
                owned_paths=["solution.py"], depends_on=[], acceptance="ok"),
        SubTask(id="T2", title="B", owner="codex", objective="B",
                owned_paths=["solution.py"], depends_on=[], acceptance="ok"),
    ])
    assert same_parallel.valid is False
    assert any("同时声明" in c for c in same_parallel.conflicts)

    # Cyclic dependency -> HARD CONFLICT
    cycle = validate_division([
        SubTask(id="T1", title="A", owner="gemini", objective="A",
                owned_paths=["a.py"], depends_on=["T2"], acceptance="ok"),
        SubTask(id="T2", title="B", owner="codex", objective="B",
                owned_paths=["b.py"], depends_on=["T1"], acceptance="ok"),
    ])
    assert cycle.valid is False
    assert any("循环依赖" in c for c in cycle.conflicts)


def test_security_csrf_origin_host_and_honest_backends(tmp_path):
    unauth = StubCollabCodex(health_status="auth")
    client, _, headers = make_client(tmp_path, codex=unauth)

    html = client.get("/")
    assert html.status_code == 200
    assert "test-csrf-token" in html.text
    for asset in ("styles.css", "app.js"):
        assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/static/graph.js").status_code == 404

    assert client.post("/api/sessions", json={"task": "hello"}).status_code == 403
    assert client.post(
        "/api/sessions", json={"task": "hello"}, headers={**headers, "Origin": "https://evil.example"}
    ).status_code == 403
    assert client.post(
        "/api/sessions", json={"task": "hello"}, headers={**headers, "Sec-Fetch-Site": "cross-site"}
    ).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.local"}).status_code == 403
    assert client.post("/api/validate-project", json={"path": "/etc"}, headers=headers).status_code == 400

    backends = client.get("/api/backends?refresh=true").json()
    assert backends["codex"]["state"] == "installed"
    assert backends["codex"]["callable"] is False
    assert backends["google"]["state"] == "restricted"
    assert backends["google"]["callable"] is False


def test_full_five_stage_learn_flow_with_fusion_write_lock_mutual_review_and_study_guide(tmp_path, capsys):
    codex = StubCollabCodex(fail_first_review=True)
    client, _, headers = make_client(tmp_path, codex=codex)

    # Stage 1: Create learn session -> Codex automatically submits its independent proposal
    created = client.post(
        "/api/sessions",
        json={"mode": "learn", "task": "Master Python asyncio fundamentals with practical examples"},
        headers=headers,
    )
    assert created.status_code == 201
    sid = created.json()["id"]
    assert "codex" in created.json()["proposals"]
    assert created.json()["stage"] == "proposing"

    # Submit Gemini Stage 1 proposal -> advances to Stage 2 & triggers Codex critique automatically
    after_g1 = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "propose",
            "payload": {
                "summary": "Gemini conceptual plan",
                "approach": "Start with event loop intuition before coroutines.",
                "strengths": ["Clear mental model"],
                "risks": ["Needs runnable drills"],
            },
        },
        headers=headers,
    ).json()
    assert after_g1["stage"] == "critiquing"
    assert "codex" in after_g1["refined"]
    assert after_g1["refined"]["codex"]["division_check"]["valid"] is True

    # Submit Gemini Stage 2 critique -> advances to Stage 3 (deciding)
    after_g2 = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "critique",
            "payload": {
                "critique_of_peer": "Codex jumped into syntax too quickly.",
                "adopted_from_peer": "Adopted Codex's runnable pitfalls section.",
                "summary": "Gemini refined plan",
                "approach": "Intuition first, then runnable examples.",
                "subtasks": [
                    {
                        "id": "T1",
                        "title": "Event loop intuition",
                        "owner": "gemini",
                        "objective": "Explain event loops & coroutines.",
                        "owned_paths": ["sections/01_concepts.md"],
                        "depends_on": [],
                        "acceptance": "Beginner-friendly.",
                    },
                    {
                        "id": "T2",
                        "title": "Asyncio examples",
                        "owner": "codex",
                        "objective": "Provide runnable asyncio examples.",
                        "owned_paths": ["sections/02_examples.md"],
                        "depends_on": ["T1"],
                        "acceptance": "Clean runnable code.",
                    },
                ],
            },
        },
        headers=headers,
    ).json()
    assert after_g2["stage"] == "deciding"

    # Stage 3: User selects "merge" with fusion tweak notes -> enters "merging"
    decided = client.post(
        f"/api/sessions/{sid}/decide",
        json={
            "choice": "merge",
            "base_model": "gemini",
            "fusion_notes": "Keep Gemini intuition and add a Codex pitfall drill.",
        },
        headers=headers,
    ).json()
    assert decided["stage"] == "merging"

    # Overlapping merged plan MUST be rejected mechanically
    bad_merge = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "merge",
            "payload": {
                "summary": "Bad overlapping plan",
                "approach": "Overlap test",
                "subtasks": [
                    {
                        "id": "T1", "title": "A", "owner": "gemini", "objective": "A",
                        "owned_paths": ["sections/shared.md"], "depends_on": [], "acceptance": "ok",
                    },
                    {
                        "id": "T2", "title": "B", "owner": "codex", "objective": "B",
                        "owned_paths": ["sections/shared.md"], "depends_on": ["T1"], "acceptance": "ok",
                    },
                ],
            },
        },
        headers=headers,
    )
    assert bad_merge.status_code == 400
    assert "冲突" in bad_merge.json()["detail"]

    # Valid disjoint merged plan locks ownership and moves to Stage 4 (executing)
    merged = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "merge",
            "payload": {
                "summary": "Fused non-overlapping plan",
                "approach": "Gemini writes intuition; Codex writes examples & pitfalls.",
                "subtasks": [
                    {
                        "id": "T1", "title": "Event loop intuition", "owner": "gemini",
                        "objective": "Explain event loop clearly.",
                        "owned_paths": ["sections/01_concepts.md"], "depends_on": [], "acceptance": "Clear",
                    },
                    {
                        "id": "T2", "title": "Examples & pitfalls", "owner": "codex",
                        "objective": "Write runnable examples and pitfalls.",
                        "owned_paths": ["sections/02_examples.md"], "depends_on": ["T1"], "acceptance": "Runs",
                    },
                ],
            },
        },
        headers=headers,
    ).json()
    assert merged["stage"] == "executing"
    assert merged["locked_plan"]["source"] == "merged"

    # Stage 4 hard write lock: Gemini CANNOT write into Codex's owned_paths!
    bad_write = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "execute_subtask",
            "payload": {
                "task_id": "T1",
                "summary": "Sneaky write",
                "files": [{"path": "sections/02_examples.md", "content": "hijack"}],
            },
        },
        headers=headers,
    )
    assert bad_write.status_code == 400

    # Gemini completes T1 cleanly -> Codex automatically executes T2 -> enters Stage 5 & Codex reviews T1 (round 1 fails)
    after_t1 = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "execute_subtask",
            "payload": {
                "task_id": "T1",
                "summary": "Wrote concepts section.",
                "files": [{"path": "sections/01_concepts.md", "content": "# Event Loop\n\nCore intuition.\n"}],
            },
        },
        headers=headers,
    ).json()
    assert after_t1["stage"] == "reviewing"
    assert after_t1["executions"]["T2"]["status"] == "completed"
    assert after_t1["reviews"]["codex"]["verdict"] == "fail"

    # Gemini passes Codex's T2 -> because Codex failed Gemini's T1, ONLY T1 returns to needs_fix!
    after_r1 = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "review_peer",
            "payload": {
                "verdict": "pass",
                "summary": "Codex examples look good.",
                "findings": ["Verified runnable snippets."],
                "fix_requests": [],
            },
        },
        headers=headers,
    ).json()
    assert after_r1["stage"] == "executing"
    assert after_r1["executions"]["T1"]["status"] == "needs_fix"
    assert after_r1["executions"]["T2"]["status"] == "completed"
    assert "library-book analogy" in after_r1["executions"]["T1"]["review_feedback"]

    # Gemini fixes T1 -> returns to reviewing (round 2) -> Codex passes -> Gemini passes -> COMPLETED + Study Guide!
    after_fix = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "execute_subtask",
            "payload": {
                "task_id": "T1",
                "summary": "Added concrete library-book analogy.",
                "files": [{"path": "sections/01_concepts.md", "content": "# Event Loop\n\nLibrary-book analogy.\n"}],
            },
        },
        headers=headers,
    ).json()
    assert after_fix["stage"] == "reviewing"
    assert after_fix["reviews"]["codex"]["verdict"] == "pass"

    done = client.post(
        f"/api/sessions/{sid}/gemini",
        json={
            "action": "review_peer",
            "payload": {"verdict": "pass", "summary": "All verified.", "findings": [], "fix_requests": []},
        },
        headers=headers,
    ).json()
    assert done["stage"] == "completed"
    assert done["study_guide_path"] and Path(done["study_guide_path"]).is_file()

    guide = client.get(f"/api/sessions/{sid}/study-guide").json()
    assert "Library-book analogy" in guide["markdown"]
    assert "双模型交叉审查与勘误记录" in guide["markdown"]
    download = client.get(f"/api/sessions/{sid}/study-guide?download=1")
    assert download.status_code == 200
    assert "Library-book analogy" in download.text

    # CLI `aisys collab show` sees exact same completed session (single source of truth!)
    assert invoke(parser().parse_args(["--data", str(tmp_path), "collab", "show", sid])) == 0
    cli_view = json.loads(capsys.readouterr().out)
    assert cli_view["stage"] == "completed"
    assert cli_view["study_guide_path"] == done["study_guide_path"]

    # Real kernel telemetry stream persisted and exposed cleanly via API
    ev_resp = client.get(f"/api/sessions/{sid}/events")
    assert ev_resp.status_code == 200
    events = ev_resp.json()["events"]
    ev_types = {e["event_type"] for e in events}
    assert {
        "session.created",
        "gemini.submitted",
        "codex.step.started",
        "codex.step.completed",
        "division.validated",
        "user.decided",
        "stage.transition",
        "writelock.audit",
        "study_guide.compiled",
    }.issubset(ev_types)
    lock_decisions = {
        e["detail"].get("decision")
        for e in events
        if e["event_type"] == "writelock.audit"
    }
    assert lock_decisions == {"ALLOW", "DENY"}
    assert done["metrics"]["lock_allows"] >= 2
    assert done["metrics"]["lock_denies"] >= 1
    assert done["metrics"]["event_count"] == len(events)

    # Antigravity focus endpoint enforces CSRF and stays side-effect-free in test mode
    assert client.post("/api/focus-antigravity").status_code == 403
    focus = client.post("/api/focus-antigravity", headers=headers)
    assert focus.status_code == 200
    assert focus.json()["reason"] == "test_mode"


def test_run_process_streams_stdout_lines_and_pid_live(tmp_path):
    pids: list[int] = []
    lines: list[str] = []
    script = (
        "import sys, time\n"
        "print('{\"type\":\"thread.started\",\"thread_id\":\"abc\"}', flush=True)\n"
        "time.sleep(0.08)\n"
        "print('{\"type\":\"turn.completed\"}', flush=True)\n"
    )
    res = run_process(
        [sys.executable, "-c", script],
        tmp_path,
        cli_env(),
        5.0,
        on_start=pids.append,
        on_stdout_line=lines.append,
    )
    assert res.code == 0
    assert len(pids) == 1 and pids[0] > 0
    assert len(lines) == 2
    assert '"thread.started"' in lines[0]
    assert '"turn.completed"' in lines[1]


def test_launcher_one_click_duplicate_and_foreign_port_protection(tmp_path):
    data_dir = tmp_path / "aisys studio spaces"
    port = free_port()
    env = {
        **os.environ,
        "AISYS_DATA": str(data_dir),
        "AISYS_PORT": str(port),
        "AISYS_NO_BROWSER": "1",
        "AISYS_NO_PAUSE": "1",
    }

    first = subprocess.run([str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stdout + first.stderr
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["app"] == "aisys"
        first_pid = health["pid"]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
            body = resp.read().decode("utf-8")
            assert "AIsys · 双模型共创工作室" in body
            assert "kernel-dock" in body

        second = subprocess.run([str(LAUNCHER)], env=env, capture_output=True, text=True, timeout=15)
        assert second.returncode == 0, second.stdout + second.stderr
        assert "正在运行" in second.stdout
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
            assert json.loads(resp.read().decode("utf-8"))["pid"] == first_pid
    finally:
        subprocess.run(
            [str(REPO_ROOT / ".venv/bin/aisys"), "--data", str(data_dir), "stop"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        for _ in range(25):
            if not (data_dir / "server.json").is_file():
                break
            time.sleep(0.1)

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
        probe = socket.create_connection(("127.0.0.1", foreign_port), timeout=1)
        probe.close()

        fb_data = tmp_path / "fallback-data"
        CollabStore(fb_data).save_server_config(ServerConfig(port=foreign_port))
        fb_env = {**env, "AISYS_DATA": str(fb_data)}
        fb_env.pop("AISYS_PORT", None)
        fb = subprocess.run([str(LAUNCHER)], env=fb_env, capture_output=True, text=True, timeout=30)
        assert fb.returncode == 0, fb.stdout + fb.stderr
        assert "改用空闲端口" in fb.stdout
        subprocess.run(
            [str(REPO_ROOT / ".venv/bin/aisys"), "--data", str(fb_data), "stop"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    finally:
        foreign.close()


def test_pipeline_customization_drives_real_execution(tmp_path):
    client, store, headers = make_client(tmp_path)

    # 1. Read default pipeline + built-in profiles
    resp = client.get("/api/pipeline")
    assert resp.status_code == 200
    body = resp.json()
    cfg = body["config"]
    assert cfg["shared"]["profile_name"] == "balanced"
    assert {p["id"] for p in body["profiles"]} >= {"balanced", "deep_learn", "strict_audit", "fast_ship"}

    # 2. Custom prompts & personas genuinely inject into rendered prompts
    cfg["shared"]["global_directives"] = "CUSTOM_GLOBAL_DIRECTIVE_XYZ"
    cfg["gemini"]["system_persona"] = "CUSTOM_GEMINI_PERSONA_XYZ"
    cfg["shared"]["study_guide_header"] = "CUSTOM_STUDY_HEADER_XYZ"
    saved = client.put("/api/pipeline", json=cfg, headers=headers)
    assert saved.status_code == 200
    assert (tmp_path / "config" / "pipeline.yaml").is_file()

    # Reject disabling BOTH models simultaneously
    invalid_cfg = json.loads(json.dumps(cfg))
    invalid_cfg["modules"]["gemini_enabled"] = False
    invalid_cfg["modules"]["codex_enabled"] = False
    assert client.put("/api/pipeline", json=invalid_cfg, headers=headers).status_code == 400

    # Verify custom directives & persona show up in actual Gemini Stage 1 prompt
    s1 = client.post(
        "/api/sessions",
        json={"task": "Test custom prompt propagation", "mode": "learn", "auto_start_codex": False},
        headers=headers,
    ).json()
    prompt_g = s1["next_action"]["pending_gemini"][0]["prompt"]
    assert "CUSTOM_GLOBAL_DIRECTIVE_XYZ" in prompt_g
    assert "CUSTOM_GEMINI_PERSONA_XYZ" in prompt_g
    assert len(s1["nodes"]) == 12
    assert set(s1["nodes"]) >= {
        "input_dispatch", "gemini_propose", "codex_propose",
        "division_gate", "writelock_sandbox", "final_delivery",
    }

    # 3. Disable Gemini + skip Stage 2 + auto-decide Stage 3 + skip Stage 5 -> Codex solo auto-completes!
    solo_cfg = json.loads(json.dumps(cfg))
    solo_cfg["modules"]["gemini_enabled"] = False
    solo_cfg["modules"]["codex_enabled"] = True
    solo_cfg["stages"]["enable_critique"] = False
    solo_cfg["stages"]["require_user_decision"] = False
    solo_cfg["stages"]["default_winner"] = "codex"
    solo_cfg["stages"]["enable_mutual_review"] = False
    assert client.put("/api/pipeline", json=solo_cfg, headers=headers).status_code == 200

    solo = client.post(
        "/api/sessions",
        json={"task": "Fast solo codex study note", "mode": "learn", "auto_start_codex": True},
        headers=headers,
    ).json()
    assert solo["stage"] == "completed"
    assert solo["nodes"]["gemini_propose"]["status"] == "skipped"
    assert solo["nodes"]["codex_propose"]["status"] == "completed"
    assert solo["nodes"]["codex_exec"]["status"] == "completed"
    assert solo["nodes"]["final_delivery"]["status"] == "completed"
    guide = client.get(f"/api/sessions/{solo['id']}/study-guide").json()
    assert "CUSTOM_STUDY_HEADER_XYZ" in guide["markdown"]

    # 4. Apply built-in profile & reset
    fast = client.post("/api/pipeline/apply-profile", json={"profile_id": "fast_ship"}, headers=headers)
    assert fast.status_code == 200
    assert fast.json()["config"]["stages"]["enable_critique"] is False
    reset = client.post("/api/pipeline/reset", headers=headers)
    assert reset.status_code == 200
    assert reset.json()["config"]["shared"]["profile_name"] == "balanced"


def test_module_control_individual_and_start_all(tmp_path):
    client, _, headers = make_client(tmp_path)

    # List all 6 modules
    snap = client.get("/api/modules").json()
    mod_ids = {m["id"] for m in snap["modules"]}
    assert mod_ids == {
        "web_kernel", "antigravity_bridge", "codex_engine",
        "mcp_bridge", "sandbox_guard", "storage_workspace",
    }
    assert snap["summary"]["total"] == 6

    # CSRF enforced on module mutations
    assert client.post("/api/modules/start-all").status_code == 403
    assert client.post("/api/modules/codex_engine/toggle", json={"enabled": False}).status_code == 403

    # Individual toggle off -> Codex shows stopped; turning off Gemini too is blocked (400)
    off = client.post("/api/modules/codex_engine/toggle", json={"enabled": False}, headers=headers)
    assert off.status_code == 200
    codex_m = next(m for m in off.json()["snapshot"]["modules"] if m["id"] == "codex_engine")
    assert codex_m["enabled"] is False
    assert codex_m["status"] == "stopped"
    assert client.post("/api/modules/antigravity_bridge/toggle", json={"enabled": False}, headers=headers).status_code == 400

    # Individual probe action on sandbox_guard & codex_engine
    sb = client.post("/api/modules/sandbox_guard/action", json={"action": "probe"}, headers=headers)
    assert sb.status_code == 200
    assert sb.json()["result"]["ok"] is True

    # Master 1-click start-all re-enables all modules and runs real probes (including real MCP stdio handshake!)
    started = client.post("/api/modules/start-all", json={"focus_antigravity": False}, headers=headers)
    assert started.status_code == 200
    res = started.json()
    assert res["ok"] is True
    assert res["results"]["codex_engine"]["status"] == "running"
    assert res["results"]["mcp_bridge"]["status"] == "running"
    assert res["results"]["mcp_bridge"]["tools_count"] >= 16
    assert res["results"]["sandbox_guard"]["status"] == "running"
    assert res["snapshot"]["summary"]["stopped"] == 0
