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

## 一键启动与统一 Web 工作台验证 · 2026-11-23

在独立隔离数据目录 `data/web-e2e/` 与自动化测试夹具中完成全部 10 项 Web 与启动器验证：

| 测试项 | 验证方式与真实观测证据 | 结果 |
| --- | --- | --- |
| Test 1 & 2：一键启动、重复启动与外来端口保护 | 真实执行 `./AIsys.command`（含空格路径 `aisys data with spaces`）：自动启动 Uvicorn 并通过 `/api/health` 校验；二次执行检测已运行实例且 PID 不变；显式端口被非 AIsys socket 占用时报错且不终止该 socket；默认端口被占用时自动切至空闲端口；`aisys stop` 优雅停止并清理 `server.json`。 | **通过** |
| Test 3 & 4：Agent 与 Workflow 配置持久化及冲突校验 | `tests/test_web.py` 验证 Agent/Workflow CRUD、乐观锁 `expected_revision`（过期返回 HTTP 409）、非法 YAML/路径校验、跨服务实例重启后持久读取。 | **通过** |
| Test 5 & 6：Web 提交真实 Codex 任务 + SSE 实时监控 | 通过 `./AIsys.command` 启动的真实 `http://127.0.0.1:8765` 提交 `local_coding` 任务（Run `e07226a8-a2e0-40b0-b551-dc8d44d67cce`）：真实 Codex `implement`（`01a20901-eba7-7f80-91f0-53a1da9fa8cb`, `9.19s`, `14505`/`200` tokens）生成 `answer.py` → Seatbelt 语法检查（`0.059s`）→ 真实 Codex `review`（`01a20902-0fa9-7172-9100-0a38545cf3d9`, `5.886s`, `verdict: pass`）；SSE 实时收到 `connected` / `runs_changed` / `run_update`；`/api/runs/{id}/diff` 返回含 `def is_palindrome` 的新增文件 diff。 | **通过** |
| Test 7 & 8：审批、Google 交接返工、取消、关机确认与安全防护 | 验证缺失 `X-AIsys-Token` / 跨站 `Origin` / `Sec-Fetch-Site` / 非法 `Host` 均返回 `403`；敏感路径拒绝 `400`；未认证 Codex 显示 `installed`（非 `callable`）并拦截误启动；审批未传 `human_confirmed` 拒绝 `400`，确认后推进；活跃任务存在时关机返回 `409`，确认后安全停止。 | **通过** |
| Test 9 & 10：兼容性回归与 Web ↔ CLI 状态一致性 | `python -m pytest -q`（34 项全过）、`ruff check src tests`、`compileall`、`pip check`、`pip wheel`、`node --check`（`app.js`/`graph.js`）与 68 个 DOM ID 静态检查全部通过；Web 修改保存的工作流与真实 Run `e07226a8` 在 `aisys validate` / `aisys show --json` 中读取状态完全一致。 | **通过** |

## 极简 5 步双模型协作室验证 · 2026-11-24

彻底移除复杂多页 IDE 与节点拖拽编辑器后，在隔离目录 `data/collab-e2e/` 与自动化测试中验证了完整 5 步双模型协作闭环：

| 测试项 | 验证方式与真实观测证据 | 结果 |
| --- | --- | --- |
| 真实 5 步双模型学习闭环（含融合微调与互审返工） | Session `5a8f4636-41e4-4f20-a149-76e564e8d48e`（主题：Python 装饰器与 `functools.wraps`，`learn` 模式）：① Gemini 与真实 Codex (`input_tokens=14567`, `output_tokens=1623`) 各自独立提案 → ② 双方交叉互审并给出无重叠分工表（`division_check.valid=True`）→ ③ 融合微调锁定 2 个互斥章节（`T1`: Gemini `sections/01_mental_model.md`，`T2`: Codex `sections/02_examples_pitfalls.md`）→ ④ 各自在硬锁定路径内完成交付（真实 Codex 生成 4,669 字符带 `assert` 的示例与踩坑题）→ ⑤ 真实 Codex 在第 1 轮互审中真实检出 `T1` 遗漏闭包与近似展开 3 项细节并打回 → 系统聚合全部 3 条反馈仅令 `T1` 返工（保留 `T2` 已完成）→ 第 2 轮复审收口并自动生成 7,979 字符的结构化 `study-guide.md`（含完整两轮勘误记录）。 | **通过（真机）** |
| 机械防重叠校验与写路径硬锁 | `tests/test_web.py` 验证跨模型共享路径（`跨模型所有权冲突`）、同模型并行写同一路径、循环依赖均在拍板/融合前硬拦截；执行期 Gemini 试图写入 Codex 的 `owned_paths` 返回 HTTP `400`。 | **通过** |
| 一键启动、重复启动与外来端口保护 | 真实执行 `./AIsys.command`（含空格路径 `aisys studio spaces`）：健康检查通过、二次执行复用同一 PID、显式外来占用端口报错且不杀外来进程、默认端口占用自动回退空闲端口、`aisys stop` 优雅停止。 | **通过（真机）** |
| 安全防护、兼容性回归与打包 | `pytest -q`（32 项全过）、`ruff check src tests`、`compileall`、`pip check`、`pip wheel`（验证仅打包极简 `index.html`/`styles.css`/`app.js`，无残留 `graph.js`/`services.py`）、`node --check`、51 个 DOM ID 完整性校验与 CSRF/Origin/Host 拦截全部通过。 | **通过** |
| 真实后台流式遥测与全局 Antigravity 一键配置 | 隔离目录 `data/telemetry-smoke/` 实测真实 Codex CLI（Session `c63ca7ee`，PID `23958`，thread `01a20947`，`11.16s`，`14551`/`302` tokens）：逐行流式捕获 `process.spawned` → `codex.thread.started` → `codex.item.completed` → `codex.turn.completed` 并持久化至 `events.jsonl` 与 SSE；`GetAllPlugins` 确认 `~/.gemini/config/plugins/aisys-bridge` 在全局（无工作区）与 AIsys 工作区均正常加载且无冲突，`run-mcp.sh` 经全局软链在 `/tmp` 启动并暴露全部 16 个 MCP 工具（含 6 个 `aisys_collab_*`）。 | **通过（真机）** |

## 仍受限制的验证与边界

- **内置浏览器截图渲染检查未完成**：尝试调用内置 `webview_browser` 对 `http://127.0.0.1:8765` 截图检查时，宿主环境返回 `ISOLATED_WORLD_UNAVAILABLE` 未能获取渲染快照；已完成 JS 语法检查、51 个 DOM ID 完整性校验及全量 HTTP/SSE/真实 Codex 流式端到端验证，建议你在 macOS 本地双击 `AIsys.command` 在 Safari/Chrome 中直观体验黑绿极光美学与实时遥测舱。
- **第三方后台无交互自动调用 Google**：Google 个人订阅条款禁止由 AIsys 从外部封装 `agy -p` 或无人值守轮询；已通过全局 Antigravity 插件预配置将触发成本降至极简 `/aisys` 一键联动。
- **自动化测试区分**：`python -m pytest -q`（32 项）中的模型节点使用 `Stub` / `StubCodex`，`AIsys.command` / Uvicorn / Seatbelt / Git / SIGTERM / MCP stdio / `run_process` 实时流式子进程均为真机进程；上表与前节 E2E 验证均为真实 Codex CLI 与真实双模型执行记录。 [集成架构](antigravity-integration.md)
