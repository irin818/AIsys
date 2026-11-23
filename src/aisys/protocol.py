"""Unified Task Exchange Protocol for host-driven dual-model collaboration."""
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .backends import Edit, Reply
from .config import Model, Node, Verdict
from .engine import Engine
from .process import redact
from .workspace import artifacts, read_context, write_atomic

ExchangeStatus = Literal["pending", "submitted", "completed", "rejected", "failed"]
PermissionMode = Literal["read_only", "scoped_write", "approval", "local_check"]


class TaskPermissions(Model):
    mode: PermissionMode
    write_paths: list[str] = Field(default_factory=list)
    requires_verdict: bool = False
    allow_shell: bool = False
    sandbox_policy: str = "aisys_managed_workspace"


class TaskContext(Model):
    run_id: str
    node: str
    space: str
    visit: int = 1
    category: str = "general"
    source_commit: str | None = None
    project_notes: str = ""
    role_instructions: str = ""
    node_instruction: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    upstream_results: dict[str, dict] = Field(default_factory=dict)
    rendered_prompt: str = ""


class TaskEnvelope(Model):
    protocol_version: Literal[1] = 1
    task_id: str
    workflow_id: str
    run_id: str
    interrupt_id: str | None = None
    node: str
    kind: Literal["handoff", "approval", "pause", "agent", "check", "report", "parallel", "route"]
    agent_role: str
    backend: str
    objective: str
    context: TaskContext
    working_directory: str
    permissions: TaskPermissions
    status: ExchangeStatus
    result: dict | None = None
    artifacts: list[str] = Field(default_factory=list)
    review_feedback: str = ""
    error: str = ""
    created_at: float
    updated_at: float


class TaskSubmission(Model):
    protocol_version: Literal[1] = 1
    task_id: str | None = None
    interrupt_id: str | None = None
    accept: bool | None = None
    status: Literal["completed", "rejected", "failed"] = "completed"
    result: str = Field(default="", max_length=32_000)
    verdict: Verdict = "none"
    files: list[Edit] = Field(default_factory=list, max_length=32)
    artifacts: list[str] = Field(default_factory=list, max_length=64)
    review_feedback: str = Field(default="", max_length=12_000)
    error: str = Field(default="", max_length=2_000)
    human_confirmed: bool = False

    @model_validator(mode="after")
    def validate_consistency(self):
        if self.accept is None:
            self.accept = self.status == "completed"
        elif self.accept and self.status != "completed":
            raise ValueError("accept=True requires status='completed'")
        elif not self.accept and self.status == "completed":
            raise ValueError("accept=False requires status='rejected' or 'failed'")
        if not self.accept and self.files:
            raise ValueError("file edits cannot be submitted when rejecting or failing a task")
        return self


class ExchangeSnapshot(Model):
    protocol_version: Literal[1] = 1
    run_id: str
    task_id: str
    workflow_id: str
    status: str
    node: str
    reason: str = ""
    source_commit: str | None = None
    pending_tasks: list[TaskEnvelope] = Field(default_factory=list)
    completed_tasks: list[TaskEnvelope] = Field(default_factory=list)
    workspaces: dict[str, str] = Field(default_factory=dict)
    artifacts: dict[str, list[str]] = Field(default_factory=dict)
    trace: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)


def _node_permissions(engine: Engine, node: Node, kind: str) -> TaskPermissions:
    if kind == "approval":
        return TaskPermissions(mode="approval", write_paths=[], requires_verdict=False)
    if kind == "check":
        return TaskPermissions(
            mode="local_check", write_paths=[], requires_verdict=False,
            allow_shell=False, sandbox_policy="macos_seatbelt_read_only",
        )
    agent = engine.workflow.agents.get(node.agent) if node.agent else None
    write_paths = list(agent.write_paths) if agent else []
    requires_verdict = any(route.when.verdict is not None for route in node.routes)
    return TaskPermissions(
        mode="scoped_write" if write_paths else "read_only",
        write_paths=write_paths,
        requires_verdict=requires_verdict,
        allow_shell=False,
        sandbox_policy="aisys_managed_workspace",
    )


