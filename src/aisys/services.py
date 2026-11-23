"""Unified application service layer shared by Web, CLI, Textual UI, and MCP."""
import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import Field, model_validator

from .backends import Backend, codex_executable, default_backends
from .config import (
    ID_RE,
    Id,
    Model,
    UniqueLoader,
    Workflow,
    available_workflow_names,
    configuration_text,
    custom_workflow_dir,
    dump_workflow_yaml,
    parse_config,
    preset_names,
)
from .engine import Engine
from .process import redact
from .protocol import (
    TaskSubmission,
    build_exchange_snapshot,
    record_exchange_snapshot,
    submit_task,
)
from .state import Run, list_runs
from .workspace import artifacts, git, read_context, relative_file, write_atomic

AISYS_VERSION = "0.1.0"
SENSITIVE_PROJECT_PARTS = {
    ".ssh", ".aws", ".gnupg", ".kube", ".codex", ".gemini", ".config",
}
SENSITIVE_PROJECT_PREFIXES = (
    Path("/System"), Path("/Library"), Path("/etc"), Path("/private/etc"),
    Path("/var"), Path("/private/var"), Path("/usr"), Path("/bin"), Path("/sbin"),
)


class ConflictError(ValueError):
    """Raised on stale configuration revision or active driver lock conflict."""


class NotFoundError(ValueError):
    """Raised when a requested workflow, agent profile, or run does not exist."""


class ServerConfig(Model):
    version: Literal[1] = 1
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    max_concurrent_runs: int = Field(default=2, ge=1, le=8)
    default_project: str = Field(default="", max_length=500)


class AgentProfile(Model):
    id: Id
    name: str = Field(min_length=1, max_length=100)
    role: str = Field(min_length=1, max_length=100)
    backend: Literal["codex", "google"]
    instructions: str = Field(min_length=1, max_length=12_000)
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    write_paths: list[str] = Field(default_factory=list, max_length=32)
    reuse_session: bool = False
    on_unavailable: Literal["stop", "handoff"] = "stop"
    default_space: Id = "main"
    default_timeout: int = Field(default=120, ge=1, le=3600)
    default_retries: int = Field(default=0, ge=0, le=5)
    template: str = Field(default="", max_length=48)
    description: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def validate_profile(self):
        if self.backend == "google" and self.reuse_session:
            raise ValueError("Google backend handoff does not manage provider sessions")
        for path in self.write_paths:
            relative_file(path)
        return self

    def to_workflow_agent(self) -> dict:
        data: dict = {
            "role": self.role,
            "backend": self.backend,
            "instructions": self.instructions,
            "write_paths": list(self.write_paths),
            "reuse_session": self.reuse_session,
            "on_unavailable": self.on_unavailable,
        }
        if self.model:
            data["model"] = self.model
        return data


class AgentLibraryFile(Model):
    version: Literal[1] = 1
    agents: list[AgentProfile] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_unique_ids(self):
        seen: set[str] = set()
        for item in self.agents:
            if item.id in seen:
                raise ValueError(f"duplicate agent id: {item.id}")
            seen.add(item.id)
        return self


