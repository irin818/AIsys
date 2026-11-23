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

## 仍受限制的验证

本机没有可供测试的 Google CLI，消费者订阅接入此编排应用也不在 Google 官方支持范围内。**Google CLI 实测与真实双模型自动协作均未验证。** 人工样例仅证明状态、审批和交接协议可用。当前两个账号配合的支持路径是 Codex 自动执行、Google 客户端中独立操作、人工导入结果。 [官方依据](discovery.md#google)
