import json
import subprocess
import sys
from pathlib import Path

import pytest

from aisys.backends import BackendResult, Edit, GoogleBackend, Reply
from aisys.cli import invoke, parser
from aisys.config import Workflow, configuration_text, parse_config, preset_names
from aisys.engine import Engine
from aisys.mcp_server import AIsysMCPServer
from aisys.protocol import (
    TaskSubmission,
    build_exchange_snapshot,
    exchange_schemas,
    submit_task,
)
from aisys.state import Run

DARWIN = pytest.mark.skipif(sys.platform != "darwin", reason="local commands use macOS Seatbelt")
REQUIRED_ENVELOPE_FIELDS = {
    "task_id", "workflow_id", "agent_role", "backend", "objective",
    "context", "working_directory", "permissions", "status", "result",
    "artifacts", "review_feedback", "error",
}


class StubCodex:
    name = "codex"
    handoff_reason = None

    def __init__(self, handler):
        self.handler = handler
        self.prompts = []

    def health(self):
        return BackendResult(status="ok")

    def run(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.handler(prompt)


def make_reply(summary, decision="none", files=None):
    return BackendResult(
        status="ok",
        reply=Reply(summary=summary, decision=decision, files=files or []),
    )


def test_dual_auto_preset_and_exchange_schemas():
    assert "dual_auto" in preset_names()
    wf = parse_config(configuration_text("dual_auto"))
    assert wf.start == "plan"
    schemas = exchange_schemas()
    assert schemas["protocol_version"] == 1
    envelope_props = set(schemas["TaskEnvelope"]["properties"])
    assert REQUIRED_ENVELOPE_FIELDS <= envelope_props


@DARWIN
def test_dual_auto_closed_loop_with_review_feedback_rework(tmp_path):
    def codex_handler(prompt):
        if "Review feedback:" in prompt and "handle negative" in prompt:
            return make_reply(
                "Fixed negative input handling",
                files=[Edit(
                    path="solution.py",
                    content=(
                        "def double_positive(n):\n"
                        "    if n < 0:\n"
                        "        raise ValueError('negative')\n"
                        "    return n * 2\n"
                    ),
                )],
            )
        return make_reply(
            "Initial implementation",
            files=[Edit(
                path="solution.py",
                content="def double_positive(n):\n    return n * 2\n",
            )],
        )

    codex = StubCodex(codex_handler)
    providers = {"codex": codex, "google": GoogleBackend()}
    run = Run.create(
        tmp_path, parse_config(configuration_text("dual_auto")),
        "Implement double_positive(n) in solution.py; raise ValueError for negative n.",
        "code", None,
    )
    engine = Engine(run, providers)
    view = engine.drive()
    snap = build_exchange_snapshot(engine, view)
    assert snap.status == "waiting"
    assert len(snap.pending_tasks) == 1
    plan_task = snap.pending_tasks[0]
    assert REQUIRED_ENVELOPE_FIELDS <= set(plan_task.model_dump().keys())
    assert plan_task.node == "plan"
    assert plan_task.agent_role == "Planner"
    assert plan_task.backend == "google"
    assert plan_task.permissions.mode == "read_only"
    assert plan_task.status == "pending"

    # Submit Antigravity plan -> automatically runs Codex implement + Seatbelt test -> pauses at review
    after_plan = submit_task(
        Engine(Run.open(tmp_path, run.id), providers),
        TaskSubmission(
            task_id=plan_task.task_id,
            result="Implement double_positive(n) raising ValueError for n < 0.",
        ),
    )
    assert after_plan.status == "waiting"
    assert len(codex.prompts) == 1
    assert after_plan.pending_tasks[0].node == "review"
    review_task = after_plan.pending_tasks[0]
    assert review_task.permissions.requires_verdict is True
    assert "def double_positive" in review_task.context.files["solution.py"]
    assert review_task.context.upstream_results["test"]["status"] == "ok"

    # Reviewer fails with explicit review_feedback -> automatically loops back to Codex -> re-tests -> review
    after_fail_review = submit_task(
        Engine(Run.open(tmp_path, run.id), providers),
        TaskSubmission(
            task_id=review_task.task_id,
            result="Missing negative check.",
            verdict="fail",
            review_feedback="Must handle negative inputs by raising ValueError.",
        ),
    )
    assert after_fail_review.status == "waiting"
    assert len(codex.prompts) == 2
    assert "Review feedback:\nMust handle negative inputs by raising ValueError." in codex.prompts[1]
    review_task_2 = after_fail_review.pending_tasks[0]
    assert review_task_2.node == "review"
    assert "raise ValueError" in review_task_2.context.files["solution.py"]

    # Reviewer approves -> completes workflow and persists exchange audit trail
    final = submit_task(
        Engine(Run.open(tmp_path, run.id), providers),
        TaskSubmission(
            task_id=review_task_2.task_id,
            result="Implementation verified.",
            verdict="pass",
            review_feedback="Negative input handling and return value verified.",
        ),
    )
    assert final.status == "complete"
    assert final.pending_tasks == []
    assert final.artifacts["main"] == ["solution.py"]
    review_completed = next(t for t in final.completed_tasks if t.node == "review")
    assert review_completed.review_feedback == "Negative input handling and return value verified."
    assert review_completed.status == "completed"
    assert (run.root / "exchange" / "snapshot.json").is_file()
    history_lines = (run.root / "exchange" / "history.jsonl").read_text().splitlines()
    assert len(history_lines) == 3


def test_exchange_guardrails_approval_task_id_and_write_paths(tmp_path):
    wf = Workflow.model_validate({
        "name": "Guardrail flow",
        "start": "gate",
        "agents": {
            "writer": {
                "role": "Writer",
                "backend": "google",
                "on_unavailable": "handoff",
                "instructions": "Write allowed.py",
                "write_paths": ["allowed.py"],
            },
        },
        "nodes": {
            "gate": {"kind": "approval", "instruction": "Human must confirm", "next": "edit"},
            "edit": {"kind": "agent", "agent": "writer", "next": "end"},
        },
    })
    run = Run.create(tmp_path, wf, "Check guardrails", "general", None)
    engine = Engine(run)
    engine.drive()
    snap = build_exchange_snapshot(engine)
    gate_task = snap.pending_tasks[0]
    assert gate_task.kind == "approval"
    assert gate_task.permissions.mode == "approval"

    # Approval gate refuses submission without explicit human_confirmed=True
    with pytest.raises(ValueError, match="human confirmation"):
        submit_task(Engine(Run.open(tmp_path, run.id)), TaskSubmission(result="auto"))

    # Mismatched task_id is rejected
    with pytest.raises(ValueError, match="task_id"):
        submit_task(
            Engine(Run.open(tmp_path, run.id)),
            TaskSubmission(task_id="wrong-task-id", human_confirmed=True),
        )

    # Human confirmation advances to handoff node
    after_gate = submit_task(
        Engine(Run.open(tmp_path, run.id)),
        TaskSubmission(task_id=gate_task.task_id, human_confirmed=True),
    )
    edit_task = after_gate.pending_tasks[0]
    assert edit_task.node == "edit"
    assert edit_task.permissions.mode == "scoped_write"
    assert edit_task.permissions.write_paths == ["allowed.py"]

    # Unapproved write path is rejected before mutating files
    with pytest.raises(ValueError, match="unapproved"):
        submit_task(
            Engine(Run.open(tmp_path, run.id)),
            TaskSubmission(
                task_id=edit_task.task_id,
                result="Attempt disallowed write",
                files=[Edit(path="disallowed.py", content="x = 1\n")],
            ),
        )
    assert not (run.workspace("main") / "disallowed.py").exists()

    # Allowed write path succeeds atomically
    done = submit_task(
        Engine(Run.open(tmp_path, run.id)),
        TaskSubmission(
            task_id=edit_task.task_id,
            result="Wrote allowed.py",
            files=[Edit(path="allowed.py", content="VALUE = 42\n")],
        ),
    )
    assert done.status == "complete"
    assert (run.workspace("main") / "allowed.py").read_text() == "VALUE = 42\n"


def test_cli_exchange_roundtrip_and_pause_resume(tmp_path, capsys):
    wf_path = tmp_path / "flow.yaml"
    wf_path.write_text(
        "version: 1\n"
        "name: CLI exchange test\n"
        "start: plan\n"
        "agents:\n"
        "  planner:\n"
        "    role: Planner\n"
        "    backend: google\n"
        "    on_unavailable: handoff\n"
        "    instructions: Plan the task.\n"
        "nodes:\n"
        "  plan:\n"
        "    kind: agent\n"
        "    agent: planner\n"
        "    next: done\n"
        "  done:\n"
        "    kind: report\n"
        "    inputs: [plan]\n",
        encoding="utf-8",
    )
    data = tmp_path / "runs"
    rc = invoke(parser().parse_args([
        "--data", str(data), "exchange", "start", str(wf_path), "--task", "Check CLI exchange",
    ]))
    assert rc == 0
    started = json.loads(capsys.readouterr().out)
    assert started["status"] == "waiting"
    run_id = started["run_id"]
    pending_task = started["pending_tasks"][0]

    assert invoke(parser().parse_args(["--data", str(data), "exchange", "pending", run_id])) == 0
    pending_out = json.loads(capsys.readouterr().out)
    assert pending_out["pending_tasks"][0]["task_id"] == pending_task["task_id"]

    sub_file = tmp_path / "sub.json"
    sub_file.write_text(json.dumps({
        "task_id": pending_task["task_id"],
        "result": "Plan completed cleanly.",
        "verdict": "none",
    }), encoding="utf-8")
    assert invoke(parser().parse_args([
        "--data", str(data), "exchange", "submit", run_id, "--file", str(sub_file),
    ])) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "complete"

    assert invoke(parser().parse_args(["--data", str(data), "exchange", "inspect", run_id])) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["status"] == "complete"
    assert any(t["node"] == "plan" and t["status"] == "completed" for t in inspected["completed_tasks"])


def test_mcp_server_in_process_and_stdio_bridge(tmp_path):
    codex = StubCodex(lambda _: make_reply("Implemented", files=[Edit(path="solution.py", content="x = 1\n")]))
    server = AIsysMCPServer(tmp_path, backends={"codex": codex, "google": GoogleBackend()})

    init = server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"},
    })
    assert init["result"]["serverInfo"]["name"] == "aisys-bridge"
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tools = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = {tool["name"] for tool in tools["result"]["tools"]}
    assert {"aisys_start_run", "aisys_submit_task_result", "aisys_get_diff", "aisys_get_report"} <= tool_names

    started_resp = server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "aisys_start_run", "arguments": {"workflow": "blank", "task": "MCP smoke"}},
    })
    assert started_resp["result"]["isError"] is False
    started = json.loads(started_resp["result"]["content"][0]["text"])
    assert started["status"] == "complete"

    # Real stdio smoke test against the workspace plugin launcher script
    script = Path(__file__).resolve().parents[1] / ".agents/plugins/aisys-bridge/scripts/run-mcp.sh"
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "aisys_list_presets", "arguments": {}}}),
    ]) + "\n"
    proc = subprocess.run(
        [str(script)],
        input=requests,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["id"] == 1
    presets_payload = json.loads(lines[1]["result"]["content"][0]["text"])
    assert "dual_auto" in presets_payload["presets"]
