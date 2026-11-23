# 接入调查 · 2026-11-23

## 本机环境

| 项目 | 实测 |
| --- | --- |
| macOS | 26.5（25F71），Apple Silicon arm64 |
| 运行时 | Python 3.14.6 和 3.12.13，Node.js 22.23.1，npm 10.9.8，Git 2.54.0，Python 3.14 的 SQLite 3.53.2 |
| Codex | ChatGPT.app 内置的 Codex CLI 0.171.0-alpha.13。也发现了现有的 `codex-a` 和 `codex-b` 启动器。本项目测试使用 PATH 上的官方 Codex CLI。 |
| Google | 在 PATH、常规安装位置及本地 Gemini 配置目录中未找到可执行的 `gemini` 或 `agy`。发现 Gemini.app 与 Antigravity 相关数据目录。CLI 版本、登录和实际模型访问均未确认。 |
| 扩展 | Codex 具备本机 Skills，支持 hooks、角色及 MCP。通过 `codex mcp list` 检查到现有 MCP 注册。自动化执行关闭这些全局扩展，待有具体工具需求时再单独接入。 |
| 项目 | AIsys 原为空目录，没有 Git 远程或项目级配置。已定位 SpireLab Mod 的 Git 仓库，当前开发树可以只读核对，并以提交快照作为隔离试运行的来源。 |

只检查了命令、版本、配置文件是否存在及公开的登录状态。CLI 缓存和凭据文件由官方客户端自行管理。

## Codex

`codex login status` 返回 `Logged in using ChatGPT`。在临时空目录用环境变量允许列表执行以下命令，stdin 要求返回 `AISYS_CODEX_OK`，退出码为 **0**，最终文本匹配，观察到 `thread.started`、`item.completed` 和带 usage 的 `turn.completed`。

```sh
codex exec --ignore-user-config --ignore-rules \
  -c 'model_provider="openai"' -c 'approval_policy="never"' \
  --sandbox read-only --skip-git-repo-check --ephemeral --json -
```

本地帮助和官方文档确认 `-C` 可指定初始工作目录，`-m` 可选模型，`exec resume <ID> -` 可按会话 ID 延续；续接时用子进程 cwd 显式定位工作目录。模型可用列表取决于账号，探针使用默认模型。官方允许在本地脚本中使用 `codex exec` 并复用 CLI 登录。ChatGPT 登录使用 ChatGPT 计划的 Codex 使用额度，API key 登录使用单独的 API 计费。可记录 CLI 实际返回的 token 计数，账号额度余额未读取。 [非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode) · [认证与计费](https://learn.chatgpt.com/docs/auth) · [CLI 参数](https://learn.chatgpt.com/docs/cli/reference)

## Google

- 自 2026-06-18 起，Gemini CLI 的消费者 Google 登录停止为个人版、Google AI Pro 和 Ultra 提供服务。Standard / Enterprise 有独立的退役时间表。 [Google 弃用公告](https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals)
- Google 消费者订阅权益可在 Antigravity 的官方客户端使用，含基线额度。直接脚本和 CI 属于官方 headless 用法。文档确认 `agy -p`、stdin 流式输入、`--output-format json/stream-json`、`--conversation <ID>` 和 `--model`，执行以启动 cwd 为工作区。这些是官方文档能力，本机未验证。 [Antigravity 计划](https://antigravity.google/docs/plans/) · [Headless 模式](https://antigravity.google/docs/cli/headless/)
- Google FAQ 明确把将 `agy` 封装进第三方 coding agents 或应用排除在个人订阅的支持范围之外。本项目属于第三方 Python 编排应用。官方 SDK 的程序化认证列出 Gemini API key、Google Cloud/Vertex AI 和离线本地模型，未列出用个人订阅认证 SDK 的方法。ACP 是针对官方文档列出的编辑器集成路线，未确认可用于此编排器。当前未找到同时满足个人订阅、自动调用和官方支持的 Google 云模型接入。 [Google FAQ](https://antigravity.google/docs/faq/#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching) · [SDK 认证](https://antigravity.google/docs/install/#programmatic-authentication)
- 采用 Google 后端可用性占位和显式人工交接。可在受支持的 Google 客户端独立生成结果，检查后将结果导入运行。无需购买 API 即可结合 Codex 自动执行与 Google 人工交接。全自动订阅双模型仍受 Google 接入方式限制。未来如出现明确授权的接入，沿统一接口实现。

## Antigravity 主控反向协作补充 · 2026-11-23

- **Codex CLI 路径与隔离修正**：在 Antigravity 终端环境中，ChatGPT.app 内置的 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`（`0.171.0-alpha.13`，`Logged in using ChatGPT`）默认不在 shell PATH 中；`CodexBackend` 现按 PATH 优先并在 macOS 上回退发现该官方内置路径。同时补充 `-c project_doc_max_bytes=0`，避免 Codex 在 `data/<run-id>/provider/<node>` 中向上读取宿主仓库 `AGENTS.md` 触发 macOS `Operation not permitted` 错误并防止宿主规则泄漏到隔离任务。
- **合规反向协作架构（方案 A）**：复核 Google Antigravity Additional Terms of Service、FAQ、Headless CLI、ACP 与 Python SDK 文档，确认第三方应用以个人订阅封装 `agy -p` / ACP 或运行无人值守后台轮询均不符合条款，且官方 Python SDK 程序化认证需单独的 `GEMINI_API_KEY` / `VERTEX_API_KEY` / GCP ADC（与个人订阅额度不互通）。与之相对，在**官方 Antigravity 客户端的用户主动会话中**将 AIsys 作为本地工具调用（通过 `aisys exchange` CLI 或工作区级插件 `.agents/plugins/aisys-bridge/` 提供的本地 stdio MCP 服务 `aisys mcp`），由 Antigravity 原生执行所有 Google/Gemini 角色（并可通过独立 Subagent 隔离角色上下文）、AIsys 负责 LangGraph 状态机、SQLite 持久化、Seatbelt 沙箱校验和 Codex CLI 自动执行，不接触 Google OAuth 状态且零额外 API 费用。详细对比与协议规范见 [Antigravity 集成设计](antigravity-integration.md)。

## 编排与实施选择

PyPI 发布的稳定版为 LangGraph 1.2.24、SQLite checkpointer 3.1.3。已在 Python 3.14.6 的开发环境和 Python 3.12.13 的独立安装环境验证运行。LangGraph 的 SQLite 快照存储图状态，外部 CLI 和文件写入另用执行记录去重，人工交接使用 checkpoint interrupt。阶段一完成后，按 Codex 实跑、人工节点与恢复、并行候选与确定性验证、本地终端界面的顺序推进。 [LangGraph 发布](https://pypi.org/project/langgraph/) · [SQLite 发布](https://pypi.org/project/langgraph-checkpoint-sqlite/) · [持久化](https://docs.langchain.com/oss/python/langgraph/persistence) · [中断](https://docs.langchain.com/oss/python/langgraph/interrupts)