def _collect_context(engine: Engine, node_name: str, view: dict, *,
                     visit: int, rendered_prompt: str = "") -> TaskContext:
    node = engine.workflow.nodes[node_name]
    agent = engine.workflow.agents.get(node.agent) if node.agent else None
    limit = engine.workflow.budget.context_chars
    file_map: dict[str, str] = {}
    for raw in node.files:
        if limit <= 0:
            break
        text = read_context(engine.run.workspace(node.space), raw, limit)
        file_map[raw] = text
        limit -= len(text)
    for item in node.shared_files:
        if limit <= 0:
            break
        text = read_context(engine.run.workspace(item.space), item.path, limit)
        file_map[f"{item.space}/{item.path}"] = text
        limit -= len(text)
    upstream: dict[str, dict] = {}
    for dep in node.inputs:
        outcome = view["results"].get(dep)
        if outcome:
            upstream[dep] = {
                "node": outcome["node"],
                "agent_role": outcome["agent_role"],
                "backend": outcome["backend"],
                "status": outcome["status"],
                "verdict": outcome["verdict"],
                "output": outcome["output"],
                "review_feedback": outcome.get("review_feedback", ""),
                "artifacts": outcome.get("artifacts", []),
                "errors": outcome.get("errors", []),
            }
    return TaskContext(
        run_id=engine.run.id,
        node=node_name,
        space=node.space,
        visit=visit,
        category=view["category"],
        source_commit=view["source_commit"],
        project_notes=redact(engine.workflow.project_notes),
        role_instructions=redact(agent.instructions) if agent else "",
        node_instruction=redact(node.instruction),
        files=file_map,
        upstream_results=upstream,
        rendered_prompt=redact(rendered_prompt),
    )


def _objective(engine: Engine, node: Node) -> str:
    task = engine.run.meta["task"]
    if node.instruction:
        return redact(f"{task}\n\nNode objective: {node.instruction}")
    return redact(task)


def _map_result_status(status: str) -> ExchangeStatus:
    if status == "ok":
        return "completed"
    if status == "rejected":
        return "rejected"
    return "failed"


def _visit_counts(view: dict) -> dict[str, int]:
    event_counts: dict[str, int] = {}
    for item in view.get("events", []):
        node_name = item["node"]
        event_counts[node_name] = max(event_counts.get(node_name, 0), int(item.get("visit", 1)))
    trace_counts: dict[str, int] = {}
    for item in view.get("trace", []):
        node_name = item["node"]
        trace_counts[node_name] = trace_counts.get(node_name, 0) + 1
    return {
        node_name: max(event_counts.get(node_name, 0), trace_counts.get(node_name, 0))
        for node_name in set(event_counts) | set(trace_counts)
    }


def build_exchange_snapshot(engine: Engine, view: dict | None = None) -> ExchangeSnapshot:
    view = view if view is not None else engine.snapshot()
    now = time.time()
    created = float(engine.run.meta.get("created", now))
    visits = _visit_counts(view)
    pending_tasks: list[TaskEnvelope] = []
    for item in view["interrupts"]:
        node_name = item["node"]
        node = engine.workflow.nodes[node_name]
        visit = int(item.get("visit") or visits.get(node_name, 0) + 1)
        space = item.get("space") or node.space
        kind = item["kind"]
        task_id = item.get("task_id") or f"{view['task_id']}:{node_name}:v{visit}"
        pending_tasks.append(TaskEnvelope(
            task_id=task_id,
            workflow_id=engine.workflow.name,
            run_id=engine.run.id,
            interrupt_id=item["id"],
            node=node_name,
            kind=kind,
            agent_role=item.get("role") or kind,
            backend=item.get("backend") or "local",
            objective=_objective(engine, node),
            context=_collect_context(
                engine, node_name, view, visit=visit, rendered_prompt=item.get("prompt", ""),
            ),
            working_directory=str(engine.run.workspace(space)),
            permissions=_node_permissions(engine, node, kind),
            status="pending",
            result=None,
            artifacts=[],
            review_feedback="",
            error="",
            created_at=created,
            updated_at=now,
        ))

    ordered_nodes: list[str] = []
    for step in view.get("trace", []):
        if step["node"] in view["results"] and step["node"] not in ordered_nodes:
            ordered_nodes.append(step["node"])
    for node_name in view["results"]:
        if node_name not in ordered_nodes:
            ordered_nodes.append(node_name)

    completed_tasks: list[TaskEnvelope] = []
    for node_name in ordered_nodes:
        outcome = view["results"][node_name]
        node = engine.workflow.nodes[node_name]
        visit = visits.get(node_name, 1)
        completed_tasks.append(TaskEnvelope(
            task_id=f"{outcome['task_id']}:{node_name}:v{visit}",
            workflow_id=engine.workflow.name,
            run_id=engine.run.id,
            interrupt_id=None,
            node=node_name,
            kind=node.kind,
            agent_role=outcome["agent_role"],
            backend=outcome["backend"],
            objective=_objective(engine, node),
            context=_collect_context(engine, node_name, view, visit=visit),
            working_directory=str(engine.run.workspace(outcome["space"])),
            permissions=_node_permissions(engine, node, node.kind),
            status=_map_result_status(outcome["status"]),
            result={
                "status": outcome["status"],
                "verdict": outcome["verdict"],
                "output": outcome["output"],
                "duration": outcome["duration"],
                "usage": outcome["usage"],
                "exit_code": outcome["exit_code"],
                "attempt": outcome["attempt"],
                "session_id": outcome["session_id"],
            },
            artifacts=list(outcome.get("artifacts", [])),
            review_feedback=outcome.get("review_feedback", ""),
            error="; ".join(outcome.get("errors", [])),
            created_at=created,
            updated_at=now,
        ))

    spaces = sorted({node.space for node in engine.workflow.nodes.values()})
    workspace_map = {space: str(engine.run.workspace(space)) for space in spaces}
    artifact_map = {space: artifacts(engine.run.workspace(space)) for space in spaces}

    return ExchangeSnapshot(
        run_id=engine.run.id,
        task_id=view["task_id"],
        workflow_id=engine.workflow.name,
        status=view["status"],
        node=view["node"],
        reason=view["reason"],
        source_commit=view["source_commit"],
        pending_tasks=pending_tasks,
        completed_tasks=completed_tasks,
        workspaces=workspace_map,
        artifacts=artifact_map,
        trace=view["trace"],
        events=view["events"],
    )


