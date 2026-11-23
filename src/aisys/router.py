"""Smart capability-aware task routing and mechanical anti-overlap division validator."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

DENIED_PARTS = {
    ".git",
    ".codex",
    ".gemini",
    ".agents",
    ".mcp.json",
    ".ssh",
    ".aws",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".kube",
    "credentials",
    "auth.json",
    "secrets",
    "secret",
    "credentials.json",
    ".gitmodules",
}
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
TASK_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,23}$")

# Weighted capability signals for optimal Gemini vs Codex scheduling (both stay 100% full-power)
GEMINI_SIGNALS: list[tuple[str, int, str]] = [
    (
        r"\b(ui|ux|css|html|svg|design|layout|frontend|react|vue|tailwind|canvas|animation)\b|界面|美观|视觉|交互|动效|设计|前端|排版",
        3,
        "视觉与前端交互设计",
    ),
    (
        r"\b(architect|architecture|prd|roadmap|spec|rfc|diagram|mermaid|docs?|readme|guide)\b|架构|规划|方案|蓝图|文档|报告|梳理",
        3,
        "系统架构与全局统筹",
    ),
    (
        r"\b(learn|study|explain|tutorial|concept|mental model|research|compare|survey)\b|学习|研学|原理|搞懂|教程|概念|调研|对比|总结",
        3,
        "第一性原理与结构化研学",
    ),
    (
        r"\b(browser|web page|screenshot|image|generate image|slide|presentation)\b|浏览器|截图|生图|网页操作",
        4,
        "浏览器与多模态原生工具",
    ),
]

CODEX_SIGNALS: list[tuple[str, int, str]] = [
    (
        r"\b(algorithm|parser|compiler|concurrency|async|lock|mutex|btree|graph|sql|db|migration|kernel|cli|sdk)\b|算法|并发|锁|状态机|解析器|数据库|底层|内核|引擎",
        3,
        "复杂算法与底层系统实现",
    ),
    (
        r"\b(debug|bug|fix|crash|race|deadlock|memory leak|profile|benchmark|perf|optimize|refactor)\b|排查|修复|报错|崩溃|竞态|死锁|性能|压测|重构",
        3,
        "根因定位、性能调优与重构",
    ),
    (
        r"\b(test|pytest|unittest|jest|vitest|fuzz|edge case|assert|ci|build|script|bash|shell)\b|测试|单测|边界|断言|验证|脚本|自动化",
        3,
        "高频终端测试与边界验证",
    ),
    (r"\b(review|audit|security|vuln|diff|pr)\b|审查|审计|安全|漏洞|代码检查", 2, "仓库级深度代码审查"),
]


def normalize_owned_path(raw: str) -> str:
    """Validate and canonicalize a relative file path or directory prefix (trailing '/')."""
    if not isinstance(raw, str):
        raise ValueError("owned_paths 元素必须为字符串")
    text = raw.strip()
    if not text or len(text) > 300:
        raise ValueError(f"非法路径长度：{raw!r}")
    is_dir = text.endswith("/")
    core = text.rstrip("/")
    if not core:
        raise ValueError("owned_paths 不能为根目录 '/'，必须明确限定子路径或文件")
    path = PurePosixPath(core)
    if path.is_absolute() or str(path) != core or not path.parts:
        raise ValueError(f"必须使用规范相对路径：{raw!r}")
    for part in path.parts:
        low = part.casefold()
        if (
            part in {".", ".."}
            or low in DENIED_PARTS
            or low.startswith(".env")
            or "\\" in part
            or ":" in part
            or any(ord(c) < 32 or ord(c) == 127 for c in part)
            or PurePosixPath(part).suffix.casefold() in DENIED_SUFFIXES
        ):
            raise ValueError(f"路径包含禁止访问的敏感片段或字符：{raw!r}")
    return f"{core}/" if is_dir else core


def paths_overlap(a: str, b: str) -> bool:
    """True if two normalized file/directory ownership specs intersect."""
    a_dir = a.endswith("/")
    b_dir = b.endswith("/")
    a_core = a.rstrip("/")
    b_core = b.rstrip("/")
    if a_core == b_core:
        return True
    if a_dir and b_core.startswith(a_core + "/"):
        return True
    if b_dir and a_core.startswith(b_core + "/"):
        return True
    return False


def analyze_and_route(task: str, *, mode: str = "auto") -> dict[str, Any]:
    """Analyze task characteristics and design optimal Gemini + Codex dual full-power routing."""
    text = (task or "").strip()
    if not text:
        raise ValueError("任务描述不能为空")

    g_score = 0
    c_score = 0
    g_hits: list[str] = []
    c_hits: list[str] = []

    for pattern, weight, label in GEMINI_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            g_score += weight
            g_hits.append(label)
    for pattern, weight, label in CODEX_SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            c_score += weight
            c_hits.append(label)

    explicit_mode = mode if mode in {"dev", "learn", "review", "fast"} else "auto"
    if explicit_mode == "learn" or (
        explicit_mode == "auto" and any("研学" in h for h in g_hits) and c_score <= g_score
    ):
        category = "deep_learn"
        resolved_mode = "learn"
        lead = "gemini"
        rationale = (
            "研学/原理类任务：由满血 Gemini 主导第一性原理框架、对比心智模型与结构化 Markdown 文档编排；"
            "由满血 Codex（开实时联网 + 终端执行）负责可运行代码示例、极端边界陷阱与可执行断言验证，再互相勘误。"
        )
        division_blueprint = [
            "Gemini 独占：总体认知框架、原理图解、概念对比与最终学习指南整合（如 sections/01_mental_model.md）",
            "Codex 独占：可运行代码实验室、反直觉陷阱复现、自动化断言验证与实操习题（如 sections/02_runnable_lab.md, examples/）",
        ]
    elif explicit_mode == "review":
        category = "code_review"
        resolved_mode = "review"
        lead = "co_lead"
        rationale = (
            "双模型交叉审查：满血 Codex 直接运行原生 `codex exec review` 结合全仓测试审计实现细节与边界；"
            "满血 Gemini 独立审查架构一致性、安全性与需求覆盖度，交叉去重后输出高置信度问题清单。"
        )
        division_blueprint = [
            "Codex：原生 `codex_review` 深度检查实现缺陷、并发/边界错误并运行测试验证",
            "Gemini：独立审查架构契约、安全假设、可维护性与用户体验，汇总双方交集与分歧",
        ]
    elif c_score >= g_score + 3:
        category = "backend_or_debug"
        resolved_mode = "dev"
        lead = "codex"
        rationale = (
            "核心算法/底层工程/排障密集型任务：由满血 Codex 领衔核心模块实现、根因排查与边界/压测闭环；"
            "由满血 Gemini 负责整体接口契约、跨模块架构把关、独立代码审查、文档集成与交付验证。"
        )
        division_blueprint = [
            "Codex 独占（隔离 Worktree）：核心算法/引擎/修复代码 + 完整单测与边界测试（自测全绿）",
            "Gemini 独占（主分支）：接口契约对齐、外围集成/配置/文档、独立审计与合流后全量回归",
        ]
    elif g_score >= c_score + 3:
        category = "architecture_or_ux"
        resolved_mode = "dev"
        lead = "gemini"
        rationale = (
            "架构规划/前端视觉/综合设计型任务：由满血 Gemini 领衔架构方案、视觉交互与主模块实现；"
            "由满血 Codex 负责后端辅助/数据处理逻辑、自动化回归测试与严格边界审查。"
        )
        division_blueprint = [
            "Gemini 独占（主分支）：系统架构、前端交互与视觉实现、核心说明文档",
            "Codex 独占（隔离 Worktree）：配套核心工具/校验逻辑、自动化测试与边界验证",
        ]
    else:
        category = "fullstack_balanced"
        resolved_mode = "dev"
        lead = "co_lead"
        rationale = (
            "综合全栈/多模块工程任务：双模型旗鼓相当，采用双核真并行策略——先各自满血分析并对齐接口契约，"
            "按目录/文件物理切分为零重叠 `owned_paths`，在双 Worktree 中同时满血执行与自测，再原生交叉 Review 合流。"
        )
        division_blueprint = [
            "Wave 1（共享契约）：先对齐数据模型、接口签名与严格互斥的 owned_paths",
            "Wave 2（双核物理并行）：Gemini 与 Codex 在各自 Worktree 满血实现各自模块并各自跑通测试",
            "Wave 3（合流互审）：Diff 闸门校验 0 越界 → 合并 → Codex 原生 review + Gemini 审查 → 全量回归",
        ]

    return {
        "mode": resolved_mode,
        "category": category,
        "lead_model": lead,
        "dual_full_power": True,
        "scores": {"gemini": g_score, "codex": c_score},
        "matched_strengths": {
            "gemini": g_hits or ["全局架构与综合统筹"],
            "codex": c_hits or ["工程实现与终端验证"],
        },
        "rationale": rationale,
        "codex_execution_profile": {
            "sandbox": "workspace-write",
            "web_search": "live",
            "worktree_isolation": resolved_mode != "review",
            "multi_turn_resume": True,
        },
        "division_blueprint": division_blueprint,
        "fast_track": explicit_mode == "fast",
    }


def validate_division(subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Mechanically enforce disjoint owned_paths across owners & concurrent waves, plus acyclic DAG."""
    conflicts: list[str] = []
    warnings: list[str] = []
    if not subtasks:
        return {"valid": False, "conflicts": ["分工表不能为空，至少需要 1 个子任务。"], "warnings": [], "waves": []}

    parsed: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    for idx, raw in enumerate(subtasks):
        if not isinstance(raw, dict):
            conflicts.append(f"第 {idx + 1} 项分工格式无效")
            continue
        tid = str(raw.get("id") or "").strip()
        owner = str(raw.get("owner") or "").strip().lower()
        title = str(raw.get("title") or "").strip()
        deps = [str(d).strip() for d in (raw.get("depends_on") or []) if str(d).strip()]
        raw_paths = raw.get("owned_paths") or []

        if not TASK_ID_RE.match(tid):
            conflicts.append(f"子任务 ID 非法：{tid!r}（须为字母开头，仅含字母/数字/_/-）")
            continue
        if tid in by_id:
            conflicts.append(f"子任务 ID 重复：{tid}")
            continue
        if owner not in {"gemini", "codex"}:
            conflicts.append(f"子任务 {tid} 的 owner 必须为 'gemini' 或 'codex'，收到：{owner!r}")
            continue
        if not title:
            conflicts.append(f"子任务 {tid} 缺少 title")
        if not raw_paths:
            conflicts.append(f"子任务 {tid} 必须声明至少 1 个独占 owned_paths")
            continue

        norm_paths: list[str] = []
        for rp in raw_paths:
            try:
                np = normalize_owned_path(rp)
                if np not in norm_paths:
                    norm_paths.append(np)
            except ValueError as err:
                conflicts.append(f"子任务 {tid}: {err}")

        item = {
            "id": tid,
            "owner": owner,
            "title": title,
            "owned_paths": norm_paths,
            "depends_on": deps,
        }
        parsed.append(item)
        by_id[tid] = item

    # Dependency reference & self-loop checks
    for st in parsed:
        for dep in st["depends_on"]:
            if dep == st["id"]:
                conflicts.append(f"子任务 {st['id']} 不能依赖自身")
            elif dep not in by_id:
                conflicts.append(f"子任务 {st['id']} 依赖未知任务 {dep!r}")

    # Cross-owner path disjointness (hard rule: Gemini and Codex NEVER share overlapping paths)
    for i, a in enumerate(parsed):
        for b in parsed[i + 1 :]:
            if a["owner"] == b["owner"]:
                continue
            for pa in a["owned_paths"]:
                for pb in b["owned_paths"]:
                    if paths_overlap(pa, pb):
                        conflicts.append(
                            f"跨模型路径重叠：{a['id']}({a['owner']}: {pa}) 与 {b['id']}({b['owner']}: {pb})"
                        )

    # Topological wave computation (detects cycles)
    waves: list[list[str]] = []
    if not conflicts:
        indeg = {st["id"]: len(set(st["depends_on"])) for st in parsed}
        dependents: dict[str, list[str]] = {st["id"]: [] for st in parsed}
        for st in parsed:
            for dep in set(st["depends_on"]):
                dependents[dep].append(st["id"])

        remaining = set(by_id)
        while remaining:
            ready = sorted(tid for tid in remaining if indeg[tid] == 0)
            if not ready:
                conflicts.append(f"任务依赖存在循环：{sorted(remaining)}")
                break
            waves.append(ready)
            for tid in ready:
                remaining.remove(tid)
                for nxt in dependents[tid]:
                    indeg[nxt] -= 1

    # Same-owner concurrent wave overlap check
    for w_idx, wave in enumerate(waves, start=1):
        for i, tid_a in enumerate(wave):
            for tid_b in wave[i + 1 :]:
                a, b = by_id[tid_a], by_id[tid_b]
                for pa in a["owned_paths"]:
                    for pb in b["owned_paths"]:
                        if paths_overlap(pa, pb):
                            conflicts.append(f"Wave {w_idx} 同波次路径冲突：{a['id']}({pa}) 与 {b['id']}({pb})")

    owners_used = {st["owner"] for st in parsed}
    if len(parsed) >= 2 and len(owners_used) < 2:
        warnings.append(f"所有子任务均分配给了 {next(iter(owners_used))}，未发挥双模型并行协同优势。")

    return {
        "valid": not conflicts,
        "conflicts": conflicts,
        "warnings": warnings,
        "waves": waves,
        "normalized_subtasks": parsed,
    }
