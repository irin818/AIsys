---
name: dc
description: >-
  Explicit `/dc` command driver for Dual Full-Power Collaboration (Gemini Host in Antigravity + Full-Power OpenAI Codex CLI Sub-Agent via MCP). Activates on `/dc`, `/dc:dev`, `/dc:learn`, `/dc:review`, `/dc:fast` with smart capability routing, physical Git Worktree isolation, multi-turn Codex resume, mechanical anti-overlap Diff Gate, and native cross-review.
---

# `/dc` · 双满血协同执行协议（Gemini 主控 × Codex 子 Agent）

## 指令速查

| 指令 | 模式 | 流程 |
| --- | --- | --- |
| `/dc <任务>` | `auto`（智能判定） | 阶段 0 智能调度 → 1 并行满血提案 → 2 多轮互审与零重叠分工 → 3 一句话拍板/融合 → 4 物理隔离真并行执行 → 5 原生互相审查与交付 |
| `/dc:dev <需求>` | `dev` | 强制工程开发模式（Git Worktree 隔离 + Diff 写锁闸门 + 原生 `codex_review` + 联合测试） |
| `/dc:learn <主题>` | `learn` | 强制深度研学模式（双模型开实时联网 + 可运行代码验证 → 输出经过实测的结构化 Markdown 学习指南） |
| `/dc:review [分支/SHA]` | `review` | 即时双满血代码审查（`codex_review` 仓库级审查 + Gemini 独立架构/安全审查 → 合并高置信度清单） |
| `/dc:fast <任务>` | `fast` | 智能调度后跳过阶段 2 辩论与阶段 3 停顿，直接锁死无重叠分工真并行执行 + 交叉审查交付 |

---

## 核心铁律（双满血 + 最优调度 + 零重叠）

1. **双满血原则**：
   - **Gemini（你）** 保留 Antigravity 全部原生工具（`read_file`、`edit_file`、`run_command`、`search_web`、`browser`、`image-generator`、`invoke_subagent`）。
   - **Codex（子 Agent）** 始终保持全功能满血开放：通过 MCP 服务 `aisys-dual_aisys_codex` 调用，**永远允许**全仓读取、终端命令测试、实时联网搜索（`web_search: true`）、多轮记忆续接（`codex_reply` → `codex exec resume`）与原生仓库级审查（`codex_review`）。
2. **先路由再开工（Stage 0）**：先调 `dual_route(task, mode)` 根据任务属性识别谁做 Lead、谁做 Co-Lead，发挥双方比较优势，避免重复劳动与无效 Token 浪费：
   - **Gemini 优先领衔**：全局系统架构、前端视觉/UI/交互、多模态/浏览器调研、第一性原理知识体系与最终文档集成。
   - **Codex 优先领衔**：核心算法/并发/底层状态机实现、复杂 Bug 根因定位与性能重构、高频终端测试/压测、可运行案例与边界陷阱验证。
3. **机械防重叠（两道硬闸）**：
   - 开工前必须调 `dual_check_division(subtasks)` 确保 Gemini 与 Codex 的 `owned_paths` 交集为空且无依赖环；
   - Codex 在独立 Git Worktree 完成后，**必须**通过 `codex_merge(job_id)` 执行 Diff 路径闸门——若 Codex 越界改动了非授权路径，立即用 `codex_reply(job_id, ...)` 令其回退越界修改后再合流！

---

## 标准执行步骤

### Step 0 · 智能任务调度（Smart Routing）
1. 解析用户指令（`/dc` → `auto`，`/dc:dev` → `dev`，`/dc:learn` → `learn`，`/dc:review` → `review`，`/dc:fast` → `fast`）。
2. 调用 MCP 工具 `dual_route(task=..., mode=...)`（若当前对话未重载 MCP，可用 `PYTHONPATH=~/Documents/AIsys/src python3 -m aisys route ...` 兜底）。
3. 若为 `/dc:review`：
   - 立即并行执行：`codex_review(project_dir=<workspace>, mode="uncommitted"|"base"|"commit", target=...)` 与 Gemini 独立 `git diff` 审查；
   - 汇总双方共同命中的高危问题、各自独有发现与修复建议，直接交付。