def _exchange_dir(engine: Engine) -> Path:
    directory = engine.run.root / "exchange"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def record_exchange_snapshot(engine: Engine, snapshot: ExchangeSnapshot, *,
                             event: str, submission: TaskSubmission | None = None) -> None:
    directory = _exchange_dir(engine)
    write_atomic(
        directory / "snapshot.json",
        snapshot.model_dump_json(indent=2).encode("utf-8"),
        mode=0o600,
    )
    entry: dict = {
        "ts": time.time(),
        "event": event,
        "run_id": snapshot.run_id,
        "status": snapshot.status,
        "node": snapshot.node,
        "pending_task_ids": [task.task_id for task in snapshot.pending_tasks],
    }
    if submission is not None:
        entry["submission"] = {
            "task_id": submission.task_id,
            "interrupt_id": submission.interrupt_id,
            "status": submission.status,
            "verdict": submission.verdict,
            "file_paths": [edit.path for edit in submission.files],
            "has_review_feedback": bool(submission.review_feedback.strip()),
            "error": redact(submission.error)[:200],
        }
    history_path = directory / "history.jsonl"
    existing = history_path.read_bytes() if history_path.is_file() else b""
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    write_atomic(history_path, existing + line, mode=0o600)


def submit_task(engine: Engine, submission: TaskSubmission, *,
                interrupt_id: str | None = None) -> ExchangeSnapshot:
    before = build_exchange_snapshot(engine)
    if not before.pending_tasks:
        raise ValueError("no pending exchange task exists for this run")
    selected_id = interrupt_id or submission.interrupt_id
    if selected_id is None:
        if len(before.pending_tasks) == 1:
            selected = before.pending_tasks[0]
        else:
            raise ValueError("multiple pending tasks; specify interrupt_id")
    else:
        matches = [task for task in before.pending_tasks if task.interrupt_id == selected_id]
        if not matches:
            raise ValueError("pending interrupt_id not found")
        selected = matches[0]

    if submission.task_id is not None and submission.task_id != selected.task_id:
        raise ValueError("submission task_id does not match the pending task")
    if selected.kind == "pause":
        raise ValueError("run is paused; use exchange resume instead of submit")
    if selected.kind == "approval" and not submission.human_confirmed:
        raise ValueError(
            "approval gate requires explicit human confirmation (human_confirmed=true)"
        )

    if selected.kind == "handoff" and submission.accept:
        summary = (submission.result or submission.review_feedback).strip()
        if not summary:
            raise ValueError("accepted handoff submission requires result or review_feedback")
        reply = Reply(
            summary=redact(summary),
            decision=submission.verdict,
            files=submission.files,
        )
        view = engine.drive(
            interrupt_id=selected.interrupt_id,
            accept=True,
            reply_json=reply.model_dump_json(),
            review_feedback=submission.review_feedback,
        )
    elif selected.kind == "handoff":
        view = engine.drive(
            interrupt_id=selected.interrupt_id,
            accept=False,
            status="failed" if submission.status == "failed" else "rejected",
            review_feedback=submission.review_feedback,
            error=submission.error,
        )
    else:
        # Human-confirmed approval gate
        if submission.files:
            raise ValueError("approval gates do not accept file edits")
        view = engine.drive(
            interrupt_id=selected.interrupt_id,
            accept=bool(submission.accept),
            response=submission.result if submission.accept else "",
            verdict=submission.verdict if submission.accept else "none",
            review_feedback=submission.review_feedback,
            error=submission.error,
            status="ok" if submission.accept else (
                "failed" if submission.status == "failed" else "rejected"
            ),
        )

    after = build_exchange_snapshot(engine, view)
    recorded = submission.model_copy(update={
        "task_id": selected.task_id,
        "interrupt_id": selected.interrupt_id,
    })
    record_exchange_snapshot(engine, after, event="submitted", submission=recorded)
    return after


def exchange_schemas() -> dict:
    return {
        "protocol_version": 1,
        "TaskEnvelope": TaskEnvelope.model_json_schema(),
        "TaskSubmission": TaskSubmission.model_json_schema(),
        "ExchangeSnapshot": ExchangeSnapshot.model_json_schema(),
    }
