# AIsys

AIsys 是一套 macOS 本地多智能体工作流平台。支持通过根目录 `AIsys.command` 一键启动本地 Web 工作台（默认 `http://127.0.0.1:8765`），也保留完整 CLI、Textual 终端工作台与 MCP 接口；全部入口共享同一个 LangGraph 编排内核、YAML 声明式配置与 SQLite 持久化状态。

**接入现状** · Codex 已真实接通（支持 PATH 优先及 macOS ChatGPT.app 内置 CLI 自动发现）。Google 个人订阅禁止由第三方应用直接封装 `agy -p` 或无人值守轮询；现已通过 **Antigravity 主控 + 统一任务交换协议** 实现合规双模型协作，脱离 Antigravity 单独使用 Web/CLI/工作台时，Google 节点自动暂停并支持在 Web 交接面板或 CLI 中提交核阅结果。 [接入调查](docs/discovery.md) · [Antigravity 集成](docs/antigravity-integration.md)

## 一键启动 Web 工作台（推荐）

在 Finder 中直接双击项目根目录的 **`AIsys.command`**（或在终端运行 `./AIsys.command`）：

1. 自动定位项目目录并检查 `.venv` 与依赖（已安装时不重复安装）。
2. 检查本地是否已有已验证的 AIsys 实例在运行：若已运行直接打开浏览器，不重复启动；若端口被其他程序占用则**不终止该进程**并自动回退到空闲端口（或提示冲突）。
3. 后台独立启动仅绑定 `127.0.0.1` 的 FastAPI 服务（默认 `http://127.0.0.1:8765`，可通过 `AISYS_PORT` 或 Web 设置更改），通过 `/api/health` 健康检查后自动打开浏览器。
4. 关闭浏览器标签页或启动终端不会中断正在执行的工作流；可在 Web 左下角点击「停止服务」或运行 `aisys stop` 优雅停止。

Web 工作台包含 **总览 Dashboard**（区分已安装/已认证/可调用/正在执行/受限/不可用/未验证的后端状态）、**任务中心**（SSE 实时监控、审批与 Google 交接、代码 Diff、队列与控制）、**可视化工作流编辑器**（拖拽节点与连线、条件路由与循环配置、与 YAML 双向实时同步）、**Agent 管理**（单后端多角色与模板库）、**统一日志中心** 及 **深色/浅色主题**。

也可以在命令行直接管理 Web 服务：

```sh
aisys serve --open        # 启动并打开浏览器
aisys status              # 检查本地服务是否运行
aisys stop                # 优雅停止已验证的本地服务
```

## 安装与 CLI 启动

