import asyncio
import json
import sys
import threading

import pytest

from aisys.backends import BackendResult, Edit, Reply, parse_events
from aisys.config import Workflow, configuration_text, parse_config, preset_names
from aisys.engine import Engine
from aisys.process import ProcessResult
from aisys.state import Run
from aisys.ui import Workbench
from aisys.workspace import artifacts, git

DARWIN = pytest.mark.skipif(sys.platform != "darwin", reason="local commands use macOS Seatbelt")


class Stub:
    name = "codex"
    handoff_reason = None

    def __init__(self, handler):
        self.handler = handler
        self.prompts = []

    def run(self, *, prompt, **kwargs):
        self.prompts.append(prompt)
        return self.handler(prompt)


def reply(text, decision="none", content=None):
    edits = [Edit(path="solution.py", content=content)] if content is not None else []
    return BackendResult(status="ok", reply=Reply(summary=text, decision=decision, files=edits))


def workflow(nodes, *, agents=None, budget=None, start="first"):
    return Workflow.model_validate({
        "name": "Test flow", "start": start, "nodes": nodes,
        "agents": agents or {}, "budget": budget or {},
    })


def fake_agent(role="Writer", write=None):
    return {"role": role, "backend": "codex", "instructions": "Follow the configured task.",
            "write_paths": write or []}


def reopen(run):
    return Run.open(run.root.parent, run.id)


def test_all_bundled_configs():
    assert {"software", "research", "independent", "competitive", "blank"} <= set(preset_names())
    for preset in preset_names():
        assert parse_config(configuration_text(preset)).nodes


def test_yaml_rejects_duplicate_keys_and_sensitive_paths():
    with pytest.raises(ValueError, match="duplicate YAML keys"):
        parse_config("name: A\nname: B\n")
    with pytest.raises(ValueError, match="aliases"):
        parse_config("name: &a A\nstart: *a\n")
    for path in ("../outside", ".env", ".git/config", "/outside", "keys/app.pem"):
        with pytest.raises(ValueError):
            workflow({"first": {"kind": "agent", "agent": "codex", "files": [path]}},
                     agents={"codex": fake_agent()})


def test_auth_unavailable_routes_and_handoff_reject(tmp_path):
    conf = workflow({
        "first": {"kind": "agent", "agent": "google",
                  "routes": [{"when": {"status": "unavailable"}, "to": "fallback"}]},
        "fallback": {"kind": "approval", "instruction": "Allow fallback"},
    }, agents={"google": {"role": "Planner", "backend": "google", "instructions": "Plan"}})
    run = Run.create(tmp_path, conf, "sample task", "code", None)
    pending = Engine(run).drive()
    assert pending["status"] == "waiting"
    assert pending["results"]["first"]["status"] == "unavailable"
    assert pending["trace"][0]["rule"] == 1
    assert Engine(reopen(run)).drive(accept=False)["status"] == "failed"


def test_pause_reopen_redirect_and_cancel(tmp_path):
    conf = workflow({
        "first": {"kind": "route", "next": "default",
                  "routes": [{"when": {"category": "risky"}, "to": "gate"}]},
        "default": {"kind": "report"},
        "gate": {"kind": "approval", "next": "default"},
    })
    paused = Run.create(tmp_path, conf, "sample task", "risky", None)
    paused.request("pause")
    assert Engine(paused).drive()["status"] == "paused"
    waiting = Engine(reopen(paused)).drive()
    assert waiting["node"] == "gate"
    assert waiting["status"] == "waiting"
    assert Engine(reopen(paused)).drive(accept=True)["status"] == "complete"

    cancelled = Run.create(tmp_path, conf, "sample task", "risky", None)
    Engine(cancelled).drive()
    cancelled.request("cancel")
    assert Engine(reopen(cancelled)).snapshot()["status"] == "cancelled"
    with pytest.raises(ValueError, match="cancelled"):
        Engine(reopen(cancelled)).drive(accept=True)


def test_parallel_workspace_and_fan_in(tmp_path):
    barrier = threading.Barrier(2, timeout=5)

    def handle(prompt):
        barrier.wait()
        return reply("Candidate ready", content=f"# {prompt.splitlines()[0]}\n")

    backend = Stub(handle)
    conf = workflow({
        "first": {"kind": "parallel", "branches": ["left", "right"], "next": "summary"},
        "left": {"kind": "agent", "agent": "a", "space": "left"},
        "right": {"kind": "agent", "agent": "b", "space": "right"},
        "summary": {"kind": "report", "inputs": ["left", "right"]},
    }, agents={"a": fake_agent("Candidate A", ["solution.py"]),
               "b": fake_agent("Candidate B", ["solution.py"])})
    run = Run.create(tmp_path, conf, "isolated edits", "code", None)
    view = Engine(run, {"codex": backend}).drive()
    assert view["status"] == "complete"
    assert (run.workspace("left") / "solution.py").read_text() == "# Role: Candidate A\n"
    assert (run.workspace("right") / "solution.py").read_text() == "# Role: Candidate B\n"
    assert not git(run.workspace("left"), "remote")
    assert "## left" in view["results"]["summary"]["output"]
    assert "## right" in view["results"]["summary"]["output"]
    assert len(backend.prompts) == 2