AGENT_TEMPLATES: list[AgentProfile] = [
    AgentProfile(
        id="codex_coder",
        name="Codex Coder",
        role="Coder",
        backend="codex",
        instructions=(
            "Implement the requested changes cleanly following the plan, test output, "
            "and review feedback. Return complete UTF-8 file contents for allowed paths."
        ),
        write_paths=["solution.py"],
        default_timeout=120,
        default_retries=0,
        template="coder",
        description="Automated code implementation role backed by ChatGPT/Codex CLI.",
    ),
    AgentProfile(
        id="codex_debugger",
        name="Codex Debugger",
        role="Debugger",
        backend="codex",
        instructions=(
            "Diagnose failing checks and defects from upstream logs and source files. "
            "Apply minimal, reversible fixes to allowed files and document root cause."
        ),
        write_paths=["solution.py"],
        default_timeout=120,
        default_retries=1,
        template="debugger",
        description="Targeted bug-fixing and failure-diagnosis role backed by Codex CLI.",
    ),
    AgentProfile(
        id="codex_tester",
        name="Codex Tester",
        role="Tester",
        backend="codex",
        instructions=(
            "Design comprehensive edge-case assertions and evaluate implementation correctness "
            "against acceptance criteria without modifying source files unless allowed."
        ),
        write_paths=[],
        default_timeout=120,
        default_retries=0,
        template="tester",
        description="Read-only test design and verification analyst backed by Codex CLI.",
    ),
    AgentProfile(
        id="gemini_planner",
        name="Gemini Planner",
        role="Planner",
        backend="google",
        on_unavailable="handoff",
        instructions=(
            "Analyze requirements and propose the smallest testable implementation plan, "
            "assumptions, and acceptance criteria without editing code."
        ),
        write_paths=[],
        default_timeout=120,
        default_retries=0,
        template="planner",
        description="Architecture and task planning role via Antigravity exchange or handoff.",
    ),
    AgentProfile(
        id="gemini_researcher",
        name="Gemini Researcher",
        role="Researcher",
        backend="google",
        on_unavailable="handoff",
        instructions=(
            "Analyze supplied materials and research tradeoffs. Document assumptions, "
            "uncertainties, and claims requiring empirical verification."
        ),
        write_paths=[],
        default_timeout=120,
        default_retries=0,
        template="researcher",
        description="Research and tradeoff analysis role via Antigravity exchange or handoff.",
    ),
    AgentProfile(
        id="gemini_reviewer",
        name="Gemini Reviewer",
        role="Independent Reviewer",
        backend="google",
        on_unavailable="handoff",
        instructions=(
            "Independently review code changes and local test outcomes against the task and plan. "
            "Return pass or fail with concrete, actionable review_feedback."
        ),
        write_paths=[],
        default_timeout=120,
        default_retries=0,
        template="reviewer",
        description="Independent cross-model code reviewer via Antigravity exchange or handoff.",
    ),
]


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _check_revision(current: str | None, expected: str | None) -> None:
    if expected is not None and current is not None and expected != current:
        raise ConflictError(
            "configuration changed on disk since it was loaded; reload before saving"
        )


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class AuditLog:
    """Unified, redacted service and audit log stored inside data/logs/."""

    def __init__(self, data: Path):
        self.data = data.resolve()
        self.path = self.data / "logs" / "aisys-service.jsonl"
        self._lock = threading.Lock()

    def record(
        self,
        category: str,
        message: str,
        *,
        level: Literal["info", "warning", "error"] = "info",
        run_id: str | None = None,
        detail: dict | None = None,
    ) -> dict:
        entry = {
            "id": uuid4().hex[:12],
            "ts": round(time.time(), 3),
            "category": category,
            "level": level,
            "run_id": run_id,
            "message": redact(message)[:1000],
            "detail": _redact_structure(detail) if detail else {},
        }
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            _ensure_private_dir(self.path.parent)
            with self.path.open("ab") as stream:
                stream.write(line)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return entry

    def read_service_entries(self, limit: int = 500) -> list[dict]:
        if not self.path.is_file():
            return []
        entries: list[dict] = []
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return []
        for line in lines[-max(limit * 2, 200):]:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    entries.append(item)
            except ValueError:
                continue
        return entries

    def query(
        self,
        *,
        run_id: str | None = None,
        category: str | None = None,
        level: str | None = None,
        q: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        items: list[dict] = list(self.read_service_entries(limit=limit))
        # Merge persisted SQLite run execution events so CLI and Web share one log history.
        runs = []
        if run_id:
            try:
                runs = [Run.open(self.data, run_id)]
            except ValueError:
                runs = []
        else:
            runs = list_runs(self.data)[:25]
        for run in runs:
            for idx, ev in enumerate(run.events()):
                status = ev.get("status", "ok")
                ev_level = "error" if status in {
                    "failed", "timeout", "rate_limit", "auth", "invalid", "budget",
                } else ("warning" if status in {"interrupted", "rejected", "cancelled"} else "info")
                ev_cat = "agent" if ev.get("backend") in {"codex", "google"} else "workflow"
                summary = ev.get("output") or "; ".join(ev.get("errors", [])) or status
                items.append({
                    "id": f"ev-{run.id[:8]}-{idx}",
                    "ts": round(float(ev.get("started", run.meta.get("created", 0))), 3),
                    "category": ev_cat,
                    "level": ev_level,
                    "run_id": run.id,
                    "message": redact(
                        f"[{ev['node']}] {ev['agent_role']} ({ev['backend']}) → "
                        f"{'running' if ev.get('in_progress') else status}"
                        + (f" / {ev['verdict']}" if ev.get("verdict") != "none" else "")
                    ),
                    "detail": {
                        "node": ev["node"],
                        "visit": ev.get("visit", 1),
                        "attempt": ev.get("attempt", 1),
                        "backend": ev.get("backend"),
                        "duration": ev.get("duration", 0),
                        "usage": ev.get("usage", {}),
                        "output": redact(summary)[:1200],
                        "errors": [redact(e)[:400] for e in ev.get("errors", [])],
                    },
                })
        keyword = (q or "").strip().lower()
        filtered = []
        for entry in items:
            if run_id and entry.get("run_id") != run_id:
                continue
            if category and category != "all" and entry.get("category") != category:
                continue
            if level and level != "all" and entry.get("level") != level:
                continue
            if keyword:
                blob = json.dumps(entry, ensure_ascii=False).lower()
                if keyword not in blob:
                    continue
            filtered.append(entry)
        filtered.sort(key=lambda item: float(item.get("ts", 0)), reverse=True)
        return filtered[: max(1, min(limit, 500))]


def _redact_structure(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _redact_structure(v) for k, v in value.items()}
    return value


class ConfigService:
    """Persistent server preferences and strict project directory validation."""

    def __init__(self, data: Path, audit: AuditLog):
        self.data = data.resolve()
        self.audit = audit
        self.path = self.data / "config" / "server.yaml"
        self._lock = threading.Lock()

    def get(self) -> tuple[ServerConfig, str]:
        with self._lock:
            if self.path.is_file() and not self.path.is_symlink():
                text = self.path.read_text(encoding="utf-8")
                raw = yaml.load(text, Loader=UniqueLoader) or {}
                return ServerConfig.model_validate(raw), _revision(text)
            default = ServerConfig()
            text = yaml.safe_dump(default.model_dump(mode="json"), sort_keys=False)
            return default, _revision(text)

    def save(self, config: ServerConfig, *, expected_revision: str | None = None) -> tuple[ServerConfig, str]:
        if config.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Web server host must be loopback (127.0.0.1 or localhost)")
        if config.default_project:
            self.validate_project_path(config.default_project)
        with self._lock:
            _, current_rev = self._read_unlocked()
            _check_revision(current_rev, expected_revision)
            text = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
            _ensure_private_dir(self.path.parent)
            write_atomic(self.path, text.encode("utf-8"), mode=0o600)
            rev = _revision(text)
        self.audit.record("config", f"Updated server config (port={config.port}, max_concurrent_runs={config.max_concurrent_runs})")
        return config, rev

    def _read_unlocked(self) -> tuple[ServerConfig, str | None]:
        if self.path.is_file() and not self.path.is_symlink():
            text = self.path.read_text(encoding="utf-8")
            raw = yaml.load(text, Loader=UniqueLoader) or {}
            return ServerConfig.model_validate(raw), _revision(text)
        default = ServerConfig()
        text = yaml.safe_dump(default.model_dump(mode="json"), sort_keys=False)
        return default, _revision(text)

    def validate_project_path(self, raw: str) -> dict:
        cleaned = raw.strip()
        if not cleaned or len(cleaned) > 500 or any(ord(c) < 32 for c in cleaned):
            raise ValueError("Invalid project directory path")
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Project path must be an absolute directory path")
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError("Project directory does not exist")
        resolved = candidate.resolve()
        if resolved == Path("/") or resolved == Path.home().resolve():
            raise ValueError("Cannot use filesystem root or home directory directly as a project")
        for prefix in SENSITIVE_PROJECT_PREFIXES:
            if resolved == prefix or resolved.is_relative_to(prefix):
                raise ValueError("System directories cannot be used as project workspaces")
        if any(part.casefold() in SENSITIVE_PROJECT_PARTS for part in resolved.parts):
            raise ValueError("Sensitive credential or agent configuration directory is disallowed")
        commit = git(resolved, "rev-parse", "--verify", "HEAD^{commit}")
        branch = git(resolved, "rev-parse", "--abbrev-ref", "HEAD")
        return {
            "valid": True,
            "path": str(resolved),
            "head": commit,
            "branch": branch,
        }


class AgentService:
    """CRUD manager for reusable logical Agent profiles persisted in data/config/agents.yaml."""

    def __init__(self, data: Path, audit: AuditLog):
        self.data = data.resolve()
        self.audit = audit
        self.path = self.data / "config" / "agents.yaml"
        self._lock = threading.Lock()

    def templates(self) -> list[dict]:
        return [tpl.model_dump(mode="json") for tpl in AGENT_TEMPLATES]

    def _load_unlocked(self) -> tuple[AgentLibraryFile, str]:
        if not self.path.is_file():
            seeded = AgentLibraryFile(agents=[AgentProfile.model_validate(t.model_dump()) for t in AGENT_TEMPLATES])
            text = yaml.safe_dump(seeded.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
            _ensure_private_dir(self.path.parent)
            write_atomic(self.path, text.encode("utf-8"), mode=0o600)
            return seeded, _revision(text)
        if self.path.is_symlink():
            raise ValueError("agents.yaml cannot be a symlink")
        text = self.path.read_text(encoding="utf-8")
        raw = yaml.load(text, Loader=UniqueLoader) or {"version": 1, "agents": []}
        return AgentLibraryFile.model_validate(raw), _revision(text)

    def _write_unlocked(self, library: AgentLibraryFile) -> str:
        text = yaml.safe_dump(library.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        _ensure_private_dir(self.path.parent)
        write_atomic(self.path, text.encode("utf-8"), mode=0o600)
        return _revision(text)

    def list_agents(self) -> dict:
        with self._lock:
            library, rev = self._load_unlocked()
        return {
            "revision": rev,
            "agents": [item.model_dump(mode="json") for item in library.agents],
            "templates": self.templates(),
        }

    def create_agent(self, profile: AgentProfile, *, expected_revision: str | None = None) -> dict:
        with self._lock:
            library, rev = self._load_unlocked()
            _check_revision(rev, expected_revision)
            if any(item.id == profile.id for item in library.agents):
                raise ConflictError(f"agent '{profile.id}' already exists")
            updated = AgentLibraryFile(agents=[*library.agents, profile])
            new_rev = self._write_unlocked(updated)
        self.audit.record("agent", f"Created agent profile '{profile.id}' ({profile.role} / {profile.backend})")
        return {"revision": new_rev, "agent": profile.model_dump(mode="json")}

    def update_agent(self, agent_id: str, profile: AgentProfile, *, expected_revision: str | None = None) -> dict:
        if profile.id != agent_id:
            raise ValueError("agent id in payload must match target id")
        with self._lock:
            library, rev = self._load_unlocked()
            _check_revision(rev, expected_revision)
            replaced = False
            next_agents = []
            for item in library.agents:
                if item.id == agent_id:
                    next_agents.append(profile)
                    replaced = True
                else:
                    next_agents.append(item)
            if not replaced:
                raise NotFoundError(f"agent '{agent_id}' not found")
            new_rev = self._write_unlocked(AgentLibraryFile(agents=next_agents))
        self.audit.record("agent", f"Updated agent profile '{agent_id}' ({profile.role} / {profile.backend})")
        return {"revision": new_rev, "agent": profile.model_dump(mode="json")}

    def delete_agent(self, agent_id: str, *, expected_revision: str | None = None) -> dict:
        with self._lock:
            library, rev = self._load_unlocked()
            _check_revision(rev, expected_revision)
            remaining = [item for item in library.agents if item.id != agent_id]
            if len(remaining) == len(library.agents):
                raise NotFoundError(f"agent '{agent_id}' not found")
            new_rev = self._write_unlocked(AgentLibraryFile(agents=remaining))
        self.audit.record("agent", f"Deleted agent profile '{agent_id}'")
        return {"revision": new_rev, "deleted": agent_id}


def workflow_graph(workflow: Workflow) -> dict:
    """Compute structured visual graph nodes, typed edges, and semantic warnings."""
    nodes = []
    edges = []
    warnings = []
    reachable: set[str] = set()
    queue = [workflow.start]
    while queue:
        current = queue.pop(0)
        if current in reachable or current not in workflow.nodes:
            continue
        reachable.add(current)
        node = workflow.nodes[current]
        for target in [node.next, *node.branches, *(route.to for route in node.routes)]:
            if target != "end" and target not in reachable:
                queue.append(target)

    for name, node in workflow.nodes.items():
        agent = workflow.agents.get(node.agent) if node.agent else None
        nodes.append({
            "id": name,
            "kind": node.kind,
            "is_start": name == workflow.start,
            "reachable": name in reachable,
            "agent": node.agent,
            "agent_role": agent.role if agent else None,
            "backend": agent.backend if agent else ("local" if node.kind == "check" else None),
            "on_unavailable": agent.on_unavailable if agent else None,
            "write_paths": list(agent.write_paths) if agent else [],
            "space": node.space,
            "max_visits": node.max_visits,
            "timeout": node.timeout,
            "retries": node.retries,
            "instruction": node.instruction,
            "files": list(node.files),
            "inputs": list(node.inputs),
            "branches": list(node.branches),
            "argv": list(node.argv),
        })
        if name not in reachable:
            warnings.append(f"Node '{name}' is not reachable from start node '{workflow.start}'.")
        if agent and agent.backend == "google":
            if agent.on_unavailable == "stop":
                has_unavail_route = any(
                    r.when.status == "unavailable" for r in node.routes
                )
                if not has_unavail_route:
                    warnings.append(
                        f"Node '{name}' uses Google backend with on_unavailable='stop'; "
                        "set on_unavailable='handoff' for Antigravity exchange or manual handoff."
                    )
        if node.kind == "parallel":
            for branch in node.branches:
                edges.append({
                    "id": f"{name}::branch::{branch}",
                    "source": name,
                    "target": branch,
                    "kind": "parallel",
                    "label": "parallel",
                })
        if node.next and node.next != "end":
            edges.append({
                "id": f"{name}::next::{node.next}",
                "source": name,
                "target": node.next,
                "kind": "next",
                "label": "next",
            })
        elif node.kind != "parallel" or node.next == "end":
            # Only non-branch nodes connect implicitly to terminal 'end'
            is_branch = any(name in other.branches for other in workflow.nodes.values())
            if not is_branch and node.next == "end":
                edges.append({
                    "id": f"{name}::next::end",
                    "source": name,
                    "target": "end",
                    "kind": "next",
                    "label": "finish",
                })
        for idx, route in enumerate(node.routes, 1):
            cond = route.when.model_dump(exclude_none=True)
            label = ", ".join(f"{k}={v}" for k, v in cond.items())
            edges.append({
                "id": f"{name}::route{idx}::{route.to}",
                "source": name,
                "target": route.to,
                "kind": "route",
                "label": label,
                "condition": cond,
            })

    return {"nodes": nodes, "edges": edges, "warnings": warnings}


class WorkflowService:
    """Unified workflow CRUD over bundled presets and data/config/workflows/*.yaml."""

    def __init__(self, data: Path, audit: AuditLog):
        self.data = data.resolve()
        self.audit = audit
        self._lock = threading.Lock()

    def _custom_path(self, workflow_id: str) -> Path:
        if not ID_RE.fullmatch(workflow_id):
            raise ValueError("workflow ID must match ^[a-z][a-z0-9_]{0,47}$")
        return custom_workflow_dir(self.data) / f"{workflow_id}.yaml"

    def _source(self, workflow_id: str) -> Literal["preset", "preset_override", "custom"]:
        custom = self._custom_path(workflow_id)
        is_preset = workflow_id in preset_names()
        if custom.is_file() and not custom.is_symlink():
            return "preset_override" if is_preset else "custom"
        if is_preset:
            return "preset"
        raise NotFoundError(f"workflow '{workflow_id}' not found")

    def list_workflows(self) -> list[dict]:
        items = []
        for wf_id in available_workflow_names(self.data):
            text = configuration_text(wf_id, data=self.data)
            wf = parse_config(text)
            source = self._source(wf_id)
            backends = sorted({agent.backend for agent in wf.agents.values()})
            graph = workflow_graph(wf)
            items.append({
                "id": wf_id,
                "name": wf.name,
                "source": source,
                "revision": _revision(text),
                "start": wf.start,
                "node_count": len(wf.nodes),
                "agent_count": len(wf.agents),
                "backends": backends,
                "has_google_handoff": any(
                    a.backend == "google" and a.on_unavailable == "handoff"
                    for a in wf.agents.values()
                ),
                "has_google_stop": any(
                    a.backend == "google" and a.on_unavailable == "stop"
                    for a in wf.agents.values()
                ),
                "has_approval": any(n.kind == "approval" for n in wf.nodes.values()),
                "warnings": graph["warnings"],
            })
        return items

    def get_workflow(self, workflow_id: str) -> dict:
        source = self._source(workflow_id)
        text = configuration_text(workflow_id, data=self.data)
        wf = parse_config(text)
        return {
            "id": workflow_id,
            "source": source,
            "revision": _revision(text),
            "workflow": wf.model_dump(mode="json"),
            "yaml": text,
            "graph": workflow_graph(wf),
        }

    def validate(self, *, yaml_text: str | None = None, workflow_data: dict | None = None) -> dict:
        try:
            if yaml_text is not None:
                wf = parse_config(yaml_text)
                normalized = dump_workflow_yaml(wf)
            elif workflow_data is not None:
                wf = Workflow.model_validate(workflow_data)
                normalized = dump_workflow_yaml(wf)
            else:
                raise ValueError("provide either yaml_text or workflow_data")
            graph = workflow_graph(wf)
            return {
                "valid": True,
                "errors": [],
                "warnings": graph["warnings"],
                "workflow": wf.model_dump(mode="json"),
                "yaml": normalized,
                "graph": graph,
            }
        except ValueError as error:
            return {
                "valid": False,
                "errors": [redact(str(error))],
                "warnings": [],
                "workflow": None,
                "yaml": yaml_text or "",
                "graph": None,
            }

    def save(
        self,
        workflow_id: str,
        *,
        yaml_text: str | None = None,
        workflow_data: dict | None = None,
        expected_revision: str | None = None,
    ) -> dict:
        target = self._custom_path(workflow_id)
        if yaml_text is not None:
            wf = parse_config(yaml_text)
            serialized = yaml_text if yaml_text.endswith("\n") else yaml_text + "\n"
        elif workflow_data is not None:
            wf = Workflow.model_validate(workflow_data)
            serialized = dump_workflow_yaml(wf)
        else:
            raise ValueError("provide either yaml_text or workflow_data")

        with self._lock:
            current_rev = None
            if target.is_file() and not target.is_symlink():
                current_rev = _revision(target.read_text(encoding="utf-8"))
            elif workflow_id in preset_names():
                current_rev = _revision(configuration_text(workflow_id))
            _check_revision(current_rev, expected_revision)
            _ensure_private_dir(target.parent)
            write_atomic(target, serialized.encode("utf-8"), mode=0o600)

        self.audit.record("workflow", f"Saved workflow '{workflow_id}' ({wf.name}, {len(wf.nodes)} nodes)")
        return self.get_workflow(workflow_id)

    def duplicate(self, source_id: str, new_id: str, *, new_name: str | None = None) -> dict:
        dest = self._custom_path(new_id)
        if dest.exists() or new_id in preset_names():
            raise ConflictError(f"workflow '{new_id}' already exists")
        source_text = configuration_text(source_id, data=self.data)
        wf = parse_config(source_text)
        if new_name and new_name.strip():
            wf = wf.model_copy(update={"name": new_name.strip()[:100]})
        else:
            wf = wf.model_copy(update={"name": f"{wf.name} (Copy)"[:100]})
        return self.save(new_id, workflow_data=wf.model_dump(mode="json"))

    def delete(self, workflow_id: str) -> dict:
        target = self._custom_path(workflow_id)
        is_preset = workflow_id in preset_names()
        with self._lock:
            if not target.is_file():
                if is_preset:
                    raise ValueError(
                        "Built-in presets cannot be deleted; duplicate into a custom workflow instead"
                    )
                raise NotFoundError(f"workflow '{workflow_id}' not found")
            target.unlink()
        action = "reset_override" if is_preset else "deleted"
        self.audit.record("workflow", f"Workflow '{workflow_id}' {action}")
        return {"id": workflow_id, "action": action}


class BackendInspector:
    """Honest, live inspection of Codex and Google backend readiness."""

    def __init__(self, backends: dict[str, Backend] | None = None):
        self._backends = backends
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_codex: dict | None = None

    def _providers(self) -> dict[str, Backend]:
        return self._backends if self._backends is not None else default_backends()

    def inspect(self, *, active_codex: bool = False, waiting_google: bool = False, force: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            if force or self._cached_codex is None or (now - self._cached_at) > 5.0:
                providers = self._providers()
                codex_backend = providers["codex"]
                health = codex_backend.health()
                exe = codex_executable() if self._backends is None else "injected-codex"
                installed = bool(exe) if health.status != "unavailable" else False
                authenticated = health.status == "ok"
                callable_now = installed and authenticated
                if health.status == "ok":
                    base_state = "callable"
                    detail = "Codex CLI 已安装并通过 ChatGPT 订阅认证，可直接自动执行。"
                elif health.status == "auth":
                    base_state = "installed"
                    detail = "已检测到 Codex CLI，但未通过 ChatGPT 登录认证（不可误判为已接通）。"
                elif health.status == "unavailable":
                    base_state = "unavailable"
                    detail = health.error or "未找到 Codex CLI。"
                elif health.status == "timeout":
                    base_state = "unverified"
                    detail = "Codex 登录状态检查超时，当前可用性未验证。"
                else:
                    base_state = "unverified"
                    detail = health.error or f"Codex 状态未验证（{health.status}）。"
                self._cached_codex = {
                    "backend": "codex",
                    "installed": installed,
                    "authenticated": authenticated,
                    "callable": callable_now,
                    "state": base_state,
                    "executable": exe if installed else None,
                    "detail": detail,
                }
                self._cached_at = now
            codex_info = dict(self._cached_codex)

        codex_info["running"] = active_codex
        if active_codex and codex_info["callable"]:
            codex_info["state"] = "running"

        gemini_app_installed = Path("/Applications/Gemini.app").is_dir()
        google_info = {
            "backend": "google",
            "installed": gemini_app_installed,
            "authenticated": False,
            "callable": False,
            "running": waiting_google,
            "state": "restricted",
            "mode": "antigravity_host_or_manual_handoff",
            "detail": (
                "受 Google 个人订阅服务条款限制，AIsys 不直接从后台调用 Google 模型；"
                "请在官方 Antigravity 客户端通过统一任务交换协议（aisys exchange / MCP）驱动，"
                "或在 Web 交接面板提交已核阅的结果。"
            ),
        }
        return {"codex": codex_info, "google": google_info}


class AIsysService:
    """Single authoritative application service facade for Web, CLI, and TUI."""

    def __init__(
        self,
        data: Path,
        *,
        project_root: Path | None = None,
        backends: dict[str, Backend] | None = None,
        sync_execution: bool = False,
    ):
        self.data = data.resolve()
        _ensure_private_dir(self.data)
        self.project_root = (project_root or Path.cwd()).resolve()
        self.backends = backends
        self.sync_execution = sync_execution
        self.instance_id = uuid4().hex
        self.started_at = time.time()

        self.audit = AuditLog(self.data)
        self.config = ConfigService(self.data, self.audit)
        self.agents = AgentService(self.data, self.audit)
        self.workflows = WorkflowService(self.data, self.audit)
        self.inspector = BackendInspector(backends=backends)

        self._exec_lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._temp_submissions: dict[str, Path] = {}
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sub_lock = threading.Lock()
        self._stop_watcher = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._fingerprints: dict[str, str] = {}
        self._shutdown_callback = None

    def server_file(self) -> Path:
        return self.data / "server.json"

    def write_server_info(self, host: str, port: int) -> dict:
        info = {
            "app": "aisys",
            "version": AISYS_VERSION,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "host": host,
            "port": port,
            "data_dir": str(self.data),
            "project_root": str(self.project_root),
            "started_at": self.started_at,
        }
        write_atomic(
            self.server_file(),
            json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"),
            mode=0o600,
        )
        return info

    def clear_server_info(self) -> None:
        path = self.server_file()
        if not path.is_file():
            return
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if current.get("instance_id") == self.instance_id:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def start_watcher(self) -> None:
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._stop_watcher.clear()
        self._watcher_thread = threading.Thread(
            target=self._watcher_loop, name="aisys-state-watcher", daemon=True,
        )
        self._watcher_thread.start()

    def stop_watcher(self) -> None:
        self._stop_watcher.set()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=2)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._sub_lock:
            self._subscribers.append((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._sub_lock:
            self._subscribers = [(lp, q) for lp, q in self._subscribers if q is not queue]

    def publish(self, event_type: str, payload: dict) -> None:
        message = {"type": event_type, "ts": round(time.time(), 3), "data": payload}
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(self._enqueue_nowait, queue, message)
            except RuntimeError:
                pass

    @staticmethod
    def _enqueue_nowait(queue: asyncio.Queue, message: dict) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass

    def _cleanup_finished(self) -> None:
        with self._exec_lock:
            done_procs = [rid for rid, proc in self._processes.items() if proc.poll() is not None]
            for rid in done_procs:
                self._processes.pop(rid, None)
                temp = self._temp_submissions.pop(rid, None)
                if temp:
                    temp.unlink(missing_ok=True)
            done_threads = [rid for rid, th in self._threads.items() if not th.is_alive()]
            for rid in done_threads:
                self._threads.pop(rid, None)

    def _watcher_loop(self) -> None:
        while not self._stop_watcher.wait(1.0):
            try:
                self._cleanup_finished()
                self._dispatch_queue()
                changed_runs = []
                for run in list_runs(self.data)[:40]:
                    snap = Engine(run, self.backends).snapshot()
                    fp = json.dumps({
                        "status": snap["status"],
                        "node": snap["node"],
                        "active": snap["active"],
                        "control": snap["control"],
                        "events": len(snap["events"]),
                        "in_progress": sum(1 for e in snap["events"] if e.get("in_progress")),
                        "trace": len(snap["trace"]),
                        "interrupts": [i["id"] for i in snap["interrupts"]],
                    }, sort_keys=True)
                    if self._fingerprints.get(run.id) != fp:
                        self._fingerprints[run.id] = fp
                        changed_runs.append(self._run_summary(run, snap))
                if changed_runs:
                    self.publish("runs_changed", {"runs": changed_runs})
            except Exception as error:
                self.audit.record("error", f"State watcher warning: {error}", level="warning")

    def _engine(self, run: Run) -> Engine:
        return Engine(run, self.backends)

    def _run_summary(self, run: Run, view: dict | None = None) -> dict:
        snap = view if view is not None else self._engine(run).snapshot()
        events = snap["events"]
        latest_event = events[-1] if events else None
        active_event = next((ev for ev in reversed(events) if ev.get("in_progress")), None)
        created = float(run.meta.get("created", 0))
        elapsed = round(max(0.0, time.time() - created), 1) if created else 0.0
        return {
            "id": run.id,
            "task_id": snap["task_id"],
            "workflow": snap["workflow"],
            "task": snap["task"],
            "category": snap["category"],
            "source_commit": snap["source_commit"],
            "status": snap["status"],
            "node": snap["node"],
            "reason": snap["reason"],
            "active": snap["active"],
            "control": snap["control"],
            "created": created,
            "elapsed_seconds": elapsed,
            "interrupt_count": len(snap["interrupts"]),
            "interrupt_kinds": [item["kind"] for item in snap["interrupts"]],
            "event_count": len(events),
            "trace_count": len(snap["trace"]),
            "active_agent": (active_event or latest_event or {}).get("agent_role"),
            "active_backend": (active_event or latest_event or {}).get("backend"),
        }

    def list_runs_summary(self, *, status_filter: str | None = None) -> list[dict]:
        self._cleanup_finished()
        items = []
        for run in list_runs(self.data):
            summary = self._run_summary(run)
            if status_filter and status_filter != "all":
                if status_filter == "queued" and summary["status"] != "ready":
                    continue
                elif status_filter == "running" and summary["status"] not in {"running", "stopping"}:
                    continue
                elif status_filter == "waiting" and summary["status"] not in {"waiting", "paused", "recoverable"}:
                    continue
                elif status_filter not in {"queued", "running", "waiting"} and summary["status"] != status_filter:
                    continue
            items.append(summary)
        return items

    def get_run_detail(self, run_id: str) -> dict:
        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        engine = self._engine(run)
        view = engine.snapshot()
        exchange = build_exchange_snapshot(engine, view)
        driver_log_path = run.root / "driver.log"
        driver_log = ""
        if driver_log_path.is_file():
            try:
                driver_log = redact(driver_log_path.read_text(encoding="utf-8", errors="replace")[-8000:])
            except OSError:
                driver_log = ""
        return {
            **self._run_summary(run, view),
            "workflow_definition": run.workflow.model_dump(mode="json"),
            "graph": workflow_graph(run.workflow),
            "results": view["results"],
            "trace": view["trace"],
            "events": view["events"],
            "interrupts": view["interrupts"],
            "exchange": exchange.model_dump(mode="json"),
            "driver_log": driver_log,
        }

    def get_run_diff(self, run_id: str, space: str = "main") -> dict:
        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        spaces = sorted({node.space for node in run.workflow.nodes.values()})
        if space not in spaces:
            raise ValueError(f"unknown workspace '{space}'; available: {', '.join(spaces)}")
        workspace = run.workspace(space)
        changed = artifacts(workspace)
        tracked_diff = git(workspace, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--")
        untracked_raw = git(workspace, "ls-files", "-z", "--others", "--exclude-standard")
        untracked = [
            name for name in untracked_raw.split("\0")
            if name and not name.startswith(".aisys-check/")
        ]
        parts = [tracked_diff] if tracked_diff else []
        for rel in sorted(untracked):
            try:
                content = read_context(workspace, rel, 32_000)
            except ValueError:
                continue
            added = "\n".join(f"+{line}" for line in content.splitlines())
            parts.append(f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n{added}")
        return {
            "run_id": run.id,
            "space": space,
            "spaces": spaces,
            "workspace": str(workspace),
            "changed_paths": changed,
            "diff": redact("\n\n".join(parts)),
        }

    def get_run_report(self, run_id: str) -> dict:
        from .cli import render_report

        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        return {"run_id": run.id, "report": redact(render_report(self._engine(run)))}

    def dashboard(self, *, force_backend_check: bool = False) -> dict:
        runs = self.list_runs_summary()
        active_codex = False
        waiting_google = False
        counts = {
            "queued": 0, "running": 0, "waiting": 0,
            "complete": 0, "failed": 0, "cancelled": 0,
        }
        for item in runs:
            st = item["status"]
            if st == "ready":
                counts["queued"] += 1
            elif st in {"running", "stopping"}:
                counts["running"] += 1
                if item.get("active_backend") == "codex":
                    active_codex = True
            elif st in {"waiting", "paused", "recoverable"}:
                counts["waiting"] += 1
                if "handoff" in item.get("interrupt_kinds", []):
                    waiting_google = True
            elif st in counts:
                counts[st] += 1

        backends = self.inspector.inspect(
            active_codex=active_codex,
            waiting_google=waiting_google,
            force=force_backend_check,
        )
        server_cfg, cfg_rev = self.config.get()
        recent_logs = self.audit.query(limit=20)
        alerts = [log for log in recent_logs if log.get("level") in {"error", "warning"}][:8]
        return {
            "server": {
                "app": "aisys",
                "version": AISYS_VERSION,
                "pid": os.getpid(),
                "instance_id": self.instance_id,
                "data_dir": str(self.data),
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "max_concurrent_runs": server_cfg.max_concurrent_runs,
                "config_revision": cfg_rev,
            },
            "backends": backends,
            "counts": counts,
            "active_runs": [r for r in runs if r["status"] in {"running", "stopping", "ready", "waiting", "paused", "recoverable"}][:12],
            "recent_completed": [r for r in runs if r["status"] == "complete"][:8],
            "recent_failed": [r for r in runs if r["status"] in {"failed", "cancelled"}][:8],
            "workflows": self.workflows.list_workflows(),
            "recent_logs": recent_logs,
            "alerts": alerts,
        }

    def _active_run_count(self) -> int:
        self._cleanup_finished()
        active = 0
        for run in list_runs(self.data)[:50]:
            if run.active():
                active += 1
        with self._exec_lock:
            managed = len(self._processes) + len(self._threads)
        return max(active, managed)

    def _spawn_driver(self, run: Run, *, argv: list[str] | None = None,
                      runner=None, temp_file: Path | None = None) -> None:
        if self.backends is not None:
            fn = runner or (lambda: self._run_engine_drive(run))
            if self.sync_execution:
                fn()
                return
            thread = threading.Thread(target=fn, name=f"aisys-run-{run.id[:8]}", daemon=True)
            with self._exec_lock:
                self._threads[run.id] = thread
            thread.start()
            return

        cmd = argv or [sys.executable, "-m", "aisys", "--data", str(self.data), "resume", run.id]
        log_path = run.root / "driver.log"
        log_file = log_path.open("ab")
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_file.close()
        with self._exec_lock:
            self._processes[run.id] = proc
            if temp_file is not None:
                self._temp_submissions[run.id] = temp_file

    def _run_engine_drive(self, run: Run) -> None:
        try:
            engine = self._engine(run)
            view = engine.drive()
            snap = build_exchange_snapshot(engine, view)
            record_exchange_snapshot(engine, snap, event="driven")
            self.publish("run_update", self._run_summary(run, view))
        except Exception as error:
            self.audit.record("error", f"Run {run.id[:8]} execution error: {error}", level="error", run_id=run.id)

    def _dispatch_queue(self) -> int:
        self._cleanup_finished()
        cfg, _ = self.config.get()
        active = self._active_run_count()
        slots = cfg.max_concurrent_runs - active
        if slots <= 0:
            return 0
        ready_runs = []
        for run in reversed(list_runs(self.data)):
            if run.active():
                continue
            with self._exec_lock:
                if run.id in self._processes or run.id in self._threads:
                    continue
            snap = self._engine(run).snapshot()
            if snap["status"] == "ready":
                ready_runs.append(run)
        started = 0
        for run in ready_runs[:slots]:
            self._spawn_driver(run)
            started += 1
            self.audit.record("task", f"Dispatched queued run {run.id[:8]} ({run.workflow.name})", run_id=run.id)
        return started

    def start_run(
        self,
        *,
        workflow_id: str,
        task: str,
        category: str = "general",
        project: str | None = None,
    ) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", category):
            raise ValueError("category must be a lowercase identifier")
        text = configuration_text(workflow_id, data=self.data)
        wf = parse_config(text)
        project_path: Path | None = None
        if project and project.strip():
            checked = self.config.validate_project_path(project)
            project_path = Path(checked["path"])

        # Guard against accidentally launching unrouted unavailable backends.
        backends = self.inspector.inspect()
        uses_codex = any(a.backend == "codex" for a in wf.agents.values())
        if uses_codex and not backends["codex"]["callable"]:
            has_codex_fallback = any(
                node.agent and wf.agents[node.agent].backend == "codex"
                and any(r.when.status in {"unavailable", "auth"} for r in node.routes)
                for node in wf.nodes.values()
            )
            if not has_codex_fallback:
                raise ValueError(
                    f"Codex backend is not callable ({backends['codex']['state']}): "
                    f"{backends['codex']['detail']}"
                )
        for node_name, node in wf.nodes.items():
            if node.agent and wf.agents[node.agent].backend == "google":
                agent_cfg = wf.agents[node.agent]
                if agent_cfg.on_unavailable == "stop" and not any(
                    r.when.status == "unavailable" for r in node.routes
                ):
                    raise ValueError(
                        f"Google node '{node_name}' has on_unavailable='stop' with no fallback route; "
                        "set on_unavailable='handoff' for Antigravity exchange / manual handoff."
                    )

        run = Run.create(self.data, wf, task, category, project_path)
        self.audit.record(
            "task",
            f"Created run {run.id[:8]} for workflow '{wf.name}'"
            + (f" on project {project_path}" if project_path else ""),
            run_id=run.id,
        )
        self._dispatch_queue()
        # Give fast local runs (like blank or immediate handoff) a brief moment to settle.
        self._wait_briefly(run.id, timeout=0.6)
        detail = self.get_run_detail(run.id)
        self.publish("run_update", self._run_summary(run))
        return detail

    def _wait_briefly(self, run_id: str, timeout: float = 0.5) -> None:
        if self.sync_execution:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._exec_lock:
                proc = self._processes.get(run_id)
                thread = self._threads.get(run_id)
            if proc is None and thread is None:
                return
            if proc is not None and proc.poll() is not None:
                self._cleanup_finished()
                return
            if thread is not None and not thread.is_alive():
                self._cleanup_finished()
                return
            time.sleep(0.05)

    def pause_run(self, run_id: str) -> dict:
        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        snap = self._engine(run).snapshot()
        if snap["status"] in {"complete", "failed", "cancelled"}:
            raise ValueError("run is already terminal")
        run.request("pause")
        self.audit.record("task", f"Requested pause at next node boundary for run {run.id[:8]}", run_id=run.id)
        detail = self.get_run_detail(run.id)
        self.publish("run_update", self._run_summary(run))
        return detail

    def cancel_run(self, run_id: str, *, terminate_active: bool = False) -> dict:
        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        snap = self._engine(run).snapshot()
        if snap["status"] in {"complete", "failed", "cancelled"}:
            raise ValueError("run is already terminal")
        run.request("cancel")
        if terminate_active:
            with self._exec_lock:
                proc = self._processes.get(run.id)
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.audit.record(
            "task",
            f"Requested cancellation for run {run.id[:8]}"
            + (" (sent SIGTERM to active driver)" if terminate_active else ""),
            level="warning",
            run_id=run.id,
        )
        self._wait_briefly(run.id, timeout=0.5)
        detail = self.get_run_detail(run.id)
        self.publish("run_update", self._run_summary(run))
        return detail

    def resume_run(self, run_id: str) -> dict:
        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        if run.active():
            raise ConflictError("run already has an active driver")
        snap = self._engine(run).snapshot()
        if snap["status"] in {"complete", "failed", "cancelled"}:
            raise ValueError("run is already terminal")
        if snap["status"] == "waiting":
            raise ValueError("run is waiting on an approval or handoff interrupt; submit a decision instead")
        self.audit.record("task", f"Resuming run {run.id[:8]} from status '{snap['status']}'", run_id=run.id)
        self._spawn_driver(run)
        self._wait_briefly(run.id, timeout=0.6)
        detail = self.get_run_detail(run.id)
        self.publish("run_update", self._run_summary(run))
        return detail

    def submit_interrupt(
        self,
        run_id: str,
        submission: TaskSubmission,
        *,
        interrupt_id: str | None = None,
    ) -> dict:
        try:
            run = Run.open(self.data, run_id)
        except ValueError as error:
            raise NotFoundError(str(error)) from error
        if run.active():
            raise ConflictError("run already has an active driver")

        engine = self._engine(run)
        before = build_exchange_snapshot(engine)
        if not before.pending_tasks:
            raise ValueError("no pending approval or handoff interrupt exists for this run")
        selected_id = interrupt_id or submission.interrupt_id
        if selected_id is None:
            if len(before.pending_tasks) == 1:
                selected = before.pending_tasks[0]
            else:
                raise ValueError("multiple pending interrupts; specify interrupt_id")
        else:
            matches = [t for t in before.pending_tasks if t.interrupt_id == selected_id]
            if not matches:
                raise NotFoundError(f"pending interrupt '{selected_id}' not found")
            selected = matches[0]

        if submission.task_id is not None and submission.task_id != selected.task_id:
            raise ValueError("submission task_id does not match the pending task")
        if selected.kind == "pause":
            raise ValueError("run is paused; call resume instead of submitting an interrupt decision")
        if selected.kind == "approval" and not submission.human_confirmed:
            raise ValueError("approval gate requires explicit human confirmation")
        if selected.kind == "handoff" and submission.accept:
            if not (submission.result or submission.review_feedback).strip():
                raise ValueError("accepted handoff requires result text or review_feedback")
            for edit in submission.files:
                if (edit.path not in selected.permissions.write_paths
                        or redact(edit.content) != edit.content):
                    raise ValueError("handoff edit is unapproved or contains credential-like text")

        self.audit.record(
            "approval",
            f"Submitted {selected.kind} decision for [{selected.node}] on run {run.id[:8]} "
            f"(status={submission.status}, verdict={submission.verdict})",
            run_id=run.id,
        )

        if self.backends is not None:
            def run_submit() -> None:
                try:
                    submit_task(self._engine(run), submission, interrupt_id=selected.interrupt_id)
                    self.publish("run_update", self._run_summary(run))
                except Exception as error:
                    self.audit.record(
                        "error", f"Interrupt submission failed on {run.id[:8]}: {error}",
                        level="error", run_id=run.id,
                    )

            self._spawn_driver(run, runner=run_submit)
            self._wait_briefly(run.id, timeout=0.6)
        else:
            ex_dir = _ensure_private_dir(run.root / "exchange")
            temp_file = ex_dir / f"web-submit-{uuid4().hex[:8]}.json"
            payload = submission.model_copy(update={"interrupt_id": selected.interrupt_id})
            write_atomic(temp_file, payload.model_dump_json().encode("utf-8"), mode=0o600)
            argv = [
                sys.executable, "-m", "aisys", "--data", str(self.data),
                "exchange", "submit", run.id, "--file", str(temp_file),
                "--interrupt", selected.interrupt_id,
            ]
            if submission.human_confirmed:
                argv.append("--human-confirmed")
            self._spawn_driver(run, argv=argv, temp_file=temp_file)
            self._wait_briefly(run.id, timeout=0.6)

        detail = self.get_run_detail(run.id)
        self.publish("run_update", self._run_summary(run))
        return detail

    def active_runs_for_shutdown(self) -> list[dict]:
        return [
            item for item in self.list_runs_summary()
            if item["status"] in {"running", "stopping", "ready"}
        ]

    def request_shutdown(self, *, confirm_active: bool = False, cancel_active: bool = False) -> dict:
        active = self.active_runs_for_shutdown()
        if active and not confirm_active:
            raise ConflictError(
                f"{len(active)} active or queued run(s) remain; confirm shutdown explicitly"
            )
        if cancel_active:
            for item in active:
                try:
                    self.cancel_run(item["id"], terminate_active=True)
                except ValueError:
                    pass
        self.audit.record(
            "service",
            f"AIsys web service shutdown requested (active={len(active)}, cancel_active={cancel_active})",
            level="warning" if active else "info",
        )
        self.publish("server_shutdown", {"active_count": len(active), "cancel_active": cancel_active})
        callback = self._shutdown_callback
        if callback is not None:
            threading.Timer(0.25, callback).start()
        return {
            "shutting_down": True,
            "active_runs_remaining": len(active) if not cancel_active else 0,
            "cancelled_active_runs": bool(cancel_active and active),
        }
