# Antigravity × AIsys 集成架构与合规调查

本文记录 2026-11-23 在官方 Google Antigravity 环境内完成的原生能力调研、合规核查、方案对比，以及最终落地的 **Antigravity 主控 + 统一任务交换协议** 集成方案。

---

## 一、Antigravity 官方能力与授权边界核查

### 1. 本机实际环境

- **官方客户端**：已安装 `/Applications/Gemini.app`（当前会话即运行在官方 Antigravity 环境内，原生使用用户已登录的 Google 个人订阅）。
- **Antigravity CLI (`agy`)**：本机 PATH 及常规目录未安装 `agy` 可执行文件。
- **Codex CLI**：位于 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`（`0.171.0-alpha.13`，`Logged in using ChatGPT`）。此前仅在 ChatGPT.app 注入的 PATH 下可见；已在 `src/aisys/backends.py` 中增加 macOS 官方 ChatGPT.app 路径回退发现（仍优先尊重 PATH 上的 `codex`），并增加 `-c project_doc_max_bytes=0` 避免隔离运行目录向上误读宿主仓库 `AGENTS.md` 触发 macOS 权限错误。

### 2. 扩展接口分类与合规状态

| 能力 / 接口 | 官方支持状态 | 费用 / 许可 | 本项目结论 |
| --- | --- | --- | --- |
| Antigravity 客户端原生工具（Terminal `run_command`、File tools、Subagents `invoke_subagent`） | **官方明确支持** | 含在现有 Google 订阅内（\$0 额外费用） | **采用**：在用户主动发起的 Antigravity 会话中作为主控环境驱动 AIsys，并用独立 Subagent 实现角色隔离（如独立 Reviewer）。 |
| Antigravity Workspace Customizations（`.agents/plugins/`、Skills、Rules、本地 Stdio MCP Server） | **官方明确支持** | 含在现有 Google 订阅内（\$0 额外费用） | **采用**：在仓库内提供 `.agents/plugins/aisys-bridge/`（含 `aisys-driver` Skill 与零依赖 `aisys mcp` stdio 服务），不修改任何全局配置。 |
| Antigravity CLI Headless 模式 (`agy -p`) 供用户直接脚本/CI 使用 | 官方明确支持（限用户直接脚本/CI） | 含在现有 Google 订阅内 | 仅限用户直接运行；**禁止**封装进 AIsys 等第三方编排器。 |
| 在第三方编排器/Agent 中封装 `agy -p` 或提取个人 OAuth 凭据 | **明确不支持 / 违反条款** | 个人订阅禁止（可能触发账号封禁） | **明确拒绝**：`GoogleBackend` 保持只读占位，AIsys 绝不调用 `agy` 或读取 Google OAuth 状态。 |
| 个人订阅下的无人值守后台轮询守护进程 | **明确不支持 / 违反条款** | 个人订阅禁止 | **明确拒绝**：不实现常驻轮询 daemon，所有执行均在用户交互式 Antigravity 会话内同步推进。 |
| Agent Client Protocol (`agy acp`) | 官方支持编辑器接入（Zed / JetBrains / IDE 扩展） | 个人 OAuth 不支持自定义外部 harness；headless 需 `GEMINI_API_KEY` | **不采用**：不满足“个人订阅零额外费用 + 外部编排器”约束。 |
| Antigravity Python SDK (`google-antigravity`) / Gemini API / Vertex AI | **官方明确支持**（面向程序化与第三方应用） | **需要额外 API 计费或企业许可**（`GEMINI_API_KEY` / `VERTEX_API_KEY` / GCP ADC），与个人订阅额度不互通 | **暂不启用**：保留为未来按统一 `Backend` 协议可选扩展的路径。 |
| Gemini CLI 消费者登录 | **已退役**（2026-06-18 起停止个人/Pro/Ultra 登录） | 不适用 | 不可用。 |

参考依据：
- Google Antigravity Additional Terms of Service (`https://antigravity.google/terms`)
- Google Antigravity FAQ & Headless CLI Docs (`https://antigravity.google/docs/faq`, `https://antigravity.google/docs/cli/headless`)
- Google Antigravity SDK & Programmatic Auth (`https://antigravity.google/docs/install`, `https://github.com/google-antigravity/antigravity-sdk-python`)
- OpenAI Codex CLI Non-Interactive & Auth Docs (`https://learn.chatgpt.com/docs/non-interactive-mode`, `https://learn.chatgpt.com/docs/auth`)

---

## 二、候选架构对比