def test_parallel_model_budget_is_atomic(tmp_path):
    backend = Stub(lambda _: reply("ok"))
    conf = workflow({
        "first": {"kind": "parallel", "branches": ["left", "right"]},
        "left": {"kind": "agent", "agent": "writer", "space": "left"},
        "right": {"kind": "agent", "agent": "writer", "space": "right"},
    }, agents={"writer": fake_agent()}, budget={"model_calls": 1})
    run = Run.create(tmp_path, conf, "parallel budget", "code", None)
    view = Engine(run, {"codex": backend}).drive()
    assert view["status"] == "failed"
    assert sorted(view["results"][n]["status"] for n in ("left", "right")) == ["budget", "ok"]
    assert len(backend.prompts) == 1


@DARWIN
def test_competitive_pending_handoff_retains_completed_peer(tmp_path):
    def handle(prompt):
        if "Independent reviewer" in prompt:
            assert "test_a" in prompt and "test_b" in prompt
            assert "candidate_a/solution.py" in prompt and "candidate_b/solution.py" in prompt
            return reply("Candidate A meets the tests", "pass")
        return reply("Candidate A", content="def solve(text):\n    return text[::-1]\n")

    from aisys.backends import GoogleBackend

    backend = Stub(handle)
    providers = {"codex": backend, "google": GoogleBackend()}
    run = Run.create(tmp_path, parse_config(configuration_text("competitive")),
                     "Reverse the input string", "code", None)
    pending = Engine(run, providers).drive()
    assert pending["status"] == "waiting"
    assert pending["interrupts"][0]["write_paths"] == ["solution.py"]
    assert len(backend.prompts) == 1
    answer = Reply(summary="Candidate B", decision="none", files=[
        Edit(path="solution.py", content="def solve(text):\n    return text\n"),
    ])
    result = Engine(reopen(run), providers).drive(accept=True, reply_json=answer.model_dump_json())
    assert result["status"] == "complete"
    assert result["results"]["test_a"]["status"] == "ok"
    assert result["results"]["test_b"]["status"] == "failed"
    assert result["results"]["judge"]["verdict"] == "pass"
    assert len(backend.prompts) == 2
    assert artifacts(run.workspace("candidate_a")) == ["solution.py"]


def test_multiple_parallel_interrupts_are_resolved_individually(tmp_path):
    conf = workflow({
        "first": {"kind": "parallel", "branches": ["left", "right"]},
        "left": {"kind": "agent", "agent": "google", "space": "a"},
        "right": {"kind": "agent", "agent": "google", "space": "b"},
    }, agents={"google": {"role": "Planner", "backend": "google",
                          "instructions": "Plan", "on_unavailable": "handoff"}})
    run = Run.create(tmp_path, conf, "parallel handoffs", "general", None)
    first = Engine(run).drive()
    left = next(item["id"] for item in first["interrupts"] if item["node"] == "left")
    assert len(first["interrupts"]) == 2
    later = Engine(reopen(run)).drive(interrupt_id=left, accept=True, response="left result")
    assert [item["node"] for item in later["interrupts"]] == ["right"]
    final = Engine(reopen(run)).drive(accept=True, response="right result")
    assert final["status"] == "complete"
    assert [final["results"][name]["output"] for name in ("left", "right")] == [
        "left result", "right result",
    ]


@DARWIN
def test_check_failure_and_review_revision_with_approval(tmp_path):
    conf = workflow({
        "first": {"kind": "agent", "agent": "writer", "next": "test", "max_visits": 3,
                  "inputs": ["test", "review"]},
        "test": {"kind": "check", "argv": ["python", "-c", "from solution import add; assert add(2, 3) == 5"],
                 "next": "review", "max_visits": 3,
                 "routes": [{"when": {"status": "failed"}, "to": "approval"}]},
        "review": {"kind": "agent", "agent": "reviewer", "max_visits": 2,
                   "routes": [{"when": {"verdict": "fail"}, "to": "approval"}]},
        "approval": {"kind": "approval", "next": "first", "max_visits": 2},
    }, agents={"writer": fake_agent("Writer", ["solution.py"]), "reviewer": fake_agent("Reviewer")})

    def handle(prompt):
        if "Role: Reviewer" in prompt:
            reviewed = "Result review" in backend.prompts[-2]
            return reply("Signed-off" if reviewed else "Document semantics",
                         decision="pass" if reviewed else "fail")
        if "Result review" in prompt or "AssertionError" in prompt:
            return reply("Repaired", content="def add(a, b):\n    return a+b\n")
        return reply("Planted defect", content="def add(a, b):\n    return a+b+1\n")

    backend = Stub(handle)
    run = Run.create(tmp_path, conf, "Review with independent fixture", "code", None)
    assert Engine(run, {"codex": backend}).drive()["node"] == "approval"
    assert Engine(reopen(run), {"codex": backend}).drive(accept=True)["node"] == "approval"
    result = Engine(reopen(run), {"codex": backend}).drive(accept=True)
    assert result["status"] == "complete"
    assert [event["status"] for event in result["events"] if event["node"] == "test"] == [
        "failed", "ok", "ok",
    ]
    assert [event["verdict"] for event in result["events"] if event["node"] == "review"] == [
        "fail", "pass",
    ]


