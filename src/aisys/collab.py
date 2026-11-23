"""5-Stage Dual-Model Co-Creation Engine (Gemini/Antigravity + Codex).

Workflow:
  1. Independent Initial Proposals (Gemini || Codex)
  2. Cross-Critique, Upgraded Plans & Structured Non-Overlapping Division
  3. User Decision (Adopt Gemini / Adopt Codex / Fuse & Tweak)
  4. Non-Overlapping Collaborative Execution (hard owned_paths write locks)
  5. Mutual Cross-Review (Gemini audits Codex, Codex audits Gemini) + Delivery
     - dev mode: isolated Git workspace diff + summary
     - learn mode: compiled Structured Markdown Study Guide (study-guide.md)
"""
import asyncio
import fcntl
import inspect
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import yaml
from pydantic import Field, model_validator

from .backends import (
    Agent,
    Backend,
    CodexBackend,
    Edit,
    codex_executable,
    default_backends,
)
from .config import Model, UniqueLoader
from .pipeline_config import (
    BUILTIN_PROFILES,
    ModuleController,
    PipelineConfig,
    PipelineConfigStore,
    safe_format,
)
from .process import redact
from .workspace import (
    apply_edits,
    artifacts,
    git,
    prepare_spaces,
    read_context,
    relative_file,
    run_check,
    write_atomic,
)

AISYS_VERSION = "0.2.0"
Mode = Literal["dev", "learn"]
Owner = Literal["gemini", "codex"]
Stage = Literal[
    "proposing",
    "critiquing",
    "deciding",
    "merging",
    "executing",
    "reviewing",
    "completed",
    "failed",
    "cancelled",
]

SENSITIVE_PROJECT_PARTS = {
    ".ssh", ".aws", ".gnupg", ".kube", ".codex", ".gemini", ".config",
}
SENSITIVE_PROJECT_PREFIXES = (
    Path("/System"), Path("/Library"), Path("/etc"), Path("/private/etc"),
    Path("/var"), Path("/private/var"), Path("/usr"), Path("/bin"), Path("/sbin"),
)
TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")

PROPOSAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "approach": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "approach", "strengths", "risks"],
}

REFINED_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "critique_of_peer": {"type": "string"},
        "adopted_from_peer": {"type": "string"},
        "summary": {"type": "string"},
        "approach": {"type": "string"},
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "owner": {"type": "string", "enum": ["gemini", "codex"]},
                    "objective": {"type": "string"},
                    "owned_paths": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {"type": "string"},
                },
                "required": [
                    "id", "title", "owner", "objective", "owned_paths", "depends_on", "acceptance",
                ],
            },
        },
    },
    "required": [
        "critique_of_peer", "adopted_from_peer", "summary", "approach", "subtasks",
    ],
}

MUTUAL_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "fix_requests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "task_id": {"type": "string"},
                    "feedback": {"type": "string"},
                },
                "required": ["task_id", "feedback"],
            },
        },
    },
    "required": ["verdict", "summary", "findings", "fix_requests"],
}


class ConflictError(ValueError):
    """Raised on active lock conflict or incompatible state transition."""


class NotFoundError(ValueError):
    """Raised when a session or resource does not exist."""


class ServerConfig(Model):
    version: Literal[1] = 1
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    default_project: str = Field(default="", max_length=500)


class SubTask(Model):
    id: str = Field(min_length=1, max_length=24)
    title: str = Field(min_length=1, max_length=120)
    owner: Owner
    objective: str = Field(min_length=1, max_length=4000)
    owned_paths: list[str] = Field(min_length=1, max_length=16)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    acceptance: str = Field(min_length=1, max_length=1000)
    check_argv: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_subtask(self):
        if not TASK_ID_RE.fullmatch(self.id):
            raise ValueError(f"invalid subtask id '{self.id}'")
        cleaned_paths = []
        seen: set[str] = set()
        for raw in self.owned_paths:
            relative_file(raw)
            if raw not in seen:
                seen.add(raw)
                cleaned_paths.append(raw)
        self.owned_paths = cleaned_paths
        return self


class DivisionValidation(Model):
    valid: bool
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    waves: list[list[str]] = Field(default_factory=list)


class Proposal(Model):
    model: Owner
    summary: str = Field(min_length=1, max_length=4000)
    approach: str = Field(min_length=1, max_length=24_000)
    strengths: list[str] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=12)
    created_at: float = Field(default_factory=time.time)
    duration: float = 0.0
    usage: dict[str, int] = Field(default_factory=dict)


class RefinedProposal(Model):
    model: Owner
    critique_of_peer: str = Field(min_length=1, max_length=8000)
    adopted_from_peer: str = Field(min_length=1, max_length=8000)
    summary: str = Field(min_length=1, max_length=4000)
    approach: str = Field(min_length=1, max_length=24_000)
    subtasks: list[SubTask] = Field(default_factory=list, max_length=12)
    division_check: DivisionValidation = Field(
        default_factory=lambda: DivisionValidation(valid=False, conflicts=["Not validated"])
    )
    created_at: float = Field(default_factory=time.time)
    duration: float = 0.0
    usage: dict[str, int] = Field(default_factory=dict)


class UserDecision(Model):
    choice: Literal["gemini", "codex", "merge"]
    base_model: Owner = "gemini"
    fusion_notes: str = Field(default="", max_length=4000)
    decided_at: float = Field(default_factory=time.time)


class LockedPlan(Model):
    source: Literal["gemini", "codex", "merged"]
    summary: str = Field(min_length=1, max_length=4000)
    approach: str = Field(min_length=1, max_length=24_000)
    fusion_notes: str = ""
    subtasks: list[SubTask] = Field(min_length=1, max_length=12)
    division_check: DivisionValidation


class TaskExecution(Model):
    task_id: str
    owner: Owner
    title: str
    owned_paths: list[str]
    status: Literal["pending", "running", "completed", "needs_fix", "failed"] = "pending"
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    check_output: str = ""
    review_feedback: str = ""
    error: str = ""
    attempts: int = 0
    duration: float = 0.0
    usage: dict[str, int] = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


class FixRequest(Model):
    task_id: str = Field(min_length=1, max_length=24)
    feedback: str = Field(min_length=1, max_length=4000)


class MutualReview(Model):
    reviewer: Owner
    target_owner: Owner
    round: int = 1
    verdict: Literal["pass", "fail"]
    summary: str = Field(min_length=1, max_length=4000)
    findings: list[str] = Field(default_factory=list, max_length=20)
    fix_requests: list[FixRequest] = Field(default_factory=list, max_length=12)
    created_at: float = Field(default_factory=time.time)
    duration: float = 0.0
    usage: dict[str, int] = Field(default_factory=dict)


class CollabSession(Model):
    version: Literal[1] = 1
    id: str
    mode: Mode
    task: str = Field(min_length=1, max_length=12_000)
    project_path: str | None = None
    source_commit: str | None = None
    stage: Stage = "proposing"
    error: str = ""
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    # Stage 1: Independent initial proposals
    proposals: dict[str, Proposal] = Field(default_factory=dict)
    # Stage 2: Cross-critique + upgraded plans + non-overlapping division
    refined: dict[str, RefinedProposal] = Field(default_factory=dict)
    # Stage 3: User decision & locked non-overlapping plan
    decision: UserDecision | None = None
    locked_plan: LockedPlan | None = None
    # Stage 4: Per-subtask execution state
    executions: dict[str, TaskExecution] = Field(default_factory=dict)
    # Stage 5: Mutual cross-reviews (latest per reviewer) & history
    review_round: int = 1
    max_review_rounds: int = 2
    reviews: dict[str, MutualReview] = Field(default_factory=dict)
    review_history: list[MutualReview] = Field(default_factory=list)
    # Final compiled deliverable (especially study-guide.md in learn mode)
    study_guide_path: str | None = None
    final_summary: str = ""


def validate_division(subtasks: list[SubTask]) -> DivisionValidation:
    """Mechanically enforce disjoint file/section ownership and acyclic dependencies."""
    conflicts: list[str] = []
    warnings: list[str] = []
    if not subtasks:
        return DivisionValidation(valid=False, conflicts=["分工表不能为空，至少需要 1 个明确的子任务。"])

    by_id: dict[str, SubTask] = {}
    for st in subtasks:
        if st.id in by_id:
            conflicts.append(f"子任务 ID 重复：{st.id}")
        by_id[st.id] = st

    # Validate dependency references and self-loops
    for st in subtasks:
        for dep in st.depends_on:
            if dep == st.id:
                conflicts.append(f"子任务 {st.id} 不能依赖自身。")
            elif dep not in by_id:
                conflicts.append(f"子任务 {st.id} 依赖了不存在的前置任务 '{dep}'。")

    # Compute topological waves and detect cycles (Kahn's algorithm)
    waves: list[list[str]] = []
    ancestors: dict[str, set[str]] = {st.id: set() for st in subtasks}
    if not conflicts:
        indeg = {st.id: len(set(st.depends_on)) for st in subtasks}
        children: dict[str, list[str]] = {st.id: [] for st in subtasks}
        for st in subtasks:
            for dep in set(st.depends_on):
                children[dep].append(st.id)
        ready = sorted(tid for tid, deg in indeg.items() if deg == 0)
        visited = 0
        while ready:
            waves.append(ready)
            next_ready = []
            for tid in ready:
                visited += 1
                for ch in children[tid]:
                    ancestors[ch].add(tid)
                    ancestors[ch].update(ancestors[tid])
                    indeg[ch] -= 1
                    if indeg[ch] == 0:
                        next_ready.append(ch)
            ready = sorted(next_ready)
        if visited != len(subtasks):
            conflicts.append("分工表存在循环依赖（环路），无法确定安全执行顺序。")

    # Hard mechanical anti-overlap check on owned_paths
    path_claims: dict[str, list[SubTask]] = {}
    for st in subtasks:
        for path in st.owned_paths:
            path_claims.setdefault(path, []).append(st)

    for path, claimants in sorted(path_claims.items()):
        if len(claimants) <= 1:
            continue
        owners = {c.owner for c in claimants}
        if len(owners) > 1:
            names = " vs ".join(f"{c.id}({c.owner})" for c in claimants)
            conflicts.append(
                f"路径/章节 '{path}' 存在跨模型所有权冲突：{names}。"
                "不同模型必须拥有互斥的文件或章节，禁止重叠修改同一目标。"
            )
        else:
            # Same owner: allowed ONLY if strictly ordered by dependency chain
            for i in range(len(claimants)):
                for j in range(i + 1, len(claimants)):
                    a, b = claimants[i], claimants[j]
                    ordered = (a.id in ancestors.get(b.id, set())) or (b.id in ancestors.get(a.id, set()))
                    if not ordered:
                        conflicts.append(
                            f"路径/章节 '{path}' 被同一模型的并行任务 {a.id} 与 {b.id} 同时声明，必须显式串行依赖或拆分文件。"
                        )

    owners_used = {st.owner for st in subtasks}
    if len(owners_used) < 2 and len(subtasks) >= 2:
        only = next(iter(owners_used))
        warnings.append(f"当前全部子任务均分配给了 {only}；建议让 Gemini 与 Codex 按优势分工协同。")

    return DivisionValidation(
        valid=len(conflicts) == 0,
        conflicts=conflicts,
        warnings=warnings,
        waves=waves if not conflicts else [],
    )


def validate_project_path(raw: str) -> dict:
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > 500 or any(ord(c) < 32 for c in cleaned):
        raise ValueError("无效的项目目录路径")
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        raise ValueError("项目目录必须为绝对路径")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("项目目录不存在")
    resolved = candidate.resolve()
    if resolved == Path("/") or resolved == Path.home().resolve():
        raise ValueError("不能直接使用根目录或用户主目录作为项目目录")
    for prefix in SENSITIVE_PROJECT_PREFIXES:
        if resolved == prefix or resolved.is_relative_to(prefix):
            raise ValueError("不允许使用系统目录作为工作区")
    if any(part.casefold() in SENSITIVE_PROJECT_PARTS for part in resolved.parts):
        raise ValueError("不允许访问敏感凭据或全局 Agent 配置目录")
    commit = git(resolved, "rev-parse", "--verify", "HEAD^{commit}")
    branch = git(resolved, "rev-parse", "--abbrev-ref", "HEAD")
    return {"valid": True, "path": str(resolved), "head": commit, "branch": branch}


