# 验证记录 · 2026-11-23

## 本机真实调用

| 验证项 | 命令与证据 | 结果 |
| --- | --- | --- |
| Codex CLI 独立调用 | [调查中的命令](discovery.md#codex) | ChatGPT 登录，退出 0，返回期望文本和 JSONL，用量字段真实可见。 |
| Codex 会话续接 | `CodexBackend.run(..., session_id=first.session_id)`，实际子命令为 `codex exec resume <ID> -`，带结构化响应、沙箱和工具限制参数 | 首轮和续接均退出 0，返回相同会话 ID 和各自的期望文本。 |
| 真实代码交付 | `aisys --data data/validation run local_coding --task 'In answer.py implement fibonacci(n) with iteration. For n less than zero raise ValueError. fibonacci(0)=0 and fibonacci(1)=1. Do not print anything at import.'` | 运行 `9d2197dc-5e9d-42ec-b8f3-6f7c2c119c91`，两次 Codex 执行和一次本地语法检查都退出 0，审查为 pass，生成 `answer.py`。首次试跑发现 macOS 沙箱需要给基础运行时读取权限，调整后重跑成功。 |
| 人工接力、失败修复和跨进程恢复 | `aisys --data data/validation run data/validation-inputs/repair-baseline.yaml --project data/validation-inputs/bug-fixture --task 'Repair add(a, b) in solution.py so it adds its arguments and the existing acceptance check passes.'`，各次用新的 `aisys resume` 调用继续 | 运行 `3391f773-b0e2-4d6f-933b-23536fb623ef`。人工规划样例交接，断言失败退出 1，人工批准，Codex 执行退出 0，将 `a+b+1` 改为 `a+b`，相同断言退出 0。导入人工审查样例，运行完成。另一次早期接力因 Codex 超时终止，保留失败记录。 |
| SpireLab 试运行 | `aisys --data data/validation run data/validation-inputs/mod-shape.yaml --project <SpireLab Git 仓库> --task 'Read the C# SDK and project layout in the committed snapshot. Summarize what was verified. Inspect metadata only.'` | 运行 `92d8cf29-d8db-4662-957a-6cdf77f07d3c`。提交 `df44602e9861b738bbf759b71c84adc7f27899cd` 的独立 Git 副本。本地布局检查和只读 Codex 检查均退出 0。源仓库仍处于相同提交且工作区干净。范围为 SDK 和项目结构，Mod 构建和游戏运行未验证。 |

真实调用的适配器参数见 [实现](../src/aisys/backends.py)。运行详情可在本机用 `aisys --data data/validation show RUN_ID --json` 查阅。样例输入、检查点和报告保存在被忽略的 `data/`，如需复制验证，可用安装后的预设或自动化测试重建夹具。

## 自动化覆盖

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | Python 3.14.6 和 3.12.13 均为 22 项通过。3.14 下 LangSmith 传递依赖有弃用提示。 |
| `ruff check src tests`、`python -m compileall -q src` | 通过。 |
| `python -m pip wheel --no-deps --wheel-dir data/wheels .` | 生成可安装 wheel。在新的 Python 3.12 环境安装并启动 `blank`，流程完成。 |
| `python -m pip check` | 两套 Python 环境均通过。 |

`python -m pytest -q` 覆盖 YAML 和路径校验、条件路由、两个同时执行的替身模型分支、工作区隔离、竞赛中独立测试和候选筛选、人工确认、暂停、恢复、取消、访问和模型尝试上限、超时/限流/认证/CLI 缺失/异常结构、预留尝试去重、真实进程收到 SIGTERM 后恢复，以及工作台编辑和启动。确定性检查和 Git 操作在本机实际执行，测试中的模型结果由替身生成。

macOS 沙箱测试核对了本地 Python 可运行，工作区外的样例文件读取、工作区源码写入和本地端口绑定均受限。检查子进程只继承专门构造的环境。

## Antigravity 主控真实双模型端到端验证 · 2026-11-23

在官方 Antigravity 会话内，以临时隔离目录 `data/e2e-validation/`（未改动任何既有项目或游戏 Mod 仓库）完成 6 项真实端到端验证，全部调用真实 ChatGPT 登录态下的 Codex CLI（`0.171.0-alpha.13`）与真实 Antigravity（含独立 Subagent 审查）：

| 测试项 | Run ID / 夹具 | 真实执行链路与观测证据 | 结果 |
| --- | --- | --- | --- |
| 测试一～三：Antigravity → AIsys → Codex → Antigravity | `aef65477-438f-4e63-9e0f-7f00b9a4042b`（`e2e_dual_loop.yaml` + `temp-project@82a9d0c`） | Antigravity 发起并提交 `plan` → AIsys 自动触发真实 Codex (`01a208d9-a027-7fb1-8de2-cadf0ce3ee7c`, `17.058s`, exit 0, `input_tokens=44374`, `output_tokens=365`) 重写 `solution.py` → macOS Seatbelt 断言校验通过（`0.058s`, exit 0） → 独立隔离的 Antigravity `research` Subagent (`f8a8d153-50e8-4eab-bd7a-994ca10e5d72`) 仅基于 `TaskEnvelope` 审查并给出 `verdict: pass` → 运行 `complete`，源夹具仓库保持干净。 | **通过** |
| 测试四：审查返工 | `5e5721e9-5e6b-4228-ac27-729898126a09`（`e2e_rework.yaml` + `rework-project@049890a`） | 首轮 Codex (`01a208da-ed7b-7311-9a71-64b53b0ec982`, `7.91s`, exit 0) 修复 `discount()` 算式并通过基础烟测 → Antigravity 审查检出缺失 3 项输入校验并返回 `verdict: fail` + 具体 `review_feedback` → AIsys 自动路由回 `implement` → 次轮 Codex (`01a208db-3535-7de3-9e71-bb79de4f473a`, `11.345s`, exit 0) 根据 `Review feedback:` 补齐 `bool`/负价/百分比范围校验 → 沙箱复测通过 → Antigravity 复审 `verdict: pass` → 实测全部边界异常通过。 | **通过** |
| 测试五：状态恢复 | `20bcc4bf-72c0-463e-b354-6b4f4f8c0a38`（`e2e_recovery.yaml`） | 真实子进程执行 `guarded_step` 写入 1 行日志进入 `in_progress` 后发送 `SIGTERM`（exit `130`）→ `exchange inspect` 显示 `recoverable` → `exchange resume` 将已预留步骤标为 `interrupted` 且 **未重复执行**（日志行数严格保持为 `1`），自动路由至 `inspect_recovery` 审批节点 → 未带 `human_confirmed` 的提交被拒绝（exit `2`）→ 带 `--human-confirmed` 后正常完成。 | **通过** |
| 测试六：配置切换（零 Python 改动） | `da4a85c2-e9b8-43ab-87ea-948dc8068db6`（`e2e_swapped_roles.yaml`） | 仅修改 YAML 互换角色与顺序：真实 Codex 先行规划 (`01a208dc-a3d8-7bd1-a6b1-c8cef57ffb4e`, `12.918s`) → Antigravity 通过 `scoped_write` 提交 `solution.py` → 沙箱测试通过 → 真实 Codex 审查 (`01a208dd-203d-76f0-8ac7-3d58a5861a6a`, `7.991s`) **真实检出 Kelvin 符号 `U+212A` 大小写边界缺陷**并返回 `verdict: fail` → AIsys 路由回 Antigravity 修复 → 沙箱复测通过 → Codex 复审 (`01a208dd-8075-7e70-94e2-0501782b42ff`, `7.973s`) 返回 `verdict: pass` 并交付。 | **通过** |
| 工作区插件与 Stdio MCP 握手 | `.agents/plugins/aisys-bridge/` | Antigravity 语言服务 `GetAllPlugins` 确认自动发现 `aisys-bridge`（工作区级、`disabled: false`，含 `aisys-driver` Skill 与 `orchestrator` MCP 配置）；`pytest` 中真实子进程通过 `run-mcp.sh` 完成 `initialize` + `tools/call(aisys_list_presets)` 握手。 | **通过** |

验证过程中首次真实调用 Codex 时还暴露并修复了两个仅真机可见的问题：Antigravity shell 下 PATH 缺失 ChatGPT.app 内置 `codex` 路径，以及 Codex 在子目录向上扫描宿主 `AGENTS.md` 触发 `os error 1`（通过 `-c project_doc_max_bytes=0` 修复）。

## 仍受限制的验证与边界

- **第三方后台无交互自动调用 Google**：Google 个人订阅条款禁止由 AIsys 从外部封装 `agy -p` 或无人值守轮询；离开官方 Antigravity 会话单独在终端运行 `aisys` 时，Google 节点仍保持人工交接。
- **新会话内实时 MCP 工具调用**：本会话启动时工作区插件尚未存在，MCP 工具调用的实时协议握手已通过 `run-mcp.sh` 真实 stdio 子进程验证，Antigravity 客户端内 MCP 工具列表注入将在新开或重载会话时生效；当前及所有会话均可通过 `aisys exchange` CLI 立即可用。
- **自动化测试区分**：`python -m pytest -q`（含 `tests/test_exchange.py`）使用 `Stub` / `StubCodex` 验证协议状态机、护栏与 MCP 逻辑；上表 6 项 E2E 验证为真实双模型与真实 Codex CLI 执行记录。 [集成架构](antigravity-integration.md)
