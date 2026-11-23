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


def _compile_study_guide(session: CollabSession, workspace: Path) -> str:
    """Compile completed non-overlapping Markdown sections + cross-review notes into study-guide.md."""
    lines = [
        f"# {session.task.splitlines()[0][:80]}",
        "",
        "> **模式**：双模型协作学习文档（Gemini + Codex）  ",
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

        self._lock = threading.Lock()
        self._active_codex_sessions: set[str] = set()
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sub_lock = threading.Lock()
        self._stop_watcher = threading.Event()
        self._watcher_thread: threading.Thread | None = None
        self._fingerprints: dict[str, str] = {}
        self._shutdown_callback = None

    def _codex(self) -> CodexBackend:
        if self.backends is not None:
            return self.backends["codex"]
        return CodexBackend()

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
                    fp = f"{item['stage']}:{item['updated_at']}:{item['codex_busy']}"
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
        )
        self._save(session)
        self.publish("sessions_changed", {"ids": [sid]})
        if auto_start_codex:
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
        data = s.model_dump(mode="json")
        data["codex_busy"] = self.is_codex_busy(session_id)
        data["workspace"] = str(ws)
        data["artifacts"] = artifacts(ws)
        data["next_action"] = self.next_action(session_id)
        return data

    def next_action(self, session_id: str) -> dict:
        """Return exact pending actions for Gemini (Antigravity), Codex, or User."""
        s = self._load(session_id)
        ws = self._workspace(session_id)
        codex_ready = False
        pending_gemini: list[dict] = []
        awaiting_user = False

        if s.stage == "proposing":
            codex_ready = "codex" not in s.proposals
            if "gemini" not in s.proposals:
                pending_gemini.append({
                    "action": "propose",
                    "stage": "proposing",
                    "prompt": self._proposal_prompt(s, ws, "gemini"),
                    "schema": PROPOSAL_SCHEMA,
                })
        elif s.stage == "critiquing":
            codex_ready = "codex" not in s.refined
            if "gemini" not in s.refined:
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
                    codex_ready = True
                else:
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
            if gemini_owned and "codex" not in s.reviews:
                codex_ready = True
            if codex_owned and "gemini" not in s.reviews:
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

    # ---------- Prompt Builders ----------
    def _proposal_prompt(self, s: CollabSession, ws: Path, model: Owner) -> str:
        mode_note = (
            "开发任务模式：请提出清晰的架构设计、关键边界与验证方案。"
            if s.mode == "dev"
            else "学习与研究模式：请设计循序渐进的知识框架、核心概念拆解与实操练习方向。"
        )
        return (
            f"你是双模型协作中的 {model.upper()}，当前处于【阶段 1：独立提出初案】。\n"
            f"请独立思考并提出你的最佳预案（不要假设看到另一模型的回答）。\n"
            f"{mode_note}\n"
            f"任务/学习目标：\n{s.task}\n\n"
            f"当前工作区概览：\n{_repo_overview(ws)}\n\n"
            "请严格返回 JSON，包含 summary（简明摘要）、approach（完整方案 Markdown）、"
            "strengths（核心优势列表）、risks（潜在风险或难点列表）。"
        )

    def _critique_prompt(
        self, s: CollabSession, ws: Path, model: Owner, own: Proposal, peer: Proposal
    ) -> str:
        peer_name = "codex" if model == "gemini" else "gemini"
        path_rule = (
            "每个子任务的 owned_paths 必须是精确的相对文件路径（如 solution.py、tests/test_solution.py）。"
            if s.mode == "dev"
            else "每个子任务的 owned_paths 必须是互斥的 Markdown 章节文件（如 sections/01_concepts.md、sections/02_examples.md）。"
        )
        return (
            f"你是双模型协作中的 {model.upper()}，当前处于【阶段 2：交叉互审 + 升级方案 + 最优分工】。\n"
            f"任务/学习目标：\n{s.task}\n\n"
            f"你的初版方案：\n{own.model_dump_json(indent=2)}\n\n"
            f"对方（{peer_name.upper()}）的初版方案：\n{peer.model_dump_json(indent=2)}\n\n"
            "请完成以下工作：\n"
            "1. 客观指出对方初案的不足或遗漏（critique_of_peer），以及你从对方吸收的优点（adopted_from_peer）。\n"
            "2. 结合双方优点输出你的升级版完整方案（summary, approach）。\n"
            "3. 设计一份【严格无重叠】的双模型最优任务分工表 subtasks[]：\n"
            "   - 将最适合 Gemini（架构规划、概念解释、文档与审查）和最适合 Codex（编码实现、脚本示例、边界测试）的工作合理分配。\n"
            f"   - {path_rule}\n"
            "   - **硬性约束**：不同 owner（gemini vs codex）的子任务绝不能声明相同的 owned_paths，否则系统将判定冲突拒绝；有先后关系的任务通过 depends_on 声明，不得形成循环。\n"
            "   - 控制在 2–6 个高内聚子任务，确保简洁清晰。"
        )

    def _merge_prompt(self, s: CollabSession) -> str:
        return (
            "当前处于【阶段 3：根据用户融合微调意见生成最终锁定分工】。\n"
            f"任务/学习目标：\n{s.task}\n\n"
            f"用户选择：{s.decision.choice if s.decision else 'merge'}，"
            f"基础方案：{s.decision.base_model if s.decision else 'gemini'}\n"
            f"用户融合微调指令：\n{s.decision.fusion_notes if s.decision else ''}\n\n"
            f"Gemini 升级方案：\n{s.refined['gemini'].model_dump_json(indent=2) if 'gemini' in s.refined else '{}'}\n\n"
            f"Codex 升级方案：\n{s.refined['codex'].model_dump_json(indent=2) if 'codex' in s.refined else '{}'}\n\n"
            "请严格按照用户微调意见融合双方优点，输出最终 summary、approach 与严格无重叠的 subtasks[]（确保不同 owner 的 owned_paths 完全互斥、依赖无环）。"
        )

    def _subtask_prompt(self, s: CollabSession, ws: Path, st: SubTask, feedback: str) -> str:
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
        return (
            f"你是 {st.owner.upper()}，当前处于【阶段 4：分工协同执行】。\n"
            f"总任务：\n{s.task}\n\n"
            f"总体锁定方案：\n{s.locked_plan.summary if s.locked_plan else ''}\n\n"
            f"你负责的专属子任务：{st.id} · {st.title}\n"
            f"任务要求：{st.objective}\n"
            f"验收标准：{st.acceptance}\n"
            f"【硬性权限锁】你只允许写入以下独占路径（不得修改其他任务的文件）：{', '.join(st.owned_paths)}\n"
            f"{fb_block}\n"
            f"前置依赖产出：\n" + ("\n".join(dep_blocks) or "(无)") + "\n\n"
            "你独占路径的当前内容：\n" + "\n".join(existing_blocks) + "\n\n"
            "请返回完整 UTF-8 文件内容（files 数组，每个 path 必须属于你的 owned_paths）、简明 summary，decision 设为 none。"
        )

    def _mutual_review_prompt(self, s: CollabSession, ws: Path, reviewer: Owner, targets: list[SubTask]) -> str:
        target_owner = "codex" if reviewer == "gemini" else "gemini"
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
        return (
            f"你是 {reviewer.upper()}，当前处于【阶段 5：双模型互相审查监督（第 {s.review_round} 轮）】。\n"
            f"请独立严格审查对方（{target_owner.upper()}）完成的所有子任务产出，检查正确性、完整性、边界条件与与总方案的一致性。\n"
            f"总任务：\n{s.task}\n\n"
            + "\n".join(blocks) + "\n\n"
            "如果全部达标，返回 verdict='pass' 且 fix_requests=[]；"
            "如果发现实质缺陷或遗漏，返回 verdict='fail' 并在 fix_requests 中逐条指明 task_id 和具体可执行的 feedback。"
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
            if "codex" in s.proposals:
                s.stage = "critiquing"
            self._save(s)
        self.publish("sessions_changed", {"ids": [session_id]})
        if auto_codex:
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
            if "codex" in s.refined:
                s.stage = "deciding"
            self._save(s)
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
            written = apply_edits(ws, edits, st.owned_paths)
            check_out = ""
            if st.check_argv:
                chk = run_check(st.check_argv, ws, 30)
                check_out = redact((chk.stdout + "\n" + chk.stderr).strip())[:4000]
                if chk.issue or chk.code != 0:
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
            s.stage = "cancelled"
            s.error = "已由用户取消"
            self._save(s)
        self.publish("sessions_changed", {"ids": [session_id]})
        return self.get_session_view(session_id)

    def _maybe_advance_after_execution(self, s: CollabSession, ws: Path) -> None:
        if not s.locked_plan:
            return
        if all(ex.status == "completed" for ex in s.executions.values()):
            s.stage = "reviewing"
            s.reviews = {}
            # Auto-pass side if a model had 0 assigned subtasks to be audited
            if not any(st.owner == "codex" for st in s.locked_plan.subtasks):
                s.reviews["gemini"] = MutualReview(
                    reviewer="gemini", target_owner="codex", round=s.review_round,
                    verdict="pass", summary="无 Codex 子任务需审查（自动跳过）。",
                )
            if not any(st.owner == "gemini" for st in s.locked_plan.subtasks):
                s.reviews["codex"] = MutualReview(
                    reviewer="codex", target_owner="gemini", round=s.review_round,
                    verdict="pass", summary="无 Gemini 子任务需审查（自动跳过）。",
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
            return

        # Complete session & compile Structured Markdown Study Guide if learn mode!
        if s.mode == "learn":
            guide = _compile_study_guide(s, ws)
            guide_out = self.sessions_dir / s.id / "study-guide.md"
            write_atomic(guide_out, guide.encode("utf-8"), mode=0o600)
            write_atomic(ws / "STUDY_GUIDE.md", guide.encode("utf-8"), mode=0o644)
            s.study_guide_path = str(guide_out)
        s.final_summary = (
            f"双模型协同与交叉审查已完成（共 {s.review_round} 轮审查，"
            f"Gemini 审查：{g_rev.verdict}，Codex 审查：{c_rev.verdict}）。"
        )
        s.stage = "completed"

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
        s = self._load(session_id)
        ws = self._workspace(session_id)
        broker = self.sessions_dir / session_id / "provider" / "codex"
        broker.mkdir(mode=0o700, exist_ok=True)
        codex = self._codex()

        # Stage 1: Codex independent proposal
        if s.stage == "proposing" and "codex" not in s.proposals:
            prompt = self._proposal_prompt(s, ws, "codex")
            res, data = codex.run_json(prompt=prompt, schema=PROPOSAL_SCHEMA, cwd=broker, timeout=180)
            with self._exclusive(session_id):
                s = self._load(session_id)
                if s.stage != "proposing":
                    return False
                if res.status != "ok" or not data:
                    s.error = redact(res.error or f"Codex Stage 1 failed ({res.status})")
                    self._save(s)
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
                if "gemini" in s.proposals:
                    s.stage = "critiquing"
                self._save(s)
            return True

        # Stage 2: Codex cross-critique + upgraded plan + division
        if s.stage == "critiquing" and "codex" not in s.refined and "gemini" in s.proposals and "codex" in s.proposals:
            prompt = self._critique_prompt(s, ws, "codex", s.proposals["codex"], s.proposals["gemini"])
            res, data = codex.run_json(prompt=prompt, schema=REFINED_SCHEMA, cwd=broker, timeout=240)
            with self._exclusive(session_id):
                s = self._load(session_id)
                if s.stage != "critiquing":
                    return False
                if res.status != "ok" or not data:
                    s.error = redact(res.error or f"Codex Stage 2 failed ({res.status})")
                    self._save(s)
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
                if "gemini" in s.refined:
                    s.stage = "deciding"
                self._save(s)
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
            self.publish("sessions_changed", {"ids": [session_id]})

            prompt = self._subtask_prompt(s, ws, ready_task, s.executions[ready_task.id].review_feedback)
            agent = Agent(
                role=f"Codex Executor ({ready_task.id})",
                backend="codex",
                instructions=ready_task.objective,
                write_paths=list(ready_task.owned_paths),
            )
            res = codex.run(prompt=prompt, agent=agent, cwd=broker, timeout=240)
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
                    return False
                try:
                    written = apply_edits(ws, res.reply.files, ready_task.owned_paths)
                    check_out = ""
                    if ready_task.check_argv:
                        chk = run_check(ready_task.check_argv, ws, 30)
                        check_out = redact((chk.stdout + "\n" + chk.stderr).strip())[:4000]
                        if chk.issue or chk.code != 0:
                            raise ValueError(f"沙箱检查失败：{check_out}")
                    ex.status = "completed"
                    ex.summary = redact(res.reply.summary)[:8000]
                    ex.artifacts = sorted(set(ex.artifacts) | set(written))
                    ex.check_output = check_out
                    ex.error = ""
                    s.error = ""
                    self._maybe_advance_after_execution(s, ws)
                except (OSError, ValueError) as err:
                    ex.status = "failed"
                    ex.error = redact(str(err))[:500]
                    s.error = ex.error
                self._save(s)
            return True

        # Stage 5: Codex independently reviews all Gemini-owned subtasks
        if s.stage == "reviewing" and s.locked_plan and "codex" not in s.reviews:
            gemini_tasks = [st for st in s.locked_plan.subtasks if st.owner == "gemini"]
            if not gemini_tasks:
                return False
            prompt = self._mutual_review_prompt(s, ws, "codex", gemini_tasks)
            res, data = codex.run_json(prompt=prompt, schema=MUTUAL_REVIEW_SCHEMA, cwd=broker, timeout=180)
            with self._exclusive(session_id):
                s = self._load(session_id)
                if s.stage != "reviewing" or "codex" in s.reviews:
                    return False
                if res.status != "ok" or not data:
                    s.error = redact(res.error or f"Codex mutual review failed ({res.status})")
                    self._save(s)
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
            content = _compile_study_guide(s, ws)
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