class BackendInspector:
    """Honest, live status reporting for Codex CLI and Google Antigravity."""

    def __init__(self, backends: dict[str, Backend] | None = None):
        self._backends = backends
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached_codex: dict | None = None

    def inspect(self, *, active_codex: bool = False, waiting_gemini: bool = False, force: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            if force or self._cached_codex is None or (now - self._cached_at) > 5.0:
                providers = self._backends if self._backends is not None else default_backends()
                health = providers["codex"].health()
                exe = codex_executable() if self._backends is None else "injected-codex"
                installed = bool(exe) if health.status != "unavailable" else False
                authenticated = health.status == "ok"
                callable_now = installed and authenticated
                if health.status == "ok":
                    state = "callable"
                    detail = "Codex CLI 已安装并通过 ChatGPT 登录认证，可自动执行。"
                elif health.status == "auth":
                    state = "installed"
                    detail = "Codex CLI 已安装，但未检测到 ChatGPT 登录状态。"
                elif health.status == "unavailable":
                    state = "unavailable"
                    detail = health.error or "未找到 Codex CLI。"
                else:
                    state = "unverified"
                    detail = health.error or f"Codex 状态未验证（{health.status}）。"
                self._cached_codex = {
                    "backend": "codex",
                    "installed": installed,
                    "authenticated": authenticated,
                    "callable": callable_now,
                    "state": state,
                    "executable": exe if installed else None,
                    "detail": detail,
                }
                self._cached_at = now
            codex = dict(self._cached_codex)

        codex["running"] = active_codex
        if active_codex and codex["callable"]:
            codex["state"] = "running"

        gemini_app = Path("/Applications/Gemini.app").is_dir()
        google = {
            "backend": "google",
            "installed": gemini_app,
            "authenticated": False,
            "callable": False,
            "running": waiting_gemini,
            "state": "restricted",
            "mode": "antigravity_host",
            "detail": (
                "遵循 Google 个人订阅条款：Gemini 侧在官方 Antigravity 客户端内原生运行（$0 额外费用），"
                "通过本工作台与 Codex 同步协同、互审并由你一键拍板。"
            ),
        }
        return {"codex": codex, "google": google}


def _repo_overview(workspace: Path, limit: int = 4000) -> str:
    files_raw = git(workspace, "ls-files")
    if not files_raw:
        return "(空白工作区，尚无已有文件)"
    names = [n for n in files_raw.splitlines() if n and not n.startswith(".aisys-check/")][:40]
    parts = ["已有文件列表：\n" + "\n".join(f"- {n}" for n in names)]
    budget = limit
    for name in names[:6]:
        if budget <= 200:
            break
        try:
            snippet = read_context(workspace, name, min(1200, budget))
        except ValueError:
            continue
        block = f"\n--- {name} ---\n{snippet}"
        parts.append(block)
        budget -= len(block)
    return redact("\n".join(parts))[:limit]


def _compile_study_guide(session: CollabSession, workspace: Path, *, header_note: str = "") -> str:
    """Compile completed non-overlapping Markdown sections + cross-review notes into study-guide.md."""
    lines = [
        f"# {session.task.splitlines()[0][:80]}",
        "",
        f"> **说明**：{header_note or '双模型协作学习文档（Gemini + Codex）'}  ",
        f"> **会话 ID**：`{session.id}`  ",
        f"> **采用方案**：{session.locked_plan.source if session.locked_plan else '—'}  ",
        "",
        "## 一、学习目标与总体框架",
        "",
        session.task,
        "",
    ]
    if session.locked_plan:
        lines.extend([
            "### 最终选定思路",
            "",
            session.locked_plan.summary,
            "",
            session.locked_plan.approach,
            "",
        ])
        if session.locked_plan.fusion_notes:
            lines.extend(["**用户融合微调意见**：" + session.locked_plan.fusion_notes, ""])
        lines.extend([
            "### 无重叠分工清单",
            "",
            "| 编号 | 负责模型 | 任务标题 | 独占文件 / 章节 | 状态 |",
            "| --- | --- | --- | --- | --- |",
        ])
        for st in session.locked_plan.subtasks:
            ex = session.executions.get(st.id)
            st_status = ex.status if ex else "pending"
            paths = ", ".join(f"`{p}`" for p in st.owned_paths)
            lines.append(f"| `{st.id}` | **{st.owner}** | {st.title} | {paths} | {st_status} |")
        lines.append("")

    lines.extend(["## 二、分模块学习内容", ""])
    if session.locked_plan:
        for st in session.locked_plan.subtasks:
            lines.extend([
                f"### {st.id} · {st.title}（负责人：{st.owner}）",
                "",
            ])
            for rel in st.owned_paths:
                try:
                    content = read_context(workspace, rel, 32_000)
                except ValueError:
                    content = "[无法读取]"
                if content == "[file absent]":
                    ex = session.executions.get(st.id)
                    content = ex.summary if ex and ex.summary else "_（该章节尚未生成内容）_"
                lines.extend([content.strip(), ""])

    if session.review_history:
        lines.extend(["## 三、双模型交叉审查与勘误记录", ""])
        for rev in session.review_history:
            lines.extend([
                f"### 第 {rev.round} 轮 · {rev.reviewer} 审查 {rev.target_owner}（结论：`{rev.verdict}`）",
                "",
                rev.summary,
                "",
            ])
            for finding in rev.findings:
                lines.append(f"- {finding}")
            if rev.findings:
                lines.append("")

    return redact("\n".join(lines).strip() + "\n")


class CollabStore:
    """Single source of truth for 5-Stage Dual-Model Co-Creation sessions."""

    def __init__(
        self,
        data: Path,
        *,
        project_root: Path | None = None,
        backends: dict[str, Backend] | None = None,
        sync_codex: bool = False,
    ):
        self.data = data.resolve()
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.sessions_dir = self.data / "sessions"
        self.sessions_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.project_root = (project_root or Path.cwd()).resolve()
        self.backends = backends
        self.sync_codex = sync_codex
        self.instance_id = uuid4().hex
        self.started_at = time.time()
        self.inspector = BackendInspector(backends=backends)
        self.pipeline_store = PipelineConfigStore(self.data)
        self.modules = ModuleController(
            data=self.data,
            project_root=self.project_root,
            pipeline_store=self.pipeline_store,
            backends=backends,
        )

        self._lock = threading.Lock()
        self._active_codex_sessions: set[str] = set()
        self._active_processes: dict[str, dict] = {}
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sub_lock = threading.Lock()
        self._stop_watcher = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._fingerprints: dict[str, str] = {}
        self._shutdown_callback = None

    def get_pipeline_config(self) -> PipelineConfig:
        return self.pipeline_store.load()

    def save_pipeline_config(self, cfg: PipelineConfig | dict) -> PipelineConfig:
        saved = self.pipeline_store.save(cfg)
        self.publish("pipeline_changed", {"profile": saved.shared.profile_name})
        return saved

    def apply_pipeline_profile(self, profile_id: str) -> PipelineConfig:
        saved = self.pipeline_store.apply_profile(profile_id)
        self.publish("pipeline_changed", {"profile": saved.shared.profile_name})
        return saved

    def reset_pipeline_config(self) -> PipelineConfig:
        saved = self.pipeline_store.reset()
        self.publish("pipeline_changed", {"profile": saved.shared.profile_name})
        return saved

    def list_builtin_profiles(self) -> list[dict]:
        return [
            {"id": p["id"], "label": p["label"], "description": p["description"]}
            for p in BUILTIN_PROFILES.values()
        ]

    def _aggregate_stats(self) -> dict:
        sessions = self.list_sessions()
        lock_allows = 0
        lock_denies = 0
        for item in sessions[:40]:
            for ev in self.list_events(item["id"], limit=200):
                if ev.get("event_type") == "writelock.audit":
                    dec = (ev.get("detail") or {}).get("decision")
                    if dec == "ALLOW":
                        lock_allows += 1
                    elif dec == "DENY":
                        lock_denies += 1
        return {
            "sessions": len(sessions),
            "completed": sum(1 for s in sessions if s["stage"] == "completed"),
            "study_guides": sum(1 for s in sessions if s["has_study_guide"]),
            "lock_allows": lock_allows,
            "lock_denies": lock_denies,
        }

    def inspect_modules(self, *, host: str = "127.0.0.1", port: int = 8765, test_mode: bool = False) -> dict:
        with self._sub_lock:
            sse_clients = len(self._subscribers)
        server_meta = {
            "host": host,
            "port": port,
            "uptime_s": round(time.time() - self.started_at, 1),
            "sse_clients": sse_clients,
            "active_codex": self.active_sessions_count(),
        }
        return self.modules.inspect_all(
            server_meta=server_meta,
            stats=self._aggregate_stats(),
            test_mode=test_mode,
        )

    def compute_node_statuses(self, s: CollabSession, events: list[dict], metrics: dict) -> dict[str, dict]:
        """Compute live status of every individual Gemini, Codex, and Shared backend node."""
        cfg = self.get_pipeline_config()
        proc = self.get_active_process(s.id)
        codex_stage = proc.get("stage") if proc else None

        def _tok(usage: dict | None) -> int:
            if not isinstance(usage, dict):
                return 0
            return int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)

        # Gemini Stage 1
        g_prop = s.proposals.get("gemini")
        if not cfg.modules.gemini_enabled:
            gp_st = "skipped"
        elif g_prop:
            gp_st = "completed"
        elif s.stage == "proposing":
            gp_st = "waiting"
        else:
            gp_st = "idle"

        # Codex Stage 1
        c_prop = s.proposals.get("codex")
        if not cfg.modules.codex_enabled:
            cp_st = "skipped"
        elif c_prop:
            cp_st = "completed"
        elif s.stage == "proposing" and codex_stage == "proposing":
            cp_st = "running"
        elif s.stage == "proposing":
            cp_st = "waiting"
        else:
            cp_st = "idle"

        # Stage 2 Critique
        g_ref = s.refined.get("gemini")
        c_ref = s.refined.get("codex")
        if not cfg.stages.enable_critique or not cfg.modules.gemini_enabled:
            gc_st = "skipped" if not g_ref else "completed"
        elif g_ref:
            gc_st = "completed"
        elif s.stage == "critiquing":
            gc_st = "waiting"
        else:
            gc_st = "idle"

        if not cfg.stages.enable_critique or not cfg.modules.codex_enabled:
            cc_st = "skipped" if not c_ref else "completed"
        elif c_ref:
            cc_st = "completed"
        elif s.stage == "critiquing" and codex_stage == "critiquing":
            cc_st = "running"
        elif s.stage == "critiquing":
            cc_st = "waiting"
        else:
            cc_st = "idle"

        # Shared Division & Decision Gate
        if s.locked_plan:
            gate_st = "completed"
        elif s.stage in {"deciding", "merging"}:
            gate_st = "waiting"
        else:
            gate_st = "idle"

        # Stage 4 Executions (per model lane)
        g_execs = [ex for ex in s.executions.values() if ex.owner == "gemini"]
        c_execs = [ex for ex in s.executions.values() if ex.owner == "codex"]

        def _exec_lane_status(items: list[TaskExecution], enabled: bool, is_codex: bool) -> str:
            if not enabled:
                return "skipped"
            if not items:
                return "completed" if s.stage in {"reviewing", "completed"} else "idle"
            if any(x.status == "failed" for x in items):
                return "failed"
            if any(x.status == "running" for x in items) or (is_codex and codex_stage == "executing"):
                return "running"
            if any(x.status == "needs_fix" for x in items):
                return "rework"
            if all(x.status == "completed" for x in items):
                return "completed"
            return "waiting" if s.stage == "executing" else "idle"

        ge_st = _exec_lane_status(g_execs, cfg.modules.gemini_enabled, False)
        ce_st = _exec_lane_status(c_execs, cfg.modules.codex_enabled, True)

        # Stage 5 Mutual Review (per model lane)
        g_rev = s.reviews.get("gemini") or next(
            (r for r in reversed(s.review_history) if r.reviewer == "gemini"), None
        )
        c_rev = s.reviews.get("codex") or next(
            (r for r in reversed(s.review_history) if r.reviewer == "codex"), None
        )
        if not cfg.stages.enable_mutual_review or not cfg.modules.gemini_enabled:
            gr_st = "skipped" if not g_rev else "completed"
        elif s.stage == "reviewing" and "gemini" not in s.reviews and c_execs:
            gr_st = "waiting"
        elif g_rev:
            gr_st = "completed" if g_rev.verdict == "pass" else "rework"
        else:
            gr_st = "idle"

        if not cfg.stages.enable_mutual_review or not cfg.modules.codex_enabled:
            cr_st = "skipped" if not c_rev else "completed"
        elif s.stage == "reviewing" and codex_stage == "reviewing":
            cr_st = "running"
        elif s.stage == "reviewing" and "codex" not in s.reviews and g_execs:
            cr_st = "waiting"
        elif c_rev:
            cr_st = "completed" if c_rev.verdict == "pass" else "rework"
        else:
            cr_st = "idle"

        lock_events = [e for e in events if e.get("category") == "lock"]
        lock_st = (
            "failed" if metrics.get("lock_denies", 0) > 0 and s.stage == "failed"
            else "completed" if metrics.get("lock_allows", 0) > 0
            else "running" if s.locked_plan and s.stage == "executing"
            else "idle"
        )
        deliv_st = "completed" if s.stage == "completed" else ("failed" if s.stage == "failed" else "idle")

        return {
            "input_dispatch": {
                "id": "input_dispatch", "label": "共享意图分发与全局约束", "owner": "shared", "stage": 0,
                "enabled": True, "status": "completed",
                "summary": f"预设 {cfg.shared.profile_name} · 模式 {s.mode.upper()}",
                "editor_section": "shared",
            },
            "gemini_propose": {
                "id": "gemini_propose", "label": "Gemini 独立初案", "owner": "gemini", "stage": 1,
                "enabled": cfg.modules.gemini_enabled, "status": gp_st,
                "summary": g_prop.summary[:120] if g_prop else "等待 Antigravity (/aisys) 独立起草",
                "editor_section": "gemini_propose",
            },
            "codex_propose": {
                "id": "codex_propose", "label": "Codex 独立初案", "owner": "codex", "stage": 1,
                "enabled": cfg.modules.codex_enabled, "status": cp_st,
                "duration_s": c_prop.duration if c_prop else (proc.get("elapsed_s") if codex_stage == "proposing" else 0),
                "tokens": _tok(c_prop.usage) if c_prop else 0,
                "pid": proc.get("pid") if codex_stage == "proposing" else None,
                "summary": c_prop.summary[:120] if c_prop else (proc.get("latest_event") if codex_stage == "proposing" else "隔离沙箱就绪"),
                "editor_section": "codex_propose",
            },
            "gemini_critique": {
                "id": "gemini_critique", "label": "Gemini 互审与分工", "owner": "gemini", "stage": 2,
                "enabled": cfg.modules.gemini_enabled and cfg.stages.enable_critique, "status": gc_st,
                "summary": g_ref.summary[:120] if g_ref else ("阶段 2 已跳过" if not cfg.stages.enable_critique else "审视 Codex 并给出互斥分工"),
                "editor_section": "gemini_critique",
            },
            "codex_critique": {
                "id": "codex_critique", "label": "Codex 互审与分工", "owner": "codex", "stage": 2,
                "enabled": cfg.modules.codex_enabled and cfg.stages.enable_critique, "status": cc_st,
                "duration_s": c_ref.duration if c_ref else (proc.get("elapsed_s") if codex_stage == "critiquing" else 0),
                "tokens": _tok(c_ref.usage) if c_ref else 0,
                "pid": proc.get("pid") if codex_stage == "critiquing" else None,
                "summary": c_ref.summary[:120] if c_ref else ("阶段 2 已跳过" if not cfg.stages.enable_critique else "审视 Gemini 并给出互斥分工"),
                "editor_section": "codex_critique",
            },
            "division_gate": {
                "id": "division_gate", "label": "防重叠校验与拍板融合", "owner": "shared", "stage": 3,
                "enabled": True, "status": gate_st,
                "summary": (
                    f"已锁定 {len(s.locked_plan.subtasks)} 项互斥任务（{len(s.locked_plan.division_check.waves)} 波次）"
                    if s.locked_plan
                    else ("人工拍板/融合微调" if cfg.stages.require_user_decision else f"自动锁定（默认 {cfg.stages.default_winner}）")
                ),
                "editor_section": "division_gate",
            },
            "gemini_exec": {
                "id": "gemini_exec", "label": "Gemini 独占路径执行", "owner": "gemini", "stage": 4,
                "enabled": cfg.modules.gemini_enabled, "status": ge_st,
                "summary": f"{sum(1 for x in g_execs if x.status == 'completed')}/{len(g_execs)} 项完成" if g_execs else "无分配任务",
                "editor_section": "gemini_exec",
            },
            "codex_exec": {
                "id": "codex_exec", "label": "Codex 独占路径执行", "owner": "codex", "stage": 4,
                "enabled": cfg.modules.codex_enabled, "status": ce_st,
                "duration_s": round(sum(x.duration for x in c_execs), 2),
                "tokens": sum(_tok(x.usage) for x in c_execs),
                "pid": proc.get("pid") if codex_stage == "executing" else None,
                "summary": f"{sum(1 for x in c_execs if x.status == 'completed')}/{len(c_execs)} 项完成" if c_execs else "无分配任务",
                "editor_section": "codex_exec",
            },
            "writelock_sandbox": {
                "id": "writelock_sandbox", "label": "硬写锁与 Seatbelt 沙箱", "owner": "shared", "stage": 4,
                "enabled": True, "status": lock_st,
                "summary": f"放行 {metrics.get('lock_allows', 0)} · 拦截 {metrics.get('lock_denies', 0)} · 校验 {len(lock_events)} 次",
                "editor_section": "writelock_sandbox",
            },
            "gemini_review": {
                "id": "gemini_review", "label": "Gemini 审查 Codex", "owner": "gemini", "stage": 5,
                "enabled": cfg.modules.gemini_enabled and cfg.stages.enable_mutual_review, "status": gr_st,
                "summary": f"第 {g_rev.round} 轮 · {g_rev.verdict.upper()}" if g_rev else ("互审已关闭" if not cfg.stages.enable_mutual_review else "等待审查 Codex 产出"),
                "editor_section": "gemini_review",
            },
            "codex_review": {
                "id": "codex_review", "label": "Codex 审查 Gemini", "owner": "codex", "stage": 5,
                "enabled": cfg.modules.codex_enabled and cfg.stages.enable_mutual_review, "status": cr_st,
                "duration_s": c_rev.duration if c_rev else (proc.get("elapsed_s") if codex_stage == "reviewing" else 0),
                "tokens": _tok(c_rev.usage) if c_rev else 0,
                "pid": proc.get("pid") if codex_stage == "reviewing" else None,
                "summary": f"第 {c_rev.round} 轮 · {c_rev.verdict.upper()}" if c_rev else ("互审已关闭" if not cfg.stages.enable_mutual_review else "等待审查 Gemini 产出"),
                "editor_section": "codex_review",
            },
            "final_delivery": {
                "id": "final_delivery", "label": "最终汇编与交付归档", "owner": "shared", "stage": 5,
                "enabled": True, "status": deliv_st,
                "summary": s.final_summary or ("编译 Study Guide Markdown" if s.mode == "learn" else "汇总隔离工作区 Git Diff"),
                "editor_section": "final_delivery",
            },
        }

    def _codex(self) -> CodexBackend:
        if self.backends is not None:
            return self.backends["codex"]
        return CodexBackend()

    # ---------- Real Kernel Telemetry (events.jsonl + SSE) ----------
    def _events_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.jsonl"

    def record_event(
        self,
        session_id: str,
        *,
        category: Literal["engine", "codex", "gemini", "lock", "workspace"],
        event_type: str,
        actor: Literal["aisys", "codex", "gemini", "user"],
        stage: str,
        summary: str,
        detail: dict | None = None,
    ) -> dict:
        ev = {
            "id": uuid4().hex[:12],
            "ts": round(time.time(), 3),
            "category": category,
            "event_type": event_type,
            "actor": actor,
            "stage": stage,
            "summary": redact(summary)[:400],
            "detail": detail or {},
        }
        try:
            ev_path = self.sessions_dir / session_id / "events.jsonl"
            if ev_path.parent.is_dir():
                line = json.dumps(ev, ensure_ascii=False) + "\n"
                with ev_path.open("a", encoding="utf-8") as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    try:
                        fh.write(line)
                        fh.flush()
                    finally:
                        fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        self.publish("trace_event", {"session_id": session_id, "event": ev})
        return ev

    def list_events(self, session_id: str, *, limit: int = 120) -> list[dict]:
        ev_path = self._events_file(session_id)
        if not ev_path.is_file():
            return []
        try:
            lines = ev_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        parsed: list[dict] = []
        for raw in lines[-max(1, min(limit, 500)):]:
            try:
                item = json.loads(raw)
                if isinstance(item, dict):
                    parsed.append(item)
            except ValueError:
                continue
        return parsed

    def get_active_process(self, session_id: str) -> dict | None:
        with self._lock:
            proc = self._active_processes.get(session_id)
            if not proc:
                return None
            snap = dict(proc)
        snap["elapsed_s"] = round(max(0.0, time.time() - snap["started_at"]), 1)
        return snap

    def _set_active_process(self, session_id: str, info: dict | None) -> None:
        with self._lock:
            if info is None:
                self._active_processes.pop(session_id, None)
            else:
                self._active_processes[session_id] = dict(info)

    def _update_active_process(self, session_id: str, **updates) -> None:
        with self._lock:
            proc = self._active_processes.get(session_id)
            if proc is not None:
                proc.update(updates)

    def _session_metrics(self, s: CollabSession, events: list[dict]) -> dict:
        in_tok = 0
        cached_tok = 0
        out_tok = 0
        codex_dur = 0.0

        def _add(usage: dict | None, dur: float = 0.0) -> None:
            nonlocal in_tok, cached_tok, out_tok, codex_dur
            if isinstance(usage, dict):
                in_tok += int(usage.get("input_tokens", 0) or 0)
                cached_tok += int(usage.get("cached_input_tokens", 0) or 0)
                out_tok += int(usage.get("output_tokens", 0) or 0)
            codex_dur += float(dur or 0.0)

        for prop in s.proposals.values():
            _add(prop.usage, prop.duration)
        for ref in s.refined.values():
            _add(ref.usage, ref.duration)
        for ex in s.executions.values():
            _add(ex.usage, ex.duration)
        for rev in s.review_history:
            _add(rev.usage, rev.duration)

        lock_allows = 0
        lock_denies = 0
        for ev in events:
            if ev.get("event_type") == "writelock.audit":
                decision = (ev.get("detail") or {}).get("decision")
                if decision == "ALLOW":
                    lock_allows += 1
                elif decision == "DENY":
                    lock_denies += 1
        return {
            "input_tokens": in_tok,
            "cached_input_tokens": cached_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "codex_duration_s": round(codex_dur, 2),
            "lock_allows": lock_allows,
            "lock_denies": lock_denies,
            "event_count": len(events),
        }

    def _apply_edits_audited(
        self,
        session_id: str,
        *,
        owner: Owner,
        task_id: str,
        stage: str,
        ws: Path,
        edits: list[Edit],
        allowed: list[str],
    ) -> list[str]:
        allowed_set = set(allowed)
        for ed in edits:
            if ed.path not in allowed_set:
                self.record_event(
                    session_id,
                    category="lock",
                    event_type="writelock.audit",
                    actor=owner,
                    stage=stage,
                    summary=f"写锁拦截越界写入：{owner.upper()} ({task_id}) → {ed.path}",
                    detail={
                        "decision": "DENY",
                        "task_id": task_id,
                        "owner": owner,
                        "path": ed.path,
                        "allowed_paths": list(allowed),
                    },
                )
        try:
            written = apply_edits(ws, edits, allowed)
        except ValueError as err:
            if not any(ed.path not in allowed_set for ed in edits):
                self.record_event(
                    session_id,
                    category="lock",
                    event_type="writelock.audit",
                    actor=owner,
                    stage=stage,
                    summary=f"写锁校验失败：{owner.upper()} ({task_id}) · {err}",
                    detail={
                        "decision": "DENY",
                        "task_id": task_id,
                        "owner": owner,
                        "allowed_paths": list(allowed),
                        "error": redact(str(err))[:300],
                    },
                )
            raise
        size_by_path = {ed.path: len(ed.content.encode("utf-8")) for ed in edits}
        for path in written:
            self.record_event(
                session_id,
                category="lock",
                event_type="writelock.audit",
                actor=owner,
                stage=stage,
                summary=f"写锁放行：{owner.upper()} ({task_id}) → {path} ({size_by_path.get(path, 0)} B)",
                detail={
                    "decision": "ALLOW",
                    "task_id": task_id,
                    "owner": owner,
                    "path": path,
                    "bytes": size_by_path.get(path, 0),
                    "allowed_paths": list(allowed),
                },
            )
        return written

    def _make_codex_stream_handler(self, session_id: str, *, stage: str, step_label: str):
        def _handle(ev: dict) -> None:
            etype = str(ev.get("type") or "codex.event")
            if etype == "process.spawned":
                pid = ev.get("pid")
                self._update_active_process(
                    session_id,
                    pid=pid,
                    sandbox=ev.get("sandbox", "read-only"),
                    latest_event=f"子进程已拉起 (PID {pid})",
                )
                self.record_event(
                    session_id,
                    category="codex",
                    event_type="process.spawned",
                    actor="codex",
                    stage=stage,
                    summary=f"拉起 Codex 沙箱进程 ({step_label}) · PID {pid} · {ev.get('sandbox', 'read-only')}",
                    detail=ev,
                )
            elif etype == "codex.thread.started":
                tid = str(ev.get("thread_id") or "")
                self._update_active_process(
                    session_id,
                    thread_id=tid,
                    latest_event=f"Codex 线程建立 {tid[:8]}",
                )
                self.record_event(
                    session_id,
                    category="codex",
                    event_type="codex.thread.started",
                    actor="codex",
                    stage=stage,
                    summary=f"Codex 会话线程建立 · thread={tid[:8]}",
                    detail=ev,
                )
            elif etype in {"codex.item.started", "codex.item.completed"}:
                itype = str(ev.get("item_type") or "item")
                chars = ev.get("chars", 0)
                verb = "开始" if etype.endswith("started") else "产出"
                label = f"Codex {verb} {itype}" + (f" ({chars} chars)" if chars else "")
                self._update_active_process(session_id, latest_event=label)
                self.record_event(
                    session_id,
                    category="codex",
                    event_type=etype,
                    actor="codex",
                    stage=stage,
                    summary=label,
                    detail=ev,
                )
            elif etype == "codex.turn.completed":
                usage = ev.get("usage") or {}
                in_t = usage.get("input_tokens", 0)
                out_t = usage.get("output_tokens", 0)
                el = ev.get("elapsed_s", 0)
                label = f"Codex 回合完成 · {el}s · in={in_t} / out={out_t} tok"
                self._update_active_process(session_id, latest_event=label)
                self.record_event(
                    session_id,
                    category="codex",
                    event_type="codex.turn.completed",
                    actor="codex",
                    stage=stage,
                    summary=label,
                    detail=ev,
                )
            elif etype == "codex.error":
                self._update_active_process(session_id, latest_event="Codex 返回错误事件")
                self.record_event(
                    session_id,
                    category="codex",
                    event_type="codex.error",
                    actor="codex",
                    stage=stage,
                    summary="Codex 进程上报异常事件",
                    detail=ev,
                )
        return _handle

    @staticmethod
    def _supports_kwarg(fn, name: str) -> bool:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        if name in sig.parameters:
            return True
        return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    # ---------- Server config & lifecycle ----------
    def server_file(self) -> Path:
        return self.data / "server.json"

    def config_file(self) -> Path:
        return self.data / "config" / "server.yaml"

    def get_server_config(self) -> ServerConfig:
        path = self.config_file()
        if path.is_file() and not path.is_symlink():
            raw = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader) or {}
            return ServerConfig.model_validate(raw)
        return ServerConfig()

    def save_server_config(self, cfg: ServerConfig) -> ServerConfig:
        if cfg.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Web server host must be loopback (127.0.0.1 or localhost)")
        if cfg.default_project:
            validate_project_path(cfg.default_project)
        path = self.config_file()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        write_atomic(path, text.encode("utf-8"), mode=0o600)
        return cfg

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
        write_atomic(self.server_file(), json.dumps(info, indent=2).encode("utf-8"), mode=0o600)
        return info

    def clear_server_info(self) -> None:
        path = self.server_file()
        if not path.is_file():
            return
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("instance_id") == self.instance_id:
                path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    # ---------- Event Pub/Sub (SSE) ----------
    def start_watcher(self) -> None:
        if self._watcher_thread and self._watcher_thread.is_alive():
            return
        self._stop_watcher.clear()
        self._watcher_thread = threading.Thread(
            target=self._watcher_loop, name="aisys-collab-watcher", daemon=True,
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
        msg = {"type": event_type, "ts": round(time.time(), 3), "data": payload}
        with self._sub_lock:
            subs = list(self._subscribers)
        for loop, queue in subs:
            if loop.is_closed():
                continue
            try:
                loop.call_soon_threadsafe(self._enqueue, queue, msg)
            except RuntimeError:
                pass

    @staticmethod
    def _enqueue(queue: asyncio.Queue, msg: dict) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            pass

    def _watcher_loop(self) -> None:
        while not self._stop_watcher.wait(1.0):
            try:
                changed = []
                for item in self.list_sessions()[:25]:
                    ev_path = self.sessions_dir / item["id"] / "events.jsonl"
                    ev_size = ev_path.stat().st_size if ev_path.is_file() else 0
                    fp = f"{item['stage']}:{item['updated_at']}:{item['codex_busy']}:{ev_size}"
                    if self._fingerprints.get(item["id"]) != fp:
                        self._fingerprints[item["id"]] = fp
                        changed.append(item["id"])
                if changed:
                    self.publish("sessions_changed", {"ids": changed})
            except Exception:
                pass

    # ---------- Storage helpers ----------
    def _session_dir(self, session_id: str) -> Path:
        try:
            key = str(UUID(session_id, version=4))
        except ValueError as error:
            raise NotFoundError("invalid session UUID") from error
        if key != session_id:
            raise NotFoundError("invalid session UUID")
        path = self.sessions_dir / key
        if not (path / "session.json").is_file():
            raise NotFoundError(f"session '{session_id}' not found")
        return path

    def _workspace(self, session_id: str) -> Path:
        root = self._session_dir(session_id)
        ws = root / "workspaces" / "main"
        if ws.is_symlink() or not ws.is_dir():
            raise ValueError("session workspace is missing")
        return ws

    @contextmanager
    def _exclusive(self, session_id: str):
        root = self._session_dir(session_id)
        with (root / "session.lock").open("a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ConflictError("session is currently busy executing a step") from error
            yield

    def _load(self, session_id: str) -> CollabSession:
        path = self._session_dir(session_id) / "session.json"
        return CollabSession.model_validate_json(path.read_text(encoding="utf-8"))

    def _save(self, session: CollabSession) -> CollabSession:
        session.updated_at = time.time()
        path = self.sessions_dir / session.id / "session.json"
        write_atomic(path, session.model_dump_json(indent=2).encode("utf-8"), mode=0o600)
        return session

    def is_codex_busy(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active_codex_sessions

    def active_sessions_count(self) -> int:
        with self._lock:
            return len(self._active_codex_sessions)

    # ---------- Public CRUD & Inspection ----------
    def create_session(
        self,
        *,
        task: str,
        mode: Mode = "dev",
        project: str | None = None,
        auto_start_codex: bool = True,
    ) -> dict:
        cleaned = task.strip()
        if not cleaned or len(cleaned) > 12_000:
            raise ValueError("任务描述长度须为 1–12000 字符")
        project_path: Path | None = None
        if project and project.strip():
            checked = validate_project_path(project)
            project_path = Path(checked["path"])

        cfg = self.get_pipeline_config()
        sid = str(uuid4())
        root = self.sessions_dir / sid
        root.mkdir(mode=0o700, parents=True)
        commit = prepare_spaces(root, {"main"}, project_path)
        (root / "provider").mkdir(mode=0o700, exist_ok=True)
        session = CollabSession(
            id=sid,
            mode=mode,
            task=redact(cleaned),
            project_path=str(project_path) if project_path else None,
            source_commit=commit,
            stage="proposing",
            max_review_rounds=cfg.stages.max_review_rounds,
        )
        self._save(session)
        should_auto_codex = auto_start_codex and cfg.modules.codex_enabled and cfg.stages.auto_start_codex
        self.record_event(
            sid,
            category="engine",
            event_type="session.created",
            actor="user",
            stage="proposing",
            summary=f"初始化 {mode.upper()} 协作会话（预设={cfg.shared.profile_name}，HEAD={commit[:8] if commit else 'empty'}）",
            detail={
                "mode": mode,
                "profile": cfg.shared.profile_name,
                "gemini_enabled": cfg.modules.gemini_enabled,
                "codex_enabled": cfg.modules.codex_enabled,
                "task_chars": len(cleaned),
                "has_project": bool(project_path),
                "source_commit": commit,
                "auto_start_codex": should_auto_codex,
            },
        )
        self.publish("sessions_changed", {"ids": [sid]})
        if should_auto_codex:
            self.trigger_codex_async(sid)
        return self.get_session_view(sid)

    def list_sessions(self) -> list[dict]:
        items = []
        if not self.sessions_dir.is_dir():
            return items
        for entry in self.sessions_dir.iterdir():
            if not (entry / "session.json").is_file():
                continue
            try:
                s = CollabSession.model_validate_json((entry / "session.json").read_text(encoding="utf-8"))
                items.append({
                    "id": s.id,
                    "mode": s.mode,
                    "task": s.task,
                    "stage": s.stage,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                    "codex_busy": self.is_codex_busy(s.id),
                    "has_study_guide": bool(s.study_guide_path),
                    "choice": s.decision.choice if s.decision else None,
                })
            except (OSError, ValueError):
                continue
        return sorted(items, key=lambda x: x["created_at"], reverse=True)

    def get_session_view(self, session_id: str) -> dict:
        s = self._load(session_id)
        ws = self._workspace(session_id)
        events = self.list_events(session_id, limit=120)
        metrics = self._session_metrics(s, events)
        data = s.model_dump(mode="json")
        data["codex_busy"] = self.is_codex_busy(session_id)
        data["active_process"] = self.get_active_process(session_id)
        data["metrics"] = metrics
        data["nodes"] = self.compute_node_statuses(s, events, metrics)
        data["pipeline_profile"] = self.get_pipeline_config().shared.profile_name
        data["events"] = events[-80:]
        data["workspace"] = str(ws)
        data["artifacts"] = artifacts(ws)
        data["next_action"] = self.next_action(session_id)
        return data

    def next_action(self, session_id: str) -> dict:
        """Return exact pending actions for Gemini (Antigravity), Codex, or User."""
        s = self._load(session_id)
        ws = self._workspace(session_id)
        cfg = self.get_pipeline_config()
        codex_ready = False
        pending_gemini: list[dict] = []
        awaiting_user = False

        if s.stage == "proposing":
            codex_ready = cfg.modules.codex_enabled and "codex" not in s.proposals
            if cfg.modules.gemini_enabled and "gemini" not in s.proposals:
                pending_gemini.append({
                    "action": "propose",
                    "stage": "proposing",
                    "prompt": self._proposal_prompt(s, ws, "gemini"),
                    "schema": PROPOSAL_SCHEMA,
                })
        elif s.stage == "critiquing":
            codex_ready = cfg.modules.codex_enabled and "codex" not in s.refined
            if cfg.modules.gemini_enabled and "gemini" not in s.refined and "gemini" in s.proposals and "codex" in s.proposals:
                pending_gemini.append({
                    "action": "critique",
                    "stage": "critiquing",
                    "peer_proposal": s.proposals["codex"].model_dump(mode="json"),
                    "prompt": self._critique_prompt(s, ws, "gemini", s.proposals["gemini"], s.proposals["codex"]),
                    "schema": REFINED_SCHEMA,
                })
        elif s.stage == "deciding":
            awaiting_user = True
        elif s.stage == "merging":
            pending_gemini.append({
                "action": "merge",
                "stage": "merging",
                "decision": s.decision.model_dump(mode="json") if s.decision else None,
                "gemini_refined": s.refined["gemini"].model_dump(mode="json") if "gemini" in s.refined else None,
                "codex_refined": s.refined["codex"].model_dump(mode="json") if "codex" in s.refined else None,
                "prompt": self._merge_prompt(s),
            })
        elif s.stage == "executing" and s.locked_plan:
            for st in s.locked_plan.subtasks:
                ex = s.executions.get(st.id)
                if not ex or ex.status not in {"pending", "needs_fix"}:
                    continue
                deps_ok = all(
                    s.executions.get(dep) and s.executions[dep].status == "completed"
                    for dep in st.depends_on
                )
                if not deps_ok:
                    continue
                if st.owner == "codex":
                    if cfg.modules.codex_enabled:
                        codex_ready = True
                elif cfg.modules.gemini_enabled:
                    pending_gemini.append({
                        "action": "execute_subtask",
                        "stage": "executing",
                        "task_id": st.id,
                        "subtask": st.model_dump(mode="json"),
                        "owned_paths": list(st.owned_paths),
                        "review_feedback": ex.review_feedback,
                        "prompt": self._subtask_prompt(s, ws, st, ex.review_feedback),
                    })
        elif s.stage == "reviewing" and s.locked_plan:
            codex_owned = [st for st in s.locked_plan.subtasks if st.owner == "codex"]
            gemini_owned = [st for st in s.locked_plan.subtasks if st.owner == "gemini"]
            if cfg.modules.codex_enabled and gemini_owned and "codex" not in s.reviews:
                codex_ready = True
            if cfg.modules.gemini_enabled and codex_owned and "gemini" not in s.reviews:
                pending_gemini.append({
                    "action": "review_peer",
                    "stage": "reviewing",
                    "round": s.review_round,
                    "target_tasks": [st.id for st in codex_owned],
                    "prompt": self._mutual_review_prompt(s, ws, "gemini", codex_owned),
                    "schema": MUTUAL_REVIEW_SCHEMA,
                })

        return {
            "session_id": s.id,
            "stage": s.stage,
            "codex_busy": self.is_codex_busy(s.id),
            "codex_ready": codex_ready,
            "awaiting_user_decision": awaiting_user,
            "pending_gemini": pending_gemini,
        }

    # ---------- Prompt Builders (Driven Live by Editable PipelineConfig) ----------
    def _compose_prompt(self, model: Owner, body: str) -> str:
        cfg = self.get_pipeline_config()
        persona = cfg.gemini.system_persona if model == "gemini" else cfg.codex.system_persona
        shared = cfg.shared.global_directives.strip()
        parts = [persona.strip()]
        if shared:
            parts.append(f"【共享全局约定】\n{shared}")
        parts.append(body.strip())
        return "\n\n".join(p for p in parts if p)

    def _proposal_prompt(self, s: CollabSession, ws: Path, model: Owner) -> str:
        cfg = self.get_pipeline_config()
        mode_note = (
            "开发任务模式：请提出清晰的架构设计、关键边界与验证方案。"
            if s.mode == "dev"
            else "学习与研究模式：请设计循序渐进的知识框架、核心概念拆解与实操练习方向。"
        )
        template = cfg.gemini.proposal_prompt if model == "gemini" else cfg.codex.proposal_prompt
        body = safe_format(template, {
            "model": model.upper(),
            "mode_note": mode_note,
            "task": s.task,
            "repo_overview": _repo_overview(ws),
        })
        return self._compose_prompt(model, body)

    def _critique_prompt(
        self, s: CollabSession, ws: Path, model: Owner, own: Proposal, peer: Proposal
    ) -> str:
        cfg = self.get_pipeline_config()
        peer_name = "CODEX" if model == "gemini" else "GEMINI"
        path_rule = (
            "每个子任务的 owned_paths 必须是精确的相对文件路径（如 solution.py、tests/test_solution.py）。"
            if s.mode == "dev"
            else "每个子任务的 owned_paths 必须是互斥的 Markdown 章节文件（如 sections/01_concepts.md、sections/02_examples.md）。"
        )
        template = cfg.gemini.critique_prompt if model == "gemini" else cfg.codex.critique_prompt
        body = safe_format(template, {
            "model": model.upper(),
            "peer_name": peer_name,
            "task": s.task,
            "own_json": own.model_dump_json(indent=2),
            "peer_json": peer.model_dump_json(indent=2),
            "path_rule": path_rule,
            "division_policy": cfg.shared.division_policy,
            "max_subtasks": cfg.stages.max_subtasks,
        })
        return self._compose_prompt(model, body)

    def _merge_prompt(self, s: CollabSession) -> str:
        cfg = self.get_pipeline_config()
        body = safe_format(cfg.gemini.merge_prompt, {
            "task": s.task,
            "choice": s.decision.choice if s.decision else "merge",
            "base_model": s.decision.base_model if s.decision else "gemini",
            "fusion_notes": s.decision.fusion_notes if s.decision else "",
            "gemini_refined": s.refined["gemini"].model_dump_json(indent=2) if "gemini" in s.refined else "{}",
            "codex_refined": s.refined["codex"].model_dump_json(indent=2) if "codex" in s.refined else "{}",
            "division_policy": cfg.shared.division_policy,
            "max_subtasks": cfg.stages.max_subtasks,
        })
        return self._compose_prompt("gemini", body)

    def _subtask_prompt(self, s: CollabSession, ws: Path, st: SubTask, feedback: str) -> str:
        cfg = self.get_pipeline_config()
        existing_blocks = []
        for path in st.owned_paths:
            existing_blocks.append(f"--- {path} ---\n{read_context(ws, path, 6000)}")
        dep_blocks = []
        for dep_id in st.depends_on:
            dep_ex = s.executions.get(dep_id)
            if dep_ex:
                dep_blocks.append(f"[{dep_id} ({dep_ex.owner})]: {dep_ex.summary}")
                for dp in dep_ex.owned_paths[:2]:
                    dep_blocks.append(f"  File {dp}:\n{read_context(ws, dp, 3000)}")
        fb_block = f"\n【对方交叉审查打回的修复意见（务必逐项修复）】\n{feedback}\n" if feedback else ""
        template = cfg.gemini.subtask_prompt if st.owner == "gemini" else cfg.codex.subtask_prompt
        body = safe_format(template, {
            "owner": st.owner.upper(),
            "task": s.task,
            "locked_summary": s.locked_plan.summary if s.locked_plan else "",
            "task_id": st.id,
            "task_title": st.title,
            "objective": st.objective,
            "acceptance": st.acceptance,
            "owned_paths": ", ".join(st.owned_paths),
            "feedback_block": fb_block,
            "dep_blocks": "\n".join(dep_blocks) or "(无)",
            "existing_blocks": "\n".join(existing_blocks),
        })
        return self._compose_prompt(st.owner, body)

    def _mutual_review_prompt(self, s: CollabSession, ws: Path, reviewer: Owner, targets: list[SubTask]) -> str:
        cfg = self.get_pipeline_config()
        target_owner = "CODEX" if reviewer == "gemini" else "GEMINI"
        blocks = []
        for st in targets:
            ex = s.executions.get(st.id)
            blocks.append(
                f"=== 子任务 {st.id}: {st.title} (owner={st.owner}) ===\n"
                f"要求：{st.objective}\n验收标准：{st.acceptance}\n"
                f"执行摘要：{ex.summary if ex else ''}\n"
                + (f"本地检查输出：{ex.check_output}\n" if ex and ex.check_output else "")
            )
            for p in st.owned_paths:
                blocks.append(f"--- 文件 {p} ---\n{read_context(ws, p, 8000)}")
        template = cfg.gemini.review_prompt if reviewer == "gemini" else cfg.codex.review_prompt
        body = safe_format(template, {
            "reviewer": reviewer.upper(),
            "target_owner": target_owner,
            "review_round": s.review_round,
            "task": s.task,
            "target_blocks": "\n".join(blocks),
        })
        return self._compose_prompt(reviewer, body)

    # ---------- Dynamic Pipeline Stage Advancement (Obeys Live PipelineConfig) ----------
    def _synthesize_direct_subtasks(self, s: CollabSession, cfg: PipelineConfig) -> list[SubTask]:
        tasks: list[SubTask] = []
        if cfg.modules.gemini_enabled and "gemini" in s.proposals:
            path = "sections/01_gemini_core.md" if s.mode == "learn" else "gemini_part.md"
            tasks.append(SubTask(
                id="T1",
                title="核心思路与结构化文档" if s.mode == "learn" else "核心架构与设计交付",
                owner="gemini",
                objective=s.proposals["gemini"].summary[:300],
                owned_paths=[path],
                acceptance="内容完整、清晰、无越界写入。",
            ))
        if cfg.modules.codex_enabled and "codex" in s.proposals:
            path = "sections/02_codex_examples.md" if s.mode == "learn" else "solution.py"
            tid = "T2" if tasks else "T1"
            tasks.append(SubTask(
                id=tid,
                title="实战代码示例与避坑验证" if s.mode == "learn" else "核心代码实现与验证",
                owner="codex",
                objective=s.proposals["codex"].summary[:300],
                owned_paths=[path],
                acceptance="实现正确、可运行验证且无越界写入。",
            ))
        return tasks

    def _advance_after_proposal(self, s: CollabSession) -> None:
        cfg = self.get_pipeline_config()
        need_g = cfg.modules.gemini_enabled
        need_c = cfg.modules.codex_enabled
        if (need_g and "gemini" not in s.proposals) or (need_c and "codex" not in s.proposals):
            return
        if cfg.stages.enable_critique and need_g and need_c:
            s.stage = "critiquing"
            return
        # Stage 2 skipped (disabled by user or solo-model mode): synthesize conflict-free refined wrappers
        subtasks = self._synthesize_direct_subtasks(s, cfg)
        div = validate_division(subtasks)
        for owner, prop in s.proposals.items():
            if owner not in s.refined:
                s.refined[owner] = RefinedProposal(
                    model=owner,  # type: ignore[arg-type]
                    critique_of_peer="阶段 2 交叉互审已按当前配置跳过。",
                    adopted_from_peer="直接沿用阶段 1 初案生成零重叠分工。",
                    summary=prop.summary,
                    approach=prop.approach,
                    subtasks=subtasks,
                    division_check=div,
                )
        self._advance_after_critique(s)

    def _advance_after_critique(self, s: CollabSession) -> None:
        cfg = self.get_pipeline_config()
        need_g = cfg.modules.gemini_enabled
        need_c = cfg.modules.codex_enabled
        if (need_g and "gemini" not in s.refined) or (need_c and "codex" not in s.refined):
            return
        if cfg.stages.require_user_decision:
            s.stage = "deciding"
            return
        # Auto-select winner when manual decision gate is disabled
        preferred = cfg.stages.default_winner
        chosen_owner = preferred if preferred in s.refined and s.refined[preferred].division_check.valid else None
        if not chosen_owner:
            chosen_owner = next(
                (m for m, ref in s.refined.items() if ref.division_check.valid),
                None,
            )
        if not chosen_owner:
            s.stage = "deciding"
            return
        chosen = s.refined[chosen_owner]
        s.decision = UserDecision(
            choice=chosen_owner,  # type: ignore[arg-type]
            base_model=chosen_owner,  # type: ignore[arg-type]
            fusion_notes="自动拍板模式（require_user_decision=false）",
        )
        self._lock_plan(
            s,
            LockedPlan(
                source=chosen_owner,  # type: ignore[arg-type]
                summary=chosen.summary,
                approach=chosen.approach,
                fusion_notes="",
                subtasks=chosen.subtasks,
                division_check=chosen.division_check,
            ),
        )

    def _finalize_completed(self, s: CollabSession, ws: Path, summary_text: str) -> None:
        cfg = self.get_pipeline_config()
        if s.mode == "learn":
            guide = _compile_study_guide(s, ws, header_note=cfg.shared.study_guide_header)
            guide_out = self.sessions_dir / s.id / "study-guide.md"
            write_atomic(guide_out, guide.encode("utf-8"), mode=0o600)
            write_atomic(ws / "STUDY_GUIDE.md", guide.encode("utf-8"), mode=0o644)
            s.study_guide_path = str(guide_out)
            self.record_event(
                s.id,
                category="workspace",
                event_type="study_guide.compiled",
                actor="aisys",
                stage="completed",
                summary=f"已自动编译结构化 Markdown 学习文档（{len(guide)} 字符，含 {len(s.review_history)} 条互审记录）",
                detail={"chars": len(guide), "reviews": len(s.review_history)},
            )
        prev = s.stage
        s.final_summary = summary_text
        s.stage = "completed"
        self.record_event(
            s.id,
            category="engine",
            event_type="stage.transition",
            actor="aisys",
            stage="completed",
            summary=f"状态机跃迁：{prev} → completed（{s.final_summary}）",
            detail={"from": prev, "to": "completed", "rounds": s.review_round},
        )

    # ---------- State Transitions (Gemini / User / Codex) ----------
    def submit_gemini_proposal(self, session_id: str, payload: dict, *, auto_codex: bool = True) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage != "proposing":
                raise ValueError(f"当前阶段为 {s.stage}，不接收初版提案")
            prop = Proposal(
                model="gemini",
                summary=redact(str(payload.get("summary", "")).strip()),
                approach=redact(str(payload.get("approach", "")).strip()),
                strengths=[redact(str(x)) for x in payload.get("strengths", [])[:12]],
                risks=[redact(str(x)) for x in payload.get("risks", [])[:12]],
            )
            s.proposals["gemini"] = prop
            prev_stage = s.stage
            self._advance_after_proposal(s)
            self._save(s)
        self.record_event(
            session_id,
            category="gemini",
            event_type="gemini.submitted",
            actor="gemini",
            stage=prev_stage,
            summary=f"Gemini 提交阶段 1 独立提案（{len(prop.approach)} 字符，{len(prop.strengths)} 项亮点）",
            detail={"action": "propose", "summary": prop.summary[:220], "chars": len(prop.approach)},
        )
        if s.stage != prev_stage:
            self.record_event(
                session_id,
                category="engine",
                event_type="stage.transition",
                actor="aisys",
                stage=s.stage,
                summary=f"状态机跃迁：{prev_stage} → {s.stage}",
                detail={"from": prev_stage, "to": s.stage},
            )
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex and self.get_pipeline_config().modules.codex_enabled:
            self.trigger_codex_async(session_id)
        return self.get_session_view(session_id)

    def submit_gemini_critique(self, session_id: str, payload: dict, *, auto_codex: bool = True) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage != "critiquing":
                raise ValueError(f"当前阶段为 {s.stage}，不接收互审升级方案")
            subtasks = [SubTask.model_validate(item) for item in payload.get("subtasks", [])]
            div = validate_division(subtasks)
            ref = RefinedProposal(
                model="gemini",
                critique_of_peer=redact(str(payload.get("critique_of_peer", "")).strip()),
                adopted_from_peer=redact(str(payload.get("adopted_from_peer", "")).strip()),
                summary=redact(str(payload.get("summary", "")).strip()),
                approach=redact(str(payload.get("approach", "")).strip()),
                subtasks=subtasks,
                division_check=div,
            )
            s.refined["gemini"] = ref
            prev_stage = s.stage
            self._advance_after_critique(s)
            self._save(s)
        self.record_event(
            session_id,
            category="lock",
            event_type="division.validated",
            actor="aisys",
            stage=prev_stage,
            summary=(
                f"防重叠算法校验 Gemini 分工表：{'通过' if div.valid else '发现冲突'}"
                f"（{len(subtasks)} 任务，{len(div.waves)} 波次）"
            ),
            detail={
                "model": "gemini",
                "valid": div.valid,
                "task_count": len(subtasks),
                "waves": div.waves,
                "conflicts": div.conflicts,
                "warnings": div.warnings,
            },
        )
        self.record_event(
            session_id,
            category="gemini",
            event_type="gemini.submitted",
            actor="gemini",
            stage=prev_stage,
            summary=f"Gemini 完成交叉互审并提出 {len(subtasks)} 项互斥分工",
            detail={"action": "critique", "summary": ref.summary[:220], "division_valid": div.valid},
        )
        if s.stage != prev_stage:
            self.record_event(
                session_id,
                category="engine",
                event_type="stage.transition",
                actor="aisys",
                stage=s.stage,
                summary=f"状态机跃迁：{prev_stage} → {s.stage}（等待你拍板或融合微调）",
                detail={"from": prev_stage, "to": s.stage},
            )
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex and s.stage == "critiquing":
            self.trigger_codex_async(session_id)
        return self.get_session_view(session_id)

    def decide(
        self,
        session_id: str,
        *,
        choice: Literal["gemini", "codex", "merge"],
        base_model: Owner | None = None,
        fusion_notes: str = "",
        subtasks_override: list[dict] | None = None,
        auto_codex: bool = True,
    ) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage != "deciding":
                raise ValueError(f"当前阶段为 {s.stage}，尚未进入用户拍板阶段")
            resolved_base: Owner = base_model or ("codex" if choice == "codex" else "gemini")
            notes = redact(fusion_notes.strip())
            s.decision = UserDecision(choice=choice, base_model=resolved_base, fusion_notes=notes)

            # Direct adoption (or direct subtasks_override) locks immediately if division is conflict-free!
            if subtasks_override is not None:
                parsed = [SubTask.model_validate(item) for item in subtasks_override]
                div = validate_division(parsed)
                if not div.valid:
                    raise ValueError("分工表存在重叠或依赖冲突：" + "；".join(div.conflicts))
                base_ref = s.refined[resolved_base]
                self._lock_plan(
                    s,
                    LockedPlan(
                        source="merged" if choice == "merge" or notes else choice,
                        summary=base_ref.summary,
                        approach=base_ref.approach,
                        fusion_notes=notes,
                        subtasks=parsed,
                        division_check=div,
                    ),
                )
            elif choice in {"gemini", "codex"} and not notes:
                chosen = s.refined.get(choice)
                if not chosen:
                    raise ValueError(f"模型 {choice} 尚未生成升级方案")
                if not chosen.division_check.valid:
                    # Force merge/fix step if model's division had an overlap conflict
                    s.stage = "merging"
                else:
                    self._lock_plan(
                        s,
                        LockedPlan(
                            source=choice,
                            summary=chosen.summary,
                            approach=chosen.approach,
                            fusion_notes="",
                            subtasks=chosen.subtasks,
                            division_check=chosen.division_check,
                        ),
                    )
            else:
                # Fusion tweak requested -> enter merging stage so Gemini/Antigravity produces fused conflict-free plan
                s.stage = "merging"
            self._save(s)
        self.record_event(
            session_id,
            category="engine",
            event_type="user.decided",
            actor="user",
            stage=s.stage,
            summary=(
                f"用户拍板：{choice}"
                + (f"（含融合微调意见）→ {s.stage}" if notes else f" → {s.stage}")
            ),
            detail={
                "choice": choice,
                "base_model": resolved_base,
                "has_fusion_notes": bool(notes),
                "next_stage": s.stage,
                "locked_waves": s.locked_plan.division_check.waves if s.locked_plan else [],
            },
        )
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex and s.stage == "executing":
            self.trigger_codex_async(session_id)
        return self.get_session_view(session_id)

    def submit_merged_plan(self, session_id: str, payload: dict, *, auto_codex: bool = True) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage != "merging":
                raise ValueError(f"当前阶段为 {s.stage}，不在融合微调阶段")
            subtasks = [SubTask.model_validate(item) for item in payload.get("subtasks", [])]
            div = validate_division(subtasks)
            if not div.valid:
                self.record_event(
                    session_id,
                    category="lock",
                    event_type="division.validated",
                    actor="aisys",
                    stage="merging",
                    summary="防重叠算法拒绝融合分工表：" + "；".join(div.conflicts),
                    detail={"valid": False, "conflicts": div.conflicts},
                )
                raise ValueError("融合后的分工表仍存在重叠或依赖冲突：" + "；".join(div.conflicts))
            self._lock_plan(
                s,
                LockedPlan(
                    source="merged",
                    summary=redact(str(payload.get("summary", "")).strip()),
                    approach=redact(str(payload.get("approach", "")).strip()),
                    fusion_notes=s.decision.fusion_notes if s.decision else "",
                    subtasks=subtasks,
                    division_check=div,
                ),
            )
            self._save(s)
        self.record_event(
            session_id,
            category="lock",
            event_type="division.validated",
            actor="aisys",
            stage="executing",
            summary=f"融合分工锁已生效（{len(subtasks)} 任务，{len(div.waves)} 波次，0 路径冲突）",
            detail={"valid": True, "waves": div.waves, "task_count": len(subtasks)},
        )
        self.record_event(
            session_id,
            category="gemini",
            event_type="gemini.submitted",
            actor="gemini",
            stage="executing",
            summary="Gemini 提交融合锁定方案，进入阶段 4 互斥协同执行",
            detail={"action": "merge", "task_count": len(subtasks)},
        )
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex:
            self.trigger_codex_async(session_id)
        return self.get_session_view(session_id)

    def _lock_plan(self, s: CollabSession, plan: LockedPlan) -> None:
        s.locked_plan = plan
        s.executions = {
            st.id: TaskExecution(
                task_id=st.id,
                owner=st.owner,
                title=st.title,
                owned_paths=list(st.owned_paths),
                status="pending",
            )
            for st in plan.subtasks
        }
        s.stage = "executing"

    def submit_gemini_subtask(
        self,
        session_id: str,
        *,
        task_id: str,
        summary: str,
        files: list[dict],
        auto_codex: bool = True,
    ) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage != "executing" or not s.locked_plan:
                raise ValueError(f"当前阶段为 {s.stage}，不接收子任务交付")
            st = next((t for t in s.locked_plan.subtasks if t.id == task_id), None)
            if not st:
                raise NotFoundError(f"子任务 '{task_id}' 不存在")
            if st.owner != "gemini":
                raise ValueError(f"子任务 '{task_id}' 归属于 {st.owner}，禁止 Gemini 越权修改")
            ex = s.executions[task_id]
            if ex.status not in {"pending", "needs_fix"}:
                raise ValueError(f"子任务 '{task_id}' 当前状态为 {ex.status}")
            for dep in st.depends_on:
                if s.executions[dep].status != "completed":
                    raise ValueError(f"前置依赖 '{dep}' 尚未完成")

            edits = [Edit.model_validate(item) for item in files]
            ws = self._workspace(session_id)
            # Mechanical hard write lock: strictly restricted to st.owned_paths!
            written = self._apply_edits_audited(
                session_id,
                owner="gemini",
                task_id=task_id,
                stage="executing",
                ws=ws,
                edits=edits,
                allowed=st.owned_paths,
            )
            check_out = ""
            if st.check_argv and self.get_pipeline_config().modules.sandbox_checks_enabled:
                chk = run_check(st.check_argv, ws, 30)
                check_out = redact((chk.stdout + "\n" + chk.stderr).strip())[:4000]
                passed = not chk.issue and chk.code == 0
                self.record_event(
                    session_id,
                    category="workspace",
                    event_type="sandbox.check",
                    actor="aisys",
                    stage="executing",
                    summary=f"沙箱验收 {task_id}：{'通过' if passed else '未通过'} (exit={chk.code})",
                    detail={"task_id": task_id, "exit_code": chk.code, "issue": chk.issue, "output": check_out[:300]},
                )
                if not passed:
                    raise ValueError(f"子任务 {task_id} 沙箱校验未通过：{check_out}")

            ex.status = "completed"
            ex.summary = redact(summary.strip())[:8000]
            ex.artifacts = sorted(set(ex.artifacts) | set(written))
            ex.check_output = check_out
            ex.error = ""
            ex.attempts += 1
            ex.updated_at = time.time()
            self._maybe_advance_after_execution(s, ws)
            self._save(s)
        self.record_event(
            session_id,
            category="gemini",
            event_type="gemini.submitted",
            actor="gemini",
            stage=s.stage,
            summary=f"Gemini 完成子任务 {task_id}（写入 {len(written)} 个独占文件）",
            detail={"action": "execute_subtask", "task_id": task_id, "files": written},
        )
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex and s.stage in {"executing", "reviewing"}:
            self.trigger_codex_async(session_id)
        return self.get_session_view(session_id)

    def submit_gemini_review(self, session_id: str, payload: dict, *, auto_codex: bool = True) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage != "reviewing" or not s.locked_plan:
                raise ValueError(f"当前阶段为 {s.stage}，不在互相审查阶段")
            codex_ids = {st.id for st in s.locked_plan.subtasks if st.owner == "codex"}
            fix_reqs = [FixRequest.model_validate(f) for f in payload.get("fix_requests", [])]
            for fr in fix_reqs:
                if fr.task_id not in codex_ids:
                    raise ValueError(f"Gemini 审查只能指向 Codex 负责的子任务，'{fr.task_id}' 不属于 Codex")
            review = MutualReview(
                reviewer="gemini",
                target_owner="codex",
                round=s.review_round,
                verdict=payload["verdict"],
                summary=redact(str(payload.get("summary", "")).strip()),
                findings=[redact(str(x)) for x in payload.get("findings", [])[:20]],
                fix_requests=fix_reqs,
            )
            s.reviews["gemini"] = review
            s.review_history.append(review)
            self.record_event(
                session_id,
                category="gemini",
                event_type="gemini.submitted",
                actor="gemini",
                stage="reviewing",
                summary=f"Gemini 完成第 {review.round} 轮交叉审查（审查 Codex → {review.verdict.upper()}）",
                detail={
                    "action": "review_peer",
                    "round": review.round,
                    "verdict": review.verdict,
                    "fix_count": len(fix_reqs),
                    "summary": review.summary[:220],
                },
            )
            ws = self._workspace(session_id)
            self._maybe_finalize_review_round(s, ws)
            self._save(s)
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex and s.stage in {"executing", "reviewing"}:
            self.trigger_codex_async(session_id)
        return self.get_session_view(session_id)

    def cancel_session(self, session_id: str) -> dict:
        with self._exclusive(session_id):
            s = self._load(session_id)
            if s.stage in {"completed", "failed", "cancelled"}:
                return self.get_session_view(session_id)
            prev_stage = s.stage
            s.stage = "cancelled"
            s.error = "已由用户取消"
            self._save(s)
        self.record_event(
            session_id,
            category="engine",
            event_type="session.cancelled",
            actor="user",
            stage="cancelled",
            summary=f"会话已在 {prev_stage} 阶段取消",
            detail={"from": prev_stage},
        )
        self.publish("sessions_changed", {"ids": [session_id]})
        return self.get_session_view(session_id)

    def _maybe_advance_after_execution(self, s: CollabSession, ws: Path) -> None:
        if not s.locked_plan:
            return
        if all(ex.status == "completed" for ex in s.executions.values()):
            cfg = self.get_pipeline_config()
            if not cfg.stages.enable_mutual_review:
                self._finalize_completed(
                    s, ws,
                    f"全部 {len(s.executions)} 项子任务已完成（阶段 5 互相审查按配置跳过）。",
                )
                return
            s.stage = "reviewing"
            s.reviews = {}
            self.record_event(
                s.id,
                category="engine",
                event_type="stage.transition",
                actor="aisys",
                stage="reviewing",
                summary=f"状态机跃迁：executing → reviewing（全部 {len(s.executions)} 项子任务完成，启动第 {s.review_round} 轮交叉审查）",
                detail={"from": "executing", "to": "reviewing", "round": s.review_round},
            )
            # Auto-pass side if a model had 0 assigned subtasks or reviewer is disabled
            if not cfg.modules.gemini_enabled or not any(st.owner == "codex" for st in s.locked_plan.subtasks):
                s.reviews["gemini"] = MutualReview(
                    reviewer="gemini", target_owner="codex", round=s.review_round,
                    verdict="pass", summary="无 Codex 子任务需审查或 Gemini 已关闭（自动跳过）。",
                )
            if not cfg.modules.codex_enabled or not any(st.owner == "gemini" for st in s.locked_plan.subtasks):
                s.reviews["codex"] = MutualReview(
                    reviewer="codex", target_owner="gemini", round=s.review_round,
                    verdict="pass", summary="无 Gemini 子任务需审查或 Codex 已关闭（自动跳过）。",
                )
            self._maybe_finalize_review_round(s, ws)

    def _maybe_finalize_review_round(self, s: CollabSession, ws: Path) -> None:
        if "gemini" not in s.reviews or "codex" not in s.reviews:
            return
        g_rev, c_rev = s.reviews["gemini"], s.reviews["codex"]
        failed_requests: list[FixRequest] = []
        if g_rev.verdict == "fail":
            failed_requests.extend(g_rev.fix_requests)
            if not g_rev.fix_requests and s.locked_plan:
                for st in s.locked_plan.subtasks:
                    if st.owner == "codex":
                        failed_requests.append(FixRequest(task_id=st.id, feedback=g_rev.summary))
        if c_rev.verdict == "fail":
            failed_requests.extend(c_rev.fix_requests)
            if not c_rev.fix_requests and s.locked_plan:
                for st in s.locked_plan.subtasks:
                    if st.owner == "gemini":
                        failed_requests.append(FixRequest(task_id=st.id, feedback=c_rev.summary))

        if failed_requests and s.review_round < s.max_review_rounds:
            grouped: dict[str, list[str]] = {}
            for fr in failed_requests:
                if fr.task_id in s.executions:
                    grouped.setdefault(fr.task_id, []).append(redact(fr.feedback))
            for tid, items in grouped.items():
                ex = s.executions[tid]
                ex.status = "needs_fix"
                ex.review_feedback = "\n".join(f"- {fb}" for fb in items)
            s.review_round += 1
            s.reviews = {}
            s.stage = "executing"
            self.record_event(
                s.id,
                category="engine",
                event_type="stage.transition",
                actor="aisys",
                stage="executing",
                summary=f"交叉审查触发定向返工：{', '.join(grouped.keys())}（保留其他已完成成果）",
                detail={"from": "reviewing", "to": "executing", "rework_tasks": list(grouped.keys()), "next_round": s.review_round},
            )
            return

        self._finalize_completed(
            s,
            ws,
            f"双模型协同与交叉审查已完成（共 {s.review_round} 轮审查，"
            f"Gemini 审查：{g_rev.verdict}，Codex 审查：{c_rev.verdict}）。",
        )

    # ---------- Automated Codex Driver ----------
    def trigger_codex_async(self, session_id: str) -> None:
        if self.sync_codex:
            self.drive_codex(session_id)
            return
        with self._lock:
            if session_id in self._active_codex_sessions:
                return
            self._active_codex_sessions.add(session_id)
        threading.Thread(
            target=self._codex_worker_wrapper,
            args=(session_id,),
            name=f"aisys-codex-{session_id[:8]}",
            daemon=True,
        ).start()

    def _codex_worker_wrapper(self, session_id: str) -> None:
        try:
            self.drive_codex(session_id)
        finally:
            with self._lock:
                self._active_codex_sessions.discard(session_id)
            self.publish("sessions_changed", {"ids": [session_id]})

    def drive_codex(self, session_id: str) -> dict:
        """Synchronously advance all currently eligible Codex steps for this session."""
        with self._lock:
            self._active_codex_sessions.add(session_id)
        try:
            for _ in range(12):
                progressed = self._step_codex_once(session_id)
                if not progressed:
                    break
                self.publish("sessions_changed", {"ids": [session_id]})
        finally:
            with self._lock:
                self._active_codex_sessions.discard(session_id)
        self.publish("sessions_changed", {"ids": [session_id]})
        return self.get_session_view(session_id)

    def _step_codex_once(self, session_id: str) -> bool:
        cfg = self.get_pipeline_config()
        if not cfg.modules.codex_enabled:
            return False
        s = self._load(session_id)
        ws = self._workspace(session_id)
        broker = self.sessions_dir / session_id / "provider" / "codex"
        broker.mkdir(mode=0o700, exist_ok=True)
        codex = self._codex()
        codex_model = cfg.codex.model.strip() or None
        codex_timeout = float(cfg.codex.timeout_s)

        def _call_run_json(*, prompt: str, schema: dict, timeout: float, stage: str, step_label: str):
            self._set_active_process(session_id, {
                "actor": "codex",
                "stage": stage,
                "step": step_label,
                "started_at": time.time(),
                "pid": None,
                "thread_id": None,
                "latest_event": f"正在准备 {step_label}…",
            })
            self.record_event(
                session_id,
                category="codex",
                event_type="codex.step.started",
                actor="codex",
                stage=stage,
                summary=f"调度 Codex 执行：{step_label}（prompt={len(prompt)} 字符，timeout={int(timeout)}s）",
                detail={"step": step_label, "model": codex_model or "default", "prompt_chars": len(prompt), "timeout_s": timeout},
            )
            self.publish("sessions_changed", {"ids": [session_id]})
            handler = self._make_codex_stream_handler(session_id, stage=stage, step_label=step_label)
            try:
                kwargs = {"prompt": prompt, "schema": schema, "cwd": broker, "timeout": timeout}
                if codex_model and self._supports_kwarg(codex.run_json, "model"):
                    kwargs["model"] = codex_model
                if self._supports_kwarg(codex.run_json, "on_event"):
                    kwargs["on_event"] = handler
                return codex.run_json(**kwargs)
            finally:
                self._set_active_process(session_id, None)

        # Stage 1: Codex independent proposal
        if s.stage == "proposing" and "codex" not in s.proposals:
            prompt = self._proposal_prompt(s, ws, "codex")
            res, data = _call_run_json(
                prompt=prompt, schema=PROPOSAL_SCHEMA, timeout=codex_timeout,
                stage="proposing", step_label="阶段 1 独立初案",
            )
            with self._exclusive(session_id):
                s = self._load(session_id)
                if s.stage != "proposing":
                    return False
                if res.status != "ok" or not data:
                    s.error = redact(res.error or f"Codex Stage 1 failed ({res.status})")
                    self._save(s)
                    self.record_event(
                        session_id, category="codex", event_type="codex.step.failed",
                        actor="codex", stage="proposing",
                        summary=f"Codex 阶段 1 失败：{s.error}",
                        detail={"status": res.status, "duration": res.duration},
                    )
                    return False
                s.error = ""
                s.proposals["codex"] = Proposal(
                    model="codex",
                    summary=redact(data["summary"]),
                    approach=redact(data["approach"]),
                    strengths=[redact(x) for x in data.get("strengths", [])[:12]],
                    risks=[redact(x) for x in data.get("risks", [])[:12]],
                    duration=res.duration,
                    usage=res.usage,
                )
                prev_stage = s.stage
                self._advance_after_proposal(s)
                self._save(s)
            self.record_event(
                session_id, category="codex", event_type="codex.step.completed",
                actor="codex", stage=prev_stage,
                summary=f"Codex 完成阶段 1 独立初案（{res.duration}s）",
                detail={"duration": res.duration, "usage": res.usage, "summary": data["summary"][:200]},
            )
            if s.stage != prev_stage:
                self.record_event(
                    session_id, category="engine", event_type="stage.transition",
                    actor="aisys", stage=s.stage,
                    summary=f"状态机跃迁：{prev_stage} → {s.stage}",
                    detail={"from": prev_stage, "to": s.stage},
                )
            return True

        # Stage 2: Codex cross-critique + upgraded plan + division
        if s.stage == "critiquing" and "codex" not in s.refined and "gemini" in s.proposals and "codex" in s.proposals:
            prompt = self._critique_prompt(s, ws, "codex", s.proposals["codex"], s.proposals["gemini"])
            res, data = _call_run_json(
                prompt=prompt, schema=REFINED_SCHEMA, timeout=max(codex_timeout, 180.0),
                stage="critiquing", step_label="阶段 2 交叉互审与分工",
            )
            with self._exclusive(session_id):
                s = self._load(session_id)
                if s.stage != "critiquing":
                    return False
                if res.status != "ok" or not data:
                    s.error = redact(res.error or f"Codex Stage 2 failed ({res.status})")
                    self._save(s)
                    self.record_event(
                        session_id, category="codex", event_type="codex.step.failed",
                        actor="codex", stage="critiquing",
                        summary=f"Codex 阶段 2 失败：{s.error}",
                        detail={"status": res.status, "duration": res.duration},
                    )
                    return False
                subtasks = []
                parse_err = []
                for item in data.get("subtasks", []):
                    try:
                        subtasks.append(SubTask.model_validate(item))
                    except ValueError as err:
                        parse_err.append(str(err))
                div = validate_division(subtasks) if not parse_err else DivisionValidation(
                    valid=False, conflicts=parse_err,
                )
                s.error = ""
                s.refined["codex"] = RefinedProposal(
                    model="codex",
                    critique_of_peer=redact(data["critique_of_peer"]),
                    adopted_from_peer=redact(data["adopted_from_peer"]),
                    summary=redact(data["summary"]),
                    approach=redact(data["approach"]),
                    subtasks=subtasks,
                    division_check=div,
                    duration=res.duration,
                    usage=res.usage,
                )
                prev_stage = s.stage
                self._advance_after_critique(s)
                self._save(s)
            self.record_event(
                session_id, category="lock", event_type="division.validated",
                actor="aisys", stage=prev_stage,
                summary=(
                    f"防重叠算法校验 Codex 分工表：{'通过' if div.valid else '发现冲突'}"
                    f"（{len(subtasks)} 任务，{len(div.waves)} 波次）"
                ),
                detail={"model": "codex", "valid": div.valid, "waves": div.waves, "conflicts": div.conflicts},
            )
            self.record_event(
                session_id, category="codex", event_type="codex.step.completed",
                actor="codex", stage=prev_stage,
                summary=f"Codex 完成阶段 2 交叉互审与分工（{res.duration}s）",
                detail={"duration": res.duration, "usage": res.usage, "division_valid": div.valid},
            )
            if s.stage != prev_stage:
                self.record_event(
                    session_id, category="engine", event_type="stage.transition",
                    actor="aisys", stage=s.stage,
                    summary=f"状态机跃迁：{prev_stage} → {s.stage}",
                    detail={"from": prev_stage, "to": s.stage},
                )
            return True

        # Stage 4: Execute next ready Codex subtask with hard owned_paths lock
        if s.stage == "executing" and s.locked_plan:
            ready_task: SubTask | None = None
            for st in s.locked_plan.subtasks:
                ex = s.executions.get(st.id)
                if st.owner != "codex" or not ex or ex.status not in {"pending", "needs_fix"}:
                    continue
                if all(s.executions[dep].status == "completed" for dep in st.depends_on):
                    ready_task = st
                    break
            if ready_task is None:
                return False

            with self._exclusive(session_id):
                s = self._load(session_id)
                ex = s.executions[ready_task.id]
                ex.status = "running"
                ex.updated_at = time.time()
                self._save(s)
            step_label = f"阶段 4 子任务 {ready_task.id} · {ready_task.title}"
            self._set_active_process(session_id, {
                "actor": "codex",
                "stage": "executing",
                "step": step_label,
                "task_id": ready_task.id,
                "started_at": time.time(),
                "pid": None,
                "thread_id": None,
                "latest_event": f"正在执行 {ready_task.id}…",
            })
            self.record_event(
                session_id, category="codex", event_type="codex.step.started",
                actor="codex", stage="executing",
                summary=f"调度 Codex 执行子任务 {ready_task.id}（写锁白名单：{', '.join(ready_task.owned_paths)}）",
                detail={"task_id": ready_task.id, "owned_paths": list(ready_task.owned_paths)},
            )
            self.publish("sessions_changed", {"ids": [session_id]})

            prompt = self._subtask_prompt(s, ws, ready_task, s.executions[ready_task.id].review_feedback)
            agent = Agent(
                role=f"Codex Executor ({ready_task.id})",
                backend="codex",
                model=codex_model,
                instructions=ready_task.objective,
                write_paths=list(ready_task.owned_paths),
            )
            handler = self._make_codex_stream_handler(session_id, stage="executing", step_label=step_label)
            try:
                run_kwargs = {"prompt": prompt, "agent": agent, "cwd": broker, "timeout": max(codex_timeout, 180.0)}
                if self._supports_kwarg(codex.run, "on_event"):
                    run_kwargs["on_event"] = handler
                res = codex.run(**run_kwargs)
            finally:
                self._set_active_process(session_id, None)
            with self._exclusive(session_id):
                s = self._load(session_id)
                ex = s.executions[ready_task.id]
                ex.attempts += 1
                ex.duration = res.duration
                ex.usage = res.usage
                ex.updated_at = time.time()
                if res.status != "ok" or not res.reply:
                    ex.status = "failed"
                    ex.error = redact(res.error or f"Codex failed ({res.status})")
                    s.error = ex.error
                    self._save(s)
                    self.record_event(
                        session_id, category="codex", event_type="codex.step.failed",
                        actor="codex", stage="executing",
                        summary=f"Codex 子任务 {ready_task.id} 失败：{ex.error}",
                        detail={"task_id": ready_task.id, "status": res.status},
                    )
                    return False
                try:
                    written = self._apply_edits_audited(
                        session_id,
                        owner="codex",
                        task_id=ready_task.id,
                        stage="executing",
                        ws=ws,
                        edits=res.reply.files,
                        allowed=ready_task.owned_paths,
                    )
                    check_out = ""
                    if ready_task.check_argv and cfg.modules.sandbox_checks_enabled:
                        chk = run_check(ready_task.check_argv, ws, 30)
                        check_out = redact((chk.stdout + "\n" + chk.stderr).strip())[:4000]
                        passed = not chk.issue and chk.code == 0
                        self.record_event(
                            session_id, category="workspace", event_type="sandbox.check",
                            actor="aisys", stage="executing",
                            summary=f"沙箱验收 {ready_task.id}：{'通过' if passed else '未通过'} (exit={chk.code})",
                            detail={"task_id": ready_task.id, "exit_code": chk.code, "issue": chk.issue},
                        )
                        if not passed:
                            raise ValueError(f"沙箱检查失败：{check_out}")
                    ex.status = "completed"
                    ex.summary = redact(res.reply.summary)[:8000]
                    ex.artifacts = sorted(set(ex.artifacts) | set(written))
                    ex.check_output = check_out
                    ex.error = ""
                    s.error = ""
                    self.record_event(
                        session_id, category="codex", event_type="codex.step.completed",
                        actor="codex", stage="executing",
                        summary=f"Codex 完成子任务 {ready_task.id}（{res.duration}s，写入 {len(written)} 文件）",
                        detail={"task_id": ready_task.id, "files": written, "duration": res.duration, "usage": res.usage},
                    )
                    self._maybe_advance_after_execution(s, ws)
                except (OSError, ValueError) as err:
                    ex.status = "failed"
                    ex.error = redact(str(err))[:500]
                    s.error = ex.error
                    self.record_event(
                        session_id, category="codex", event_type="codex.step.failed",
                        actor="codex", stage="executing",
                        summary=f"Codex 子任务 {ready_task.id} 落盘被阻断：{ex.error}",
                        detail={"task_id": ready_task.id, "error": ex.error},
                    )
                self._save(s)
            return True

        # Stage 5: Codex independently reviews all Gemini-owned subtasks
        if s.stage == "reviewing" and s.locked_plan and "codex" not in s.reviews:
            gemini_tasks = [st for st in s.locked_plan.subtasks if st.owner == "gemini"]
            if not gemini_tasks:
                return False
            prompt = self._mutual_review_prompt(s, ws, "codex", gemini_tasks)
            res, data = _call_run_json(
                prompt=prompt, schema=MUTUAL_REVIEW_SCHEMA, timeout=codex_timeout,
                stage="reviewing", step_label=f"阶段 5 第 {s.review_round} 轮交叉审查 Gemini",
            )
            with self._exclusive(session_id):
                s = self._load(session_id)
                if s.stage != "reviewing" or "codex" in s.reviews:
                    return False
                if res.status != "ok" or not data:
                    s.error = redact(res.error or f"Codex mutual review failed ({res.status})")
                    self._save(s)
                    self.record_event(
                        session_id, category="codex", event_type="codex.step.failed",
                        actor="codex", stage="reviewing",
                        summary=f"Codex 交叉审查失败：{s.error}",
                        detail={"status": res.status, "duration": res.duration},
                    )
                    return False
                gemini_ids = {st.id for st in gemini_tasks}
                fixes = [
                    FixRequest(task_id=f["task_id"], feedback=redact(f["feedback"]))
                    for f in data.get("fix_requests", [])
                    if f.get("task_id") in gemini_ids and f.get("feedback")
                ]
                review = MutualReview(
                    reviewer="codex",
                    target_owner="gemini",
                    round=s.review_round,
                    verdict=data["verdict"],
                    summary=redact(data["summary"]),
                    findings=[redact(x) for x in data.get("findings", [])[:20]],
                    fix_requests=fixes,
                    duration=res.duration,
                    usage=res.usage,
                )
                s.reviews["codex"] = review
                s.review_history.append(review)
                s.error = ""
                self.record_event(
                    session_id, category="codex", event_type="codex.step.completed",
                    actor="codex", stage="reviewing",
                    summary=f"Codex 完成第 {review.round} 轮交叉审查（审查 Gemini → {review.verdict.upper()}，{len(fixes)} 条修复要求）",
                    detail={"round": review.round, "verdict": review.verdict, "fix_count": len(fixes), "usage": res.usage},
                )
                self._maybe_finalize_review_round(s, ws)
                self._save(s)
            return True

        return False

    # ---------- Deliverables (Diff & Study Guide) ----------
    def get_diff(self, session_id: str) -> dict:
        ws = self._workspace(session_id)
        changed = artifacts(ws)
        tracked_diff = git(ws, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--")
        untracked_raw = git(ws, "ls-files", "-z", "--others", "--exclude-standard")
        untracked = [
            name for name in untracked_raw.split("\0")
            if name and not name.startswith(".aisys-check/")
        ]
        parts = [tracked_diff] if tracked_diff else []
        for rel in sorted(untracked):
            try:
                content = read_context(ws, rel, 32_000)
            except ValueError:
                continue
            added = "\n".join(f"+{line}" for line in content.splitlines())
            parts.append(f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n{added}")
        return {
            "session_id": session_id,
            "workspace": str(ws),
            "changed_paths": changed,
            "diff": redact("\n\n".join(parts)),
        }

    def get_study_guide(self, session_id: str) -> dict:
        s = self._load(session_id)
        ws = self._workspace(session_id)
        if s.study_guide_path and Path(s.study_guide_path).is_file():
            content = Path(s.study_guide_path).read_text(encoding="utf-8")
        else:
            content = _compile_study_guide(
                s, ws, header_note=self.get_pipeline_config().shared.study_guide_header,
            )
        return {
            "session_id": s.id,
            "path": s.study_guide_path or str(self.sessions_dir / s.id / "study-guide.md"),
            "markdown": redact(content),
        }

    def request_shutdown(self, *, confirm_active: bool = False) -> dict:
        active = self.active_sessions_count()
        if active > 0 and not confirm_active:
            raise ConflictError(f"当前仍有 {active} 个进行中的模型任务，请确认后再停止服务。")
        self.publish("server_shutdown", {"active": active})
        cb = self._shutdown_callback
        if cb is not None:
            threading.Timer(0.25, cb).start()
        return {"shutting_down": True, "active_sessions": active}