### Step 1 · 双满血并行初案（Parallel Full-Power Proposals）
1. 立即调用 `codex_spawn(prompt=..., project_dir=<workspace>, sandbox="read-only", isolate_worktree=false, web_search=true, background=true)` 让满血 Codex 在后台自己读全仓/查资料提出独立初案；
2. **同一时间**，Gemini 用原生工具自主读项目/联网，形成自己的独立初案；
3. 调用 `codex_status(job_id, wait=true)` 拿到 Codex 的完整提案与 `thread_id`。

### Step 2 · 多轮互审 + 机械防重叠分工（`/dc:fast` 跳过辩论直入分工校验）
1. 调用 `codex_reply(job_id, prompt=...)` 把 Gemini 的初案与对 Codex 的客观挑刺发给**同一个 Codex 线程**（继承全部上下文与 ~90% 缓存），让 Codex 反审 Gemini、吸收优点并提出建议的互斥分工；
2. Gemini 吸收 Codex 的优点与批评，综合双方特长与 `dual_route` 建议，构造严格无重叠的 `subtasks[]`（明确每项 `id`、`owner`、`title`、`owned_paths`、`depends_on`）；
3. **强制调用 `dual_check_division(subtasks)`** 校验 `valid == true`（若存在任何路径重叠或循环依赖，修正直至 `valid == true`）。

### Step 3 · 极简拍板 / 融合微调（`/dc:fast` 自动跳过）
向用户呈现极简清晰的三段式摘要，请用户一句话拍板：
- **智能路由结论**：谁主攻、谁协攻、为何最优；
- **双模型互审精华对比**：Gemini 方案 vs Codex 方案各自吸收了对方什么、关键差异在哪；
- **已通过机械防重叠校验的分工波次表**（列出 `Wave`、`owner`、`owned_paths`）。
等待用户回复（如 `1` 采纳 / `2` 采纳 / 一句融合微调意见），绝不自作主张跳过人类拍板（除非 `/dc:fast`）。

### Step 4 · 物理隔离双满血真并行执行
1. 锁定最终分工（若用户提出融合微调，调整 `subtasks[]` 并再次调 `dual_check_division` 验证 `valid == true`）。
2. 为 Codex 负责的波次调用：
   `codex_spawn(prompt=..., project_dir=<workspace>, sandbox="workspace-write", isolate_worktree=true, owned_paths=<codex_paths>, web_search=true, background=true)`
   （Codex 在独立 Git Worktree 内满血写代码、跑真实测试直到全绿）。
3. **同一时间（物理并行！）**，Gemini 在主工作区使用原生 `edit_file` / `write_file` / `run_command` 满血完成 Gemini 的独占 `owned_paths` 并跑通自己的检查（绝不触碰 Codex 的 `owned_paths`）。
4. 调用 `codex_status(job_id, wait=true, include_diff=true)` 等待 Codex 交付。
5. 调用 `codex_merge(job_id, cleanup_after=false)` 执行**机械 Diff 防重叠闸门**：
   - 若 `reason == "ownership_violation"`：用 `codex_reply(job_id, "合流闸门检测到越界文件：<violations>，请回退这些文件仅保留 <owned_paths> 改动并重跑测试")` 定点清理，再重跑 `codex_merge`；
   - 合并通过后再清理临时 Worktree（待 Stage 5 审查完结后 `codex_cleanup(action="cleanup", job_id=...)`）。

### Step 5 · 双满血互相审查 + 定点返工 + 最终交付
1. **Codex 审查 Gemini（及合流后整体）**：调用 `codex_review(project_dir=<workspace>, mode="uncommitted")`；
2. **Gemini 独立审查 Codex**：细读合流后的 Codex diff，并运行完整测试/lint/构建；
3. 若发现实质缺陷，按归属定点修复（Codex 归属用 `codex_reply` 修复并重测，Gemini 归属用 `edit_file` 修复），最多 2 轮收敛；
4. 清理临时 Worktree（`codex_cleanup`），按模式交付：
   - **`dev` 模式**：在任务分支交付清晰变更报告、真实测试输出与 `git status`（不未经授权强推或改 `main`）；
   - **`learn` 模式**：汇编双方经过代码实测与交叉勘误的完整结构化 Markdown 学习文档（保存为项目内或用户指定 `.md` 文件并附可点击链接）。
