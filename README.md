# AIsys

AIsys 是一套 macOS 本地智能体工作流。用 YAML 定义角色和条件路由，用 Codex CLI 调用已登录的 ChatGPT 账号，用 SQLite 保存每个运行。终端工作台支持编辑配置、启动任务和跟踪节点。

**接入现状** · Codex 已真实接通。Google AI 个人订阅的第三方自动调用受官方支持范围限制。含 Google 的预设会在交接节点暂停，由你在官方客户端生成结果、检查内容并导入。 [接入调查](docs/discovery.md)

## 安装和启动

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
| Workflow 与 Agent | `config.py` 校验配置，`presets/` 提供可复制的 YAML |
| Orchestrator | `engine.py` 编译 LangGraph，负责条件路由、并行汇合、预算、人工中断和恢复 |
| Model Backend | `backends.py` 管理 Codex CLI 和 Google 可用性，规范化结果、错误、退出码和用量 |
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