要求 macOS、Python 3.12 或更新版本及 Git。真实模型节点另需 PATH 上的 Codex CLI 和现有 ChatGPT 登录。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
aisys doctor
aisys ui
```

先用 `aisys run blank --task '检查本地状态持久化'` 试跑，无模型调用。用下列命令体验真实 Codex 写代码、Python 校验和审查。

```sh
aisys run local_coding --task '在 answer.py 中实现 fibonacci(n)，负数抛 ValueError'
aisys runs
aisys report RUN_ID
aisys diff RUN_ID
```

工作台的 **Workflow** 页可选择预设、编辑 YAML、验证节点关系并保存副本。填写任务后可直接启动。**Runs** 页轮询本地检查点和执行记录，显示当前节点、各角色状态、路由、变更路径及待处理审批。审批和交接在 CLI 中完成。长任务可用 `--task-file` 从 UTF-8 文件读入。

## 预设

| 预设 | 流程 |
| --- | --- |
| `dual_auto` | Antigravity/Google 规划 → Codex 实现 → 本地沙箱检查 → Antigravity/Google 独立审查；测试或审查失败自动路由返工（受 `max_visits` 与 `budget` 限制）。 |
| `software` | Google 规划交接 → Codex 实现 → 本地测试 → Google 审查交接。测试或审查失败时先审批再返工。 |
| `research` | Google 分析已有资料和研究方法 → Codex 编写验证代码 → 本地检查 → Google 审阅 → 报告。 |
| `independent` | Codex 与 Google 分支独立分析，之后由另一逻辑角色比较。Google 分支经人工交接。 |
| `competitive` | 两个独立 Git 副本分别实现 `solve(text)`，用相同测试检查，再独立审查。内置题目是反转字符串。 |
| `blank` | 仅含本地报告节点，作为自定义工作流的起点。 |
| `local_coding` | 真实 Codex 实现和审查，中间执行本地语法检查。 |

```sh
aisys init software my-workflow.yaml
aisys validate my-workflow.yaml
aisys schema
aisys run my-workflow.yaml --category code --project /path/to/git/repo --task-file task.txt
```

`--project` 取指定仓库的已提交 HEAD，按 workspace 分别构建无远程、无共享 Git 元数据的独立副本。省略时创建空白 Git 工作区。实际修改进入 `data/<run-id>/workspaces/<space>`，由用户检查并选择如何合并。原仓库的未提交改动、未跟踪文件、符号链接、子模块和匹配敏感规则的路径不会进入快照。Git 归档属性也适用。

## Antigravity 主控协作（统一任务交换协议）

在官方 Antigravity 客户端中打开本仓库时，Antigravity 会自动发现工作区插件 `.agents/plugins/aisys-bridge/`（包含 `aisys-driver` Skill 与本地 stdio MCP 服务 `aisys mcp`），无需修改全局配置。也可直接通过结构化 CLI 驱动任意 YAML 预设：

```sh
aisys doctor
aisys exchange start dual_auto --project /path/to/git/repo --category code --task '实现需求...'
aisys exchange pending RUN_ID
aisys exchange submit RUN_ID --stdin < submission.json
aisys exchange resume RUN_ID
aisys exchange inspect RUN_ID
aisys exchange schema
```

对含 `kind: approval` 的人工审批节点，`exchange submit` 默认拒绝自动放行，必须经用户明确确认并传入 `--human-confirmed`（或在 JSON 中设置 `"human_confirmed": true`）。详细架构与合规边界见 [Antigravity 集成文档](docs/antigravity-integration.md)。

## 人工交接和恢复

当状态为 `waiting` 时：

```sh
aisys show RUN_ID
aisys prompt RUN_ID
# 在受支持的 Google 客户端处理该提示，把核阅后的结果存为 response.txt。
aisys resume RUN_ID --accept --file response.txt
# 审查交接请同时给出明确的 --verdict pass 或 --verdict fail。
```

同一并行组有多个待处理交接时，用 `--interrupt INTERRUPT_ID` 精确选择。人工审批节点用 `aisys resume RUN_ID --accept` 或 `--reject`。竞赛预设的人工代码候选使用 `--reply-json response.json`，结构如下。

```json
{
  "summary": "解释实现和限制",
  "decision": "none",
  "files": [{
    "path": "solution.py",
    "content": "def solve(text):\n    return text[::-1]\n"
  }]
}
```

`aisys pause RUN_ID` 在当前节点或整个并行批次结束后暂停。`aisys resume RUN_ID` 续行。 `aisys cancel RUN_ID` 请求在安全边界结束运行，并保留结果。关闭工作台后，已启动的后台执行仍可通过 `aisys show` 查看。CLI 中断后也可重新打开同一个运行。给 `--data /path/to/store` 指定其他本地存储时，把它放在子命令前。

## 调整 YAML

- `agents` 定义逻辑角色、后端、角色指令、可选模型名、精确可写路径。`reuse_session: true` 允许同一 Codex 节点在后续访问中续接本运行内的成功会话，默认使用临时会话。
- `nodes` 支持 `agent`、`check`、`approval`、`route`、`parallel` 和 `report`。`instruction` 是节点指令。`files`、`shared_files` 和 `inputs` 分别声明本 workspace 文件、其他 workspace 文件和前置结果。长期项目说明可写入 YAML 的 `project_notes`。每个节点只携带明确选择的输入。
- `next` 为默认成功路径，`routes` 按顺序匹配 `category`、`status` 或 `verdict`。`when.result` 可引用某个节点的最新结果。失败且没有匹配规则时，流程结束。并行分支使用不同的 `space`，所有分支结束后汇合。
- `budget` 控制总节点步数、模型尝试数、并发、上下文字符数和自工作区创建完成起的总耗时。总耗时包含人工等待。`max_visits` 限制循环，`timeout` 限制节点尝试，`retries` 对超时和限流做有限重试。`model_calls` 是本地尝试计数。实际用量计数来自 CLI。
- `check.argv` 由配置作者指定，使用 argv 执行。用 `python` 可运行基础 Python。输出和退出码进入记录。应按实际项目把预设的语法检查替换成具备业务断言的验收测试。检查读取工作区和系统运行时，写入专用临时子目录，网络受 macOS Seatbelt 限制。
- Google 不可用时默认返回 `unavailable`。配置 `on_unavailable: handoff` 可选择人工交接，并可为不可用状态编辑路由。当前没有付费 API 后端或自动回退。

## 架构、数据和边界

| 职责 | 模块 |
| --- | --- |
| Workflow 与 Agent | `config.py` 校验配置（支持 `presets/` 与 `data/config/workflows/` 统一解析），`services.py` 提供统一业务服务层与 Agent 库 |
| Web 与启动器 | `web_server.py`（FastAPI 仅绑定 `127.0.0.1` + CSRF/Origin/Host 防护 + SSE）、`web/` 零构建工作台与可视化编辑器、`AIsys.command` 一键启动 |
| Orchestrator | `engine.py` 编译 LangGraph，负责条件路由、并行汇合、预算、人工中断和恢复 |
| Exchange & MCP | `protocol.py` 定义统一任务交换信封/提交/审计，`mcp_server.py` 提供零依赖本地 stdio MCP |
| Model Backend | `backends.py` 管理 Codex CLI（支持 macOS ChatGPT.app 内置回退与 `project_doc_max_bytes=0` 隔离）和 Google 可用性边界 |
| Tool | `workspace.py` 处理快照、上下文、受限编辑和本地检查，`process.py` 限制子进程时间及输出量 |
| State | `state.py` 冻结配置，保存 SQLite 检查点及外部执行记录，每个运行只允许一个驱动进程 |

模型通过 stdin 收到上下文，在独立的只读运行目录返回结构化结果。编辑角色提交完整 UTF-8 文本，程序只接受角色允许的路径。删除、符号链接穿越和凭据类路径被拒绝。运行目录和日志留在本地，凭据类文本做常见模式脱敏。提交前仍需检查项目文件和配置中的敏感数据。

已完成尝试在同一运行内复用。若进程在外部调用中意外终止，已预留但结果不明的尝试标为 `interrupted`，由配置决定是否进入人工审批或其他路径。批量写入中遇到正常 I/O 错误会尝试回退。进程被强制终止或系统掉电时，应检查隔离副本中的部分改动后再继续。SQLite 存储包含任务和结果，应保留在受控目录。

当前编辑粒度是小型文本文件，测试命令依赖 macOS 工具链且无网络。暂未提供图形节点编辑、自动合并候选、账号余额查询或完整重放已发生的外部副作用。模型接入和测试证据见 [验证记录](docs/validation.md)。

## 开发检查

```sh
python -m pytest -q
ruff check src tests
python -m pip check
python -m pip wheel --no-deps --wheel-dir data/wheels .
```
