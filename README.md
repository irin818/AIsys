# AIsys v2 · `/dc` 双满血协同原生插件

**单一入口（Antigravity）· 零 Web · 零 LangGraph · 零第三方运行时依赖 · \$0 额外 API 费用**

AIsys v2 是一个专为 macOS 设计的原生 Antigravity 全局插件（`aisys-dual`）：它将你本机已登录的官方 **OpenAI Codex CLI** 封装为 **Gemini（Antigravity 主控）** 的**全功能满血并行子 Agent**。

- **Gemini（主控）100% 满血**：保留 Antigravity 原生全仓读写、终端、联网、浏览器、生图与与你直接对话的能力。
- **Codex（子 Agent）100% 满血**：跑在真实项目的隔离 **Git Worktree** 中，全功能开放 `workspace-write` 读写、终端跑真测试、实时联网搜索（`web_search="live"`）、原生多轮记忆续接（`codex exec resume`）与原生仓库级审查（`codex exec review`）。
- **先智能调度再开工**：自动按任务特征分析 Gemini 与 Codex 比较优势，最优分配主/协与并行波次。
- **机械防重叠零冲突**：开工前 `dual_check_division` 校验路径互斥 → 并行期独立 Git Worktree 物理隔离 → 合流前 `codex_merge` Diff 闸门逐文件核验零越界。

> 旧版（LangGraph + FastAPI Web + Textual TUI）已完整保留在 Git 存档标签 `archive/v1-web-langgraph` 中。

---

## 极简专用指令（不误触自然语言）

在 Antigravity 任意项目对话中输入：

| 指令 | 作用 |
| --- | --- |
| `/dc <任务或主题>` | 智能判定开发/研学并最优调度 Gemini 与 Codex，跑完双满血 5 步协同 |
| `/dc:dev <需求>` | 强制工程模式：智能路由 → 提案互审 → 你拍板 → 双 Worktree 真并行实现 + 自测 → Diff 闸门合流 → 原生 `codex review` 互审 |
| `/dc:learn <主题>` | 强制深度研学模式：双模型开实时联网 + 可运行代码实测 → 输出经交叉勘误的结构化 Markdown 学习指南 |
| `/dc:review [分支或提交]` | 即时双满血代码审查（Codex 原生 `codex exec review` + Gemini 独立架构/安全审查） |
| `/dc:fast <任务>` | 极速直出模式：智能最优调度后跳过阶段 2 辩论与人工停顿，直接互斥并行执行 + 互审交付 |

---

## 安装与全局启用（一键软链，全项目通用）

```sh
# 1. 全局注册到 Antigravity 插件目录
ln -sfn "$(pwd)" ~/.gemini/config/plugins/aisys-dual

# 2. 自检本机满血 Codex CLI 与 Git 环境
PYTHONPATH=src python3 -m aisys doctor
```

插件通过 `mcp_config.json` 自动暴露 9 个纯标准库 MCP 工具（`aisys-dual_aisys_codex`）：
`codex_doctor` · `dual_route` · `dual_check_division` · `codex_spawn` · `codex_reply` · `codex_status` · `codex_review` · `codex_merge` · `codex_cleanup`。

---

## 本地测试

```sh
python3 -m pytest -q
ruff check src tests
```