| 维度 | 方案 A：Antigravity 主控 + 同步任务交换（**已采用**） | 方案 B：AIsys 主控（当前基线） | 方案 C：AIsys 任务队列 + 后台轮询交换 | 方案 D：官方 SDK / Gemini API |
| --- | --- | --- | --- | --- |
| **是否合规** | **合规**：所有 Google 推理均在官方 Antigravity 客户端的用户主动会话内完成；AIsys 仅作本地工具被调用，不接触 Google 凭据。 | 仅保留人工交接时合规；若自动封装 `agy -p` 则**不合规**。 | 仅同步交换合规；**无人值守后台轮询消耗个人订阅额度不合规**。 | **合规**（官方支持的程序化路径）。 |
| **是否需要额外 API 费用** | **否（\$0）**，复用现有 Google Antigravity + ChatGPT/Codex 订阅。 | **否（\$0）**。 | 个人订阅下不可合规自动轮询；改用 API 需额外付费。 | **是**（需 `GEMINI_API_KEY` / Vertex 计费或企业许可）。 |
| **自动化程度** | **高**：单入口内完成双模型规划、Codex 自动编码、Seatbelt 沙箱测试、Antigravity 独立审查及自动返工闭环；人工审批节点仍严格停顿待用户确认。 | 低：每个 Google 节点均需人工复制提示词与导入结果。 | 理论上高，但受个人订阅政策限制无法合规实现无人后台拉取。 | 极高（可后台无交互运行）。 |
| **技术复杂度** | **低–中**：复用 AIsys 全部 LangGraph/SQLite/Worktree/Seatbelt 内核，仅新增协议层 `protocol.py`、CLI `exchange`、stdio `mcp_server.py` 与 `.agents/` 插件。 | 极低（已有实现）。 | 高（需守护进程、锁协调、异常处理且存在合规风险）。 | 中（需新增 API 适配与计费管理）。 |
| **可靠性** | **高**：状态与检查点完全由 AIsys SQLite + `effects` 去重表托管，支持跨进程/SIGTERM 恢复与角色写路径强校验。 | 高。 | 中（轮询竞态与进程生命周期管理复杂）。 | 高。 |
| **可扩展性** | **高**：所有角色、顺序、并行分支、条件路由、循环上限仍由 YAML 控制；未来如需付费 API 仍可沿 `Backend` 协议无缝接入。 | 中。 | 中。 | 高。 |
| **是否保留现有 AIsys** | **100% 保留**：全部既有预设、CLI、Textual UI 与人工交接完全兼容。 | 100% 保留。 | 需较大改造。 | 可保留核心引擎。 |

**决策结论**：采用 **方案 A（Antigravity 主控）融合方案 C 的结构化任务交换协议（同步步进、零后台轮询）**。这是在零额外 API 费用、完全遵守双方官方条款前提下，自动化程度最高且完整保留 AIsys 原有架构的唯一合规方案。

---

## 三、统一任务交换协议（Unified Task Exchange Protocol）

实现位于 `src/aisys/protocol.py`，可通过 `aisys exchange schema` 导出完整 JSON Schema。

### 1. 任务信封（`TaskEnvelope`）

同时覆盖待执行任务（`pending_tasks`）与已完成历史节点（`completed_tasks`，含 Codex、Google 及本地检查），统一字段包括：

- `protocol_version`: 协议版本（`1`）
- `task_id`: 确定性节点访问 ID（`<run_task_id>:<node>:v<visit>`），支持重放校验与防误提交
- `workflow_id`: 工作流名称
- `run_id`: AIsys 运行 UUID
- `interrupt_id`: LangGraph 持久化中断 ID（待处理时存在）
- `node` / `kind`: 节点名与类型（`handoff` / `approval` / `pause` / `agent` / `check` / `report` / `parallel` / `route`）
- `agent_role` / `backend`: 逻辑角色与执行后端（`google` / `codex` / `local`）
- `objective`: 任务目标及节点指令
- `context`: 结构化上下文（`files`、`upstream_results`、`role_instructions`、`node_instruction`、`project_notes`、`rendered_prompt` 等，严格受 `budget.context_chars` 与节点显式声明约束）
- `working_directory`: 隔离工作区路径（只读参考）
- `permissions`: 显式权限声明（`mode`: `read_only` / `scoped_write` / `approval` / `local_check`，`write_paths`，`requires_verdict`，`allow_shell: false`，`sandbox_policy`）
- `status`: 明确生命周期状态（`pending` → `submitted` → `completed` / `rejected` / `failed`）
- `result`: 结构化执行结果（`status`、`verdict`、`output`、`duration`、`usage`、`exit_code`、`attempt`、`session_id`）
- `artifacts`: 实际写入工作区的相对路径列表
- `review_feedback`: 独立审查反馈文本（自动注入下游返工节点提示词）
- `error`: 错误摘要（已脱敏）
- `created_at` / `updated_at`: 时间戳

### 2. 安全护栏与持久化审计

- **人工审批防绕过**：对 `kind == "approval"` 的节点，`TaskSubmission` 必须显式携带 `human_confirmed: true`（或 CLI `--human-confirmed`），否则直接拒绝；`.agents/plugins/aisys-bridge/skills/aisys-driver/SKILL.md` 规定 Antigravity 遇到 `approval` 必须停下征求用户明确批准。
- **严格路径白名单**：`scoped_write` 角色提交的 `files` 必须完全位于 `agent.write_paths` 内、经凭据脱敏校验、通过 AIsys 原子写入并记录进 SQLite `effects` 表，实现崩溃幂等。
- **审计落盘**：所有交换快照与提交历史保存在已忽略的 `data/<run-id>/exchange/snapshot.json` 与 `history.jsonl`（权限 `0600`）。

---

## 四、工作区级 Antigravity 插件与 MCP

仓库内置 `.agents/plugins/aisys-bridge/`（工作区级自动发现，无需修改 `~/.gemini/config/`）：

- `plugin.json`: 插件清单
- `skills/aisys-driver/SKILL.md`: Antigravity 主控执行手册（含角色隔离、并行处理、审批停顿、崩溃恢复）
- `mcp_config.json` + `scripts/run-mcp.sh`: 零外部依赖的本地 stdio MCP 服务（`aisys mcp`），向 Antigravity 暴露 `aisys_doctor`、`aisys_list_presets`、`aisys_validate_workflow`、`aisys_start_run`、`aisys_get_pending_tasks`、`aisys_submit_task_result`、`aisys_resume_run`、`aisys_inspect_run`、`aisys_get_diff`、`aisys_get_report` 工具。
