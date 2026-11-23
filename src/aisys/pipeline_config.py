"""Editable backend pipeline configuration, profiles, and real module controller."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from .backends import Backend, CodexBackend, codex_executable
from .config import Model, UniqueLoader
from .process import cli_env, redact
from .workspace import write_atomic

Owner = Literal["gemini", "codex"]

DEFAULT_SHARED_DIRECTIVES = (
    "始终以简洁、准确、可验证为第一原则；明确标注假设与边界条件，不编造未实测的数据或结果。"
)
DEFAULT_DIVISION_POLICY = (
    "将架构规划、原理解析、文档梳理优先分配给 Gemini；将代码实现、可运行案例、边界测试优先分配给 Codex。"
    "不同 owner 的子任务绝不允许共享 owned_paths；有前后依赖时通过 depends_on 声明且禁止成环。"
)
DEFAULT_STUDY_GUIDE_HEADER = (
    "本学习指南由 Gemini (Antigravity) 与 Codex 双模型独立提案、交叉互审、互斥协作并互相质检生成。"
)

DEFAULT_GEMINI_PERSONA = (
    "你是双模型共创中的 Gemini（运行于 Antigravity），擅长系统性架构拆解、第一性原理推演、清晰结构化表达与高屋建瓴的审查。"
)
DEFAULT_CODEX_PERSONA = (
    "你是双模型共创中的 Codex（运行于隔离沙箱 CLI），擅长严谨工程实现、可运行代码示例、极端边界验证与细粒度代码/技术审计。"
)

DEFAULT_PROPOSAL_PROMPT = (
    "当前处于【阶段 1：独立提出初案】。\n"
    "请独立思考并提出你的最佳预案（不要假设看到另一模型的回答）。\n"
    "{mode_note}\n"
    "任务/学习目标：\n{task}\n\n"
    "当前工作区概览：\n{repo_overview}\n\n"
    "请严格返回 JSON，包含 summary（简明摘要）、approach（完整方案 Markdown）、"
    "strengths（核心优势列表）、risks（潜在风险或难点列表）。"
)

DEFAULT_CRITIQUE_PROMPT = (
    "当前处于【阶段 2：交叉互审 + 升级方案 + 最优分工】。\n"
    "任务/学习目标：\n{task}\n\n"
    "你的初版方案：\n{own_json}\n\n"
    "对方（{peer_name}）的初版方案：\n{peer_json}\n\n"
    "请完成以下工作：\n"
    "1. 客观指出对方初案的不足或遗漏（critique_of_peer），以及你从对方吸收的优点（adopted_from_peer）。\n"
    "2. 结合双方优点输出你的升级版完整方案（summary, approach）。\n"
    "3. 设计一份【严格无重叠】的双模型最优任务分工表 subtasks[]（1–{max_subtasks} 项）：\n"
    "   - {division_policy}\n"
    "   - {path_rule}\n"
    "   - 硬性约束：不同 owner 绝不能声明相同的 owned_paths，depends_on 不得形成循环。"
)

DEFAULT_MERGE_PROMPT = (
    "当前处于【阶段 3：根据用户融合微调意见生成最终锁定分工】。\n"
    "任务/学习目标：\n{task}\n\n"
    "用户选择：{choice}，基础方案：{base_model}\n"
    "用户融合微调指令：\n{fusion_notes}\n\n"
    "Gemini 升级方案：\n{gemini_refined}\n\n"
    "Codex 升级方案：\n{codex_refined}\n\n"
    "分工原则：{division_policy}\n"
    "请严格按用户意见融合双方优点，输出最终 summary、approach 与严格无重叠的 subtasks[]（最多 {max_subtasks} 项）。"
)

DEFAULT_SUBTASK_PROMPT = (
    "当前处于【阶段 4：分工协同执行】。\n"
    "总任务：\n{task}\n\n"
    "总体锁定方案：\n{locked_summary}\n\n"
    "你负责的专属子任务：{task_id} · {task_title}\n"
    "任务要求：{objective}\n"
    "验收标准：{acceptance}\n"
    "【硬性权限锁】你只允许写入以下独占路径（不得修改其他任务的文件）：{owned_paths}\n"
    "{feedback_block}\n"
    "前置依赖产出：\n{dep_blocks}\n\n"
    "你独占路径的当前内容：\n{existing_blocks}\n\n"
    "请返回完整 UTF-8 文件内容（files 数组，每个 path 必须属于你的 owned_paths）、简明 summary，decision 设为 none。"
)

DEFAULT_REVIEW_PROMPT = (
    "当前处于【阶段 5：双模型互相审查监督（第 {review_round} 轮）】。\n"
    "请独立严格审查对方（{target_owner}）完成的所有子任务产出，检查正确性、完整性、边界条件与总方案一致性。\n"
    "总任务：\n{task}\n\n"
    "{target_blocks}\n\n"
    "如果全部达标，返回 verdict='pass' 且 fix_requests=[]；"
    "如发现实质缺陷或遗漏，返回 verdict='fail' 并在 fix_requests 中逐条指明 task_id 和具体可执行 feedback。"
)


def safe_format(template: str, variables: dict[str, object]) -> str:
    """Substitute known `{key}` tokens while preserving unknown braces safely."""
    out = template
    for key, val in variables.items():
        out = out.replace("{" + key + "}", str(val))
    return out


class ModuleSwitches(Model):
    gemini_enabled: bool = True
    codex_enabled: bool = True
    mcp_enabled: bool = True
    sandbox_checks_enabled: bool = True

    @model_validator(mode="after")
    def _at_least_one_model(self):
        if not self.gemini_enabled and not self.codex_enabled:
            raise ValueError("Gemini 与 Codex 至少需要启用一个模型，否则无法推进协作。")
        return self


class StageControls(Model):
    enable_critique: bool = True
    require_user_decision: bool = True
    default_winner: Owner = "gemini"
    enable_mutual_review: bool = True
    max_review_rounds: int = Field(default=2, ge=0, le=4)
    max_subtasks: int = Field(default=6, ge=1, le=12)
    auto_start_codex: bool = True


class SharedDirectives(Model):
    profile_name: str = Field(default="balanced", min_length=1, max_length=40)
    global_directives: str = Field(default=DEFAULT_SHARED_DIRECTIVES, max_length=4000)
    division_policy: str = Field(default=DEFAULT_DIVISION_POLICY, max_length=4000)
    study_guide_header: str = Field(default=DEFAULT_STUDY_GUIDE_HEADER, max_length=2000)


class GeminiConfig(Model):
    role_title: str = Field(default="架构规划、原理剖析与审查专家", max_length=120)
    trigger_command: str = Field(default="/aisys", min_length=1, max_length=80)
    system_persona: str = Field(default=DEFAULT_GEMINI_PERSONA, max_length=4000)
    proposal_prompt: str = Field(default=DEFAULT_PROPOSAL_PROMPT, max_length=10_000)
    critique_prompt: str = Field(default=DEFAULT_CRITIQUE_PROMPT, max_length=10_000)
    merge_prompt: str = Field(default=DEFAULT_MERGE_PROMPT, max_length=10_000)
    subtask_prompt: str = Field(default=DEFAULT_SUBTASK_PROMPT, max_length=10_000)
    review_prompt: str = Field(default=DEFAULT_REVIEW_PROMPT, max_length=10_000)


class CodexConfig(Model):
    role_title: str = Field(default="工程实现、可运行验证与边界审计专家", max_length=120)
    model: str = Field(default="", max_length=80)
    timeout_s: int = Field(default=180, ge=30, le=600)
    system_persona: str = Field(default=DEFAULT_CODEX_PERSONA, max_length=4000)
    proposal_prompt: str = Field(default=DEFAULT_PROPOSAL_PROMPT, max_length=10_000)
    critique_prompt: str = Field(default=DEFAULT_CRITIQUE_PROMPT, max_length=10_000)
    subtask_prompt: str = Field(default=DEFAULT_SUBTASK_PROMPT, max_length=10_000)
    review_prompt: str = Field(default=DEFAULT_REVIEW_PROMPT, max_length=10_000)


class PipelineConfig(Model):
    version: Literal[2] = 2
    modules: ModuleSwitches = Field(default_factory=ModuleSwitches)
    stages: StageControls = Field(default_factory=StageControls)
    shared: SharedDirectives = Field(default_factory=SharedDirectives)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)


def build_builtin_profiles() -> dict[str, dict]:
    balanced = PipelineConfig()

    deep_learn = PipelineConfig()
    deep_learn.shared.profile_name = "deep_learn"
    deep_learn.shared.global_directives = (
        "深度研学模式：优先讲透底层心智模型与反直觉陷阱，所有关键结论配可运行最小示例、边界对比表和自测练习题。"
    )
    deep_learn.stages.enable_critique = True
    deep_learn.stages.require_user_decision = True
    deep_learn.stages.enable_mutual_review = True
    deep_learn.stages.max_review_rounds = 2
    deep_learn.stages.max_subtasks = 6

    strict_audit = PipelineConfig()
    strict_audit.shared.profile_name = "strict_audit"
    strict_audit.shared.global_directives = (
        "严苛工程审计模式：优先关注并发安全、权限边界、错误恢复与可复现测试；发现任何未经验证的假设必须打回修复。"
    )
    strict_audit.stages.enable_critique = True
    strict_audit.stages.require_user_decision = True
    strict_audit.stages.enable_mutual_review = True
    strict_audit.stages.max_review_rounds = 3
    strict_audit.codex.timeout_s = 240

    fast_ship = PipelineConfig()
    fast_ship.shared.profile_name = "fast_ship"
    fast_ship.shared.global_directives = (
        "极速直出模式：省略冗长铺垫，直接输出可执行核心方案与精炼产物。"
    )
    fast_ship.stages.enable_critique = False
    fast_ship.stages.require_user_decision = False
    fast_ship.stages.default_winner = "codex"
    fast_ship.stages.enable_mutual_review = True
    fast_ship.stages.max_review_rounds = 1
    fast_ship.stages.max_subtasks = 4

    return {
        "balanced": {
            "id": "balanced",
            "label": "标准均衡双模型",
            "description": "完整 5 阶段：独立提案 → 交叉互审 → 人工拍板/融合 → 互斥协同 → 2 轮互相监督。",
            "config": balanced.model_dump(mode="json"),
        },
        "deep_learn": {
            "id": "deep_learn",
            "label": "深度学术研学",
            "description": "强化第一性原理、对比案例与可运行练习，双模型交叉勘误并汇编结构化 Markdown 指南。",
            "config": deep_learn.model_dump(mode="json"),
        },
        "strict_audit": {
            "id": "strict_audit",
            "label": "严苛工程审计",
            "description": "面向关键代码与架构设计，放宽 Codex 超时至 240s，最高 3 轮交叉审查返工。",
            "config": strict_audit.model_dump(mode="json"),
        },
        "fast_ship": {
            "id": "fast_ship",
            "label": "极速直出流水线",
            "description": "跳过阶段 2 互审与阶段 3 人工停顿，自动锁定分工直奔协同执行与 1 轮快速审查。",
            "config": fast_ship.model_dump(mode="json"),
        },
    }


BUILTIN_PROFILES = build_builtin_profiles()


class PipelineConfigStore:
    """Persisted YAML configuration for editable backend pipeline & module switches."""

    def __init__(self, data: Path):
        self.data = data.resolve()
        self.path = self.data / "config" / "pipeline.yaml"

    def load(self) -> PipelineConfig:
        if self.path.is_file() and not self.path.is_symlink():
            try:
                raw = yaml.load(self.path.read_text(encoding="utf-8"), Loader=UniqueLoader) or {}
                return PipelineConfig.model_validate(raw)
            except (OSError, ValueError):
                return PipelineConfig()
        return PipelineConfig()

    def save(self, cfg: PipelineConfig | dict) -> PipelineConfig:
        validated = cfg if isinstance(cfg, PipelineConfig) else PipelineConfig.model_validate(cfg)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = yaml.safe_dump(validated.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        write_atomic(self.path, text.encode("utf-8"), mode=0o600)
        return validated

    def apply_profile(self, profile_id: str) -> PipelineConfig:
        if profile_id not in BUILTIN_PROFILES:
            raise ValueError(f"未知配置预设：{profile_id}")
        return self.save(BUILTIN_PROFILES[profile_id]["config"])

    def reset(self) -> PipelineConfig:
        return self.save(PipelineConfig())


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return 0
    for root, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _antigravity_app_path() -> Path | None:
    for candidate in (Path("/Applications/Antigravity.app"), Path("/Applications/Gemini.app")):
        if candidate.exists():
            return candidate
    return None


def _is_antigravity_running() -> bool:
    if sys.platform != "darwin":
        return False
    for proc_name in ("Antigravity", "Gemini"):
        try:
            proc = subprocess.run(
                ["pgrep", "-x", proc_name],
                capture_output=True, text=True, timeout=2,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


class ModuleController:
    """Real inspection, toggle, health probing, and 1-click launch for all 6 local modules."""

    def __init__(
        self,
        *,
        data: Path,
        project_root: Path,
        pipeline_store: PipelineConfigStore,
        backends: dict[str, Backend] | None = None,
    ):
        self.data = data.resolve()
        self.project_root = project_root.resolve()
        self.pipeline_store = pipeline_store
        self.backends = backends
        self._probe_cache: dict[str, dict] = {}

    def _codex_backend(self) -> Backend:
        if self.backends is not None:
            return self.backends["codex"]
        return CodexBackend()

    def _plugin_source_dir(self) -> Path:
        return self.project_root / ".agents" / "plugins" / "aisys-bridge"

    def _global_plugin_link(self) -> Path:
        return Path.home() / ".gemini" / "config" / "plugins" / "aisys-bridge"

    def inspect_all(
        self,
        *,
        server_meta: dict,
        stats: dict,
        test_mode: bool = False,
    ) -> dict:
        cfg = self.pipeline_store.load()
        codex_probe = self._probe_cache.get("codex_engine")
        mcp_probe = self._probe_cache.get("mcp_bridge")
        sandbox_probe = self._probe_cache.get("sandbox_guard")

        # 1. Web Kernel
        web_mod = {
            "id": "web_kernel",
            "name": "AIsys 本地服务内核",
            "lane": "shared",
            "role": "FastAPI 仅本机回环控制面 · SSE 实时推送 · 状态机引擎",
            "enabled": True,
            "status": "running",
            "latency_ms": 1,
            "can_toggle": False,
            "actions": ["probe"],
            "metrics": {
                "pid": os.getpid(),
                "host": server_meta.get("host", "127.0.0.1"),
                "port": server_meta.get("port", 8765),
                "uptime_s": server_meta.get("uptime_s", 0),
                "sse_clients": server_meta.get("sse_clients", 0),
                "active_codex": server_meta.get("active_codex", 0),
            },
            "detail": f"监听 http://{server_meta.get('host', '127.0.0.1')}:{server_meta.get('port', 8765)} · PID {os.getpid()}",
            "last_checked_at": round(time.time(), 1),
        }

        # 2. Codex CLI Engine
        codex_exe = codex_executable()
        if not cfg.modules.codex_enabled:
            c_status = "stopped"
            c_detail = "已手动关闭开关（当前为 Gemini 单模型或暂停模式）"
        elif server_meta.get("active_codex", 0) > 0:
            c_status = "running"
            c_detail = "正在隔离沙箱中执行后台模型任务"
        elif codex_probe:
            c_status = codex_probe["status"]
            c_detail = codex_probe["detail"]
        elif self.backends is not None:
            h = self._codex_backend().health()
            c_status = "ready" if h.status == "ok" else "degraded"
            c_detail = h.error or "Codex 后端已就绪"
        elif codex_exe:
            c_status = "ready"
            c_detail = f"已检测到 CLI：{Path(codex_exe).name}（点击「启动/实测」验证登录与响应）"
        else:
            c_status = "unavailable"
            c_detail = "未在 PATH 或 ChatGPT.app 中发现 codex CLI"

        codex_mod = {
            "id": "codex_engine",
            "name": "Codex CLI 沙箱引擎",
            "lane": "codex",
            "role": cfg.codex.role_title,
            "enabled": cfg.modules.codex_enabled,
            "status": c_status,
            "latency_ms": (codex_probe or {}).get("latency_ms"),
            "can_toggle": True,
            "actions": ["start", "probe", "stop"],
            "metrics": {
                "model": cfg.codex.model or "ChatGPT 默认",
                "timeout_s": cfg.codex.timeout_s,
                "auto_start": cfg.stages.auto_start_codex,
                "executable": Path(codex_exe).name if codex_exe else "未安装",
                "version": (codex_probe or {}).get("version", ""),
            },
            "detail": c_detail,
            "last_checked_at": (codex_probe or {}).get("ts"),
        }

        # 3. Antigravity / Gemini Bridge
        app_path = _antigravity_app_path()
        app_running = False if test_mode else _is_antigravity_running()
        src_plugin = self._plugin_source_dir()
        global_link = self._global_plugin_link()
        plugin_linked = global_link.exists() or src_plugin.is_dir()
        skill_ok = (src_plugin / "skills" / "aisys" / "SKILL.md").is_file()
        rule_ok = (src_plugin / "rules" / "AGENTS.md").is_file()

        if not cfg.modules.gemini_enabled:
            g_status = "stopped"
            g_detail = "已手动关闭开关（当前为 Codex 独立运行模式）"
        elif app_running and plugin_linked and skill_ok and rule_ok:
            g_status = "running"
            g_detail = f"{app_path.stem if app_path else 'Antigravity'} 运行中 · 全局 /aisys 桥接已就绪"
        elif plugin_linked and skill_ok and rule_ok:
            g_status = "ready"
            g_detail = f"全局插件已配置 · 点击「一键唤起」自动复制 {cfg.gemini.trigger_command} 并切至客户端"
        else:
            g_status = "degraded"
            g_detail = "全局插件配置不完整，点击「一键修复并启动」自动补齐"

        gemini_mod = {
            "id": "antigravity_bridge",
            "name": "Gemini · Antigravity 客户端桥",
            "lane": "gemini",
            "role": cfg.gemini.role_title,
            "enabled": cfg.modules.gemini_enabled,
            "status": g_status,
            "latency_ms": (self._probe_cache.get("antigravity_bridge") or {}).get("latency_ms"),
            "can_toggle": True,
            "actions": ["start", "repair", "stop"],
            "metrics": {
                "app": app_path.stem if app_path else "未检测",
                "app_running": app_running,
                "global_plugin": global_link.is_symlink() or global_link.is_dir(),
                "trigger": cfg.gemini.trigger_command,
                "skill_ready": skill_ok and rule_ok,
            },
            "detail": g_detail,
            "last_checked_at": round(time.time(), 1),
        }

        # 4. MCP Stdio Bridge
        if not cfg.modules.mcp_enabled:
            m_status = "stopped"
            m_detail = "MCP 协议桥开关已关闭"
        elif mcp_probe:
            m_status = mcp_probe["status"]
            m_detail = mcp_probe["detail"]
        else:
            run_sh = src_plugin / "scripts" / "run-mcp.sh"
            m_status = "ready" if run_sh.is_file() else "degraded"
            m_detail = "Stdio MCP 服务已注册（点击「实测握手」检验 16 个工具连通）"

        mcp_mod = {
            "id": "mcp_bridge",
            "name": "MCP Stdio 协议桥",
            "lane": "shared",
            "role": "为 Antigravity 暴露结构化 aisys_collab_* 与工作流工具",
            "enabled": cfg.modules.mcp_enabled,
            "status": m_status,
            "latency_ms": (mcp_probe or {}).get("latency_ms"),
            "can_toggle": True,
            "actions": ["start", "probe", "stop"],
            "metrics": {
                "tools_count": (mcp_probe or {}).get("tools_count", 16),
                "collab_tools": (mcp_probe or {}).get("collab_tools", 6),
                "transport": "stdio JSON-RPC 2.0",
            },
            "detail": m_detail,
            "last_checked_at": (mcp_probe or {}).get("ts"),
        }

        # 5. Anti-Overlap & Seatbelt Sandbox Guard
        has_seatbelt = Path("/usr/bin/sandbox-exec").is_file()
        has_git = bool(shutil.which("git"))
        if not cfg.modules.sandbox_checks_enabled:
            s_status = "degraded"
            s_detail = "命令沙箱校验已暂停（注意：owned_paths 硬文件写锁始终保持强制生效）"
        elif sandbox_probe:
            s_status = sandbox_probe["status"]
            s_detail = sandbox_probe["detail"]
        else:
            s_status = "running" if (has_seatbelt and has_git) else "degraded"
            s_detail = (
                f"防重叠锁 + macOS Seatbelt 就绪 · 累计放行 {stats.get('lock_allows', 0)} / 拦截 {stats.get('lock_denies', 0)}"
            )

        sandbox_mod = {
            "id": "sandbox_guard",
            "name": "防重叠锁与 Seatbelt 沙箱",
            "lane": "shared",
            "role": "机械互斥路径校验 · owned_paths 写锁 · 命令沙箱隔离",
            "enabled": cfg.modules.sandbox_checks_enabled,
            "status": s_status,
            "latency_ms": (sandbox_probe or {}).get("latency_ms"),
            "can_toggle": True,
            "actions": ["probe", "stop"],
            "metrics": {
                "seatbelt": has_seatbelt,
                "git": has_git,
                "lock_allows": stats.get("lock_allows", 0),
                "lock_denies": stats.get("lock_denies", 0),
                "max_subtasks": cfg.stages.max_subtasks,
            },
            "detail": s_detail,
            "last_checked_at": (sandbox_probe or {}).get("ts"),
        }

        # 6. Storage & Isolated Workspaces
        size_kb = round(_dir_size_bytes(self.data) / 1024, 1)
        storage_mod = {
            "id": "storage_workspace",
            "name": "持久化状态与隔离工作区",
            "lane": "shared",
            "role": "会话快照 · events.jsonl 审计流 · Markdown 学习文档仓库",
            "enabled": True,
            "status": "running",
            "latency_ms": 1,
            "can_toggle": False,
            "actions": ["probe", "open"],
            "metrics": {
                "sessions": stats.get("sessions", 0),
                "completed": stats.get("completed", 0),
                "study_guides": stats.get("study_guides", 0),
                "size_kb": size_kb,
            },
            "detail": f"共 {stats.get('sessions', 0)} 个会话 · {stats.get('study_guides', 0)} 份学习文档 · 占用 {size_kb} KB",
            "last_checked_at": round(time.time(), 1),
        }

        modules = [web_mod, gemini_mod, codex_mod, mcp_mod, sandbox_mod, storage_mod]
        return {
            "modules": modules,
            "summary": {
                "total": len(modules),
                "online": sum(1 for m in modules if m["status"] in {"running", "ready"}),
                "running": sum(1 for m in modules if m["status"] == "running"),
                "stopped": sum(1 for m in modules if m["status"] == "stopped"),
            },
        }

    def toggle_module(self, module_id: str, enabled: bool) -> PipelineConfig:
        cfg = self.pipeline_store.load()
        data = cfg.model_dump(mode="json")
        mapping = {
            "antigravity_bridge": "gemini_enabled",
            "codex_engine": "codex_enabled",
            "mcp_bridge": "mcp_enabled",
            "sandbox_guard": "sandbox_checks_enabled",
        }
        field = mapping.get(module_id)
        if not field:
            raise ValueError(f"模块 '{module_id}' 不支持独立开关")
        data["modules"][field] = bool(enabled)
        return self.pipeline_store.save(data)

    def probe_codex(self) -> dict:
        t0 = time.monotonic()
        backend = self._codex_backend()
        res = backend.health()
        version = ""
        exe = codex_executable()
        if exe and self.backends is None:
            try:
                vp = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=3)
                if vp.returncode == 0:
                    version = redact(vp.stdout.strip().splitlines()[0])[:64]
            except (OSError, subprocess.SubprocessError):
                pass
        latency = round((time.monotonic() - t0) * 1000, 1)
        ok = res.status == "ok"
        entry = {
            "status": "running" if ok else ("unavailable" if res.status == "unavailable" else "degraded"),
            "latency_ms": latency,
            "version": version,
            "detail": (
                f"ChatGPT 登录验证通过（{latency} ms）" + (f" · {version}" if version else "")
                if ok
                else redact(res.error or f"Codex 检测状态：{res.status}")
            ),
            "ts": round(time.time(), 1),
        }
        self._probe_cache["codex_engine"] = entry
        return entry

    def probe_mcp(self) -> dict:
        t0 = time.monotonic()
        run_sh = self._plugin_source_dir() / "scripts" / "run-mcp.sh"
        cmd = [str(run_sh)] if run_sh.is_file() else [sys.executable, "-m", "aisys", "--data", str(self.data), "mcp"]
        handshake = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"aisys-probe","version":"1"}}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
        try:
            proc = subprocess.run(
                cmd,
                input=handshake,
                capture_output=True,
                text=True,
                timeout=8,
                env=cli_env(),
            )
            tools: list[str] = []
            for line in proc.stdout.splitlines():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 2 and isinstance(msg.get("result"), dict):
                    tools = [t.get("name", "") for t in msg["result"].get("tools", []) if isinstance(t, dict)]
            latency = round((time.monotonic() - t0) * 1000, 1)
            collab_tools = [t for t in tools if "collab" in t]
            ok = proc.returncode == 0 and len(tools) >= 6
            entry = {
                "status": "running" if ok else "degraded",
                "latency_ms": latency,
                "tools_count": len(tools),
                "collab_tools": len(collab_tools),
                "tools": tools,
                "detail": (
                    f"Stdio MCP 握手成功（{latency} ms · {len(tools)} 工具 / {len(collab_tools)} 协作工具）"
                    if ok
                    else f"MCP 握手异常 (exit={proc.returncode})"
                ),
                "ts": round(time.time(), 1),
            }
        except (OSError, subprocess.SubprocessError) as err:
            entry = {
                "status": "degraded",
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "tools_count": 0,
                "collab_tools": 0,
                "detail": redact(f"MCP 探测失败：{err}")[:200],
                "ts": round(time.time(), 1),
            }
        self._probe_cache["mcp_bridge"] = entry
        return entry

    def probe_sandbox(self) -> dict:
        t0 = time.monotonic()
        has_seatbelt = Path("/usr/bin/sandbox-exec").is_file()
        git_ver = ""
        try:
            gp = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3)
            if gp.returncode == 0:
                git_ver = gp.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        latency = round((time.monotonic() - t0) * 1000, 1)
        ok = has_seatbelt and bool(git_ver)
        entry = {
            "status": "running" if ok else "degraded",
            "latency_ms": latency,
            "detail": (
                f"Seatbelt 沙箱 + {git_ver} + 防重叠写锁校验正常（{latency} ms）"
                if ok
                else "Seatbelt 或 Git 未就绪"
            ),
            "ts": round(time.time(), 1),
        }
        self._probe_cache["sandbox_guard"] = entry
        return entry

    def launch_antigravity(self, *, copy_trigger: bool = True, focus_app: bool = True, test_mode: bool = False) -> dict:
        t0 = time.monotonic()
        cfg = self.pipeline_store.load()
        trigger = cfg.gemini.trigger_command or "/aisys"
        repaired_link = False
        copied = False
        focused = False
        app_path = _antigravity_app_path()

        if not test_mode:
            # Ensure global Antigravity plugin symlink is intact
            src_plugin = self._plugin_source_dir()
            global_link = self._global_plugin_link()
            if src_plugin.is_dir() and not global_link.exists():
                try:
                    global_link.parent.mkdir(parents=True, exist_ok=True)
                    global_link.symlink_to(src_plugin)
                    repaired_link = True
                except OSError:
                    pass
            # Copy ultra-minimal trigger command straight into macOS pasteboard
            if copy_trigger and sys.platform == "darwin" and shutil.which("pbcopy"):
                try:
                    subprocess.run(["pbcopy"], input=trigger.encode("utf-8"), timeout=2, check=True)
                    copied = True
                except (OSError, subprocess.SubprocessError):
                    pass
            # Launch / bring Antigravity client to front
            if focus_app and sys.platform == "darwin" and app_path:
                try:
                    subprocess.Popen(
                        ["open", "-a", str(app_path)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    focused = True
                except OSError:
                    pass

        latency = round((time.monotonic() - t0) * 1000, 1)
        res = {
            "status": "running" if (focused or _is_antigravity_running() or test_mode) else "ready",
            "latency_ms": latency,
            "trigger": trigger,
            "copied": copied,
            "focused": focused,
            "app": app_path.stem if app_path else None,
            "repaired_link": repaired_link,
            "reason": "test_mode" if test_mode else "ok",
            "ts": round(time.time(), 1),
        }
        self._probe_cache["antigravity_bridge"] = res
        return res

    def run_action(self, module_id: str, action: str, *, test_mode: bool = False) -> dict:
        if module_id == "codex_engine":
            if action == "stop":
                self.toggle_module("codex_engine", False)
                return {"module": module_id, "action": action, "ok": True, "detail": "已暂停 Codex 模块"}
            if action == "start":
                self.toggle_module("codex_engine", True)
            probe = self.probe_codex()
            return {"module": module_id, "action": action, "ok": probe["status"] == "running", **probe}

        if module_id == "antigravity_bridge":
            if action == "stop":
                self.toggle_module("antigravity_bridge", False)
                return {"module": module_id, "action": action, "ok": True, "detail": "已暂停 Gemini / Antigravity 联动"}
            if action == "start":
                self.toggle_module("antigravity_bridge", True)
                res = self.launch_antigravity(copy_trigger=True, focus_app=True, test_mode=test_mode)
                return {"module": module_id, "action": action, "ok": True, **res}
            res = self.launch_antigravity(copy_trigger=False, focus_app=False, test_mode=test_mode)
            return {"module": module_id, "action": action, "ok": True, **res}

        if module_id == "mcp_bridge":
            if action == "stop":
                self.toggle_module("mcp_bridge", False)
                return {"module": module_id, "action": action, "ok": True, "detail": "已关闭 MCP 协议桥"}
            if action == "start":
                self.toggle_module("mcp_bridge", True)
            probe = self.probe_mcp()
            return {"module": module_id, "action": action, "ok": probe["status"] == "running", **probe}

        if module_id == "sandbox_guard":
            if action == "stop":
                self.toggle_module("sandbox_guard", False)
                return {"module": module_id, "action": action, "ok": True, "detail": "已暂停沙箱命令检查（文件写锁仍生效）"}
            if action == "start":
                self.toggle_module("sandbox_guard", True)
            probe = self.probe_sandbox()
            return {"module": module_id, "action": action, "ok": probe["status"] == "running", **probe}

        if module_id == "storage_workspace":
            if action == "open" and not test_mode and sys.platform == "darwin":
                try:
                    subprocess.Popen(["open", str(self.data)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return {"module": module_id, "action": action, "ok": True, "detail": "已在 Finder 中打开数据目录"}
                except OSError:
                    pass
            return {"module": module_id, "action": action, "ok": True, "detail": f"数据目录：{self.data}"}

        if module_id == "web_kernel":
            return {"module": module_id, "action": action, "ok": True, "detail": f"内核运行正常 (PID {os.getpid()})"}

        raise ValueError(f"未知模块：{module_id}")

    def start_all(self, *, focus_antigravity: bool = False, test_mode: bool = False) -> dict:
        cfg = self.pipeline_store.load()
        data = cfg.model_dump(mode="json")
        data["modules"] = {
            "gemini_enabled": True,
            "codex_enabled": True,
            "mcp_enabled": True,
            "sandbox_checks_enabled": True,
        }
        self.pipeline_store.save(data)
        results = {
            "codex_engine": self.probe_codex(),
            "mcp_bridge": self.probe_mcp(),
            "sandbox_guard": self.probe_sandbox(),
            "antigravity_bridge": self.launch_antigravity(
                copy_trigger=focus_antigravity,
                focus_app=focus_antigravity,
                test_mode=test_mode,
            ),
        }
        return {"ok": True, "results": results}