@pytest.mark.parametrize("status", ["timeout", "rate_limit", "auth", "invalid"])
def test_provider_failures_are_bounded(tmp_path, monkeypatch, status):
    monkeypatch.setattr("aisys.engine.time.sleep", lambda _: None)
    backend = Stub(lambda _: BackendResult(status=status, error="fixture"))
    conf = workflow({"first": {"kind": "agent", "agent": "writer", "retries": 2}},
                    agents={"writer": fake_agent()})
    run = Run.create(tmp_path, conf, "failure", "general", None)
    view = Engine(run, {"codex": backend}).drive()
    assert view["status"] == "failed"
    assert len(backend.prompts) == (3 if status in {"timeout", "rate_limit"} else 1)
    assert view["results"]["first"]["status"] == status


def test_reserved_attempt_after_interruption_does_not_reexecute(tmp_path):
    backend = Stub(lambda _: pytest.fail("ambiguous model attempt ran twice"))
    conf = workflow({"first": {"kind": "agent", "agent": "writer"}},
                    agents={"writer": fake_agent()})
    run = Run.create(tmp_path, conf, "interrupted process", "general", None)
    engine = Engine(run, {"codex": backend})
    assert run.begin_effect(1, engine.record("first", conf.nodes["first"]), model_call=True) is None
    result = Engine(reopen(run), {"codex": backend}).drive()
    assert result["results"]["first"]["status"] == "interrupted"


def test_visit_limit_stops_loop(tmp_path):
    conf = workflow({"first": {"kind": "route", "next": "first", "max_visits": 3}})
    run = Run.create(tmp_path, conf, "bounded loop", "general", None)
    result = Engine(run).drive()
    assert result["status"] == "failed"
    assert "Visit limit" in result["reason"]
    assert len(result["trace"]) == 3


def test_codex_jsonl_protocol():
    events = [
        {"type": "thread.started", "thread_id": "f454c22d-11b5-43a9-a36e-88bf07f6f9ba"},
        {"type": "item.completed", "item": {"type": "agent_message",
            "text": Reply(summary="OK", decision="none", files=[]).model_dump_json()}},
        {"type": "turn.completed", "usage": {"input_tokens": 5, "unrecognized": 10}},
    ]
    raw = "\n".join(map(json.dumps, events))
    result = parse_events(ProcessResult(code=0, stdout=raw))
    assert result.status == "ok" and result.reply.summary == "OK"
    assert result.usage == {"input_tokens": 5}
    assert result.session_id == events[0]["thread_id"]
    assert parse_events(ProcessResult(code=0, stdout="garbage")).status == "invalid"
    assert parse_events(ProcessResult(code=1, stderr="HTTP 429")).status == "rate_limit"
    assert parse_events(ProcessResult(code=1, stderr="unauthorized")).status == "auth"
    assert parse_events(ProcessResult(issue="timeout")).status == "timeout"
    assert parse_events(ProcessResult(issue="unavailable")).status == "unavailable"


def test_codex_cli_not_found(monkeypatch):
    from aisys.backends import CodexBackend

    monkeypatch.setattr("aisys.backends.shutil.which", lambda _: None)
    assert CodexBackend().health().status == "unavailable"


def test_workbench_edits_config_and_starts_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def test():
        from textual.widgets import Input, Static, TextArea

        app = Workbench(tmp_path / "runs")
        async with app.run_test(size=(116, 56)) as pilot:
            editor = app.query_one("#yaml", TextArea)
            editor.load_text(editor.text.replace("Blank workflow", "Workbench test"))
            app.query_one("#config-path", Input).value = str(tmp_path / "my.yaml")
            assert await pilot.click("#save")
            assert "Workbench test" in (tmp_path / "my.yaml").read_text()
            app.query_one("#task", Input).value = "Inspect terminal runner"
            app.query_one("#start").scroll_visible(immediate=True)
            assert await pilot.click("#start")
            for _ in range(30):
                await pilot.pause(0.05)
                if "complete" in str(app.query_one("#details", Static).content):
                    break
            assert "Workbench test · complete" in str(app.query_one("#details", Static).content)

    asyncio.run(test())
