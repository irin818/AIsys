# AIsys v2 项目约定

- 本仓库是原生 Antigravity 全局插件（`aisys-dual`），将本机官方 Codex CLI 封装为 Gemini（Antigravity 主控）的满血并行子 Agent，零 Web、零 LangGraph、零第三方运行时依赖（纯 Python 3.12+ 标准库）。
- 始终保持双满血开放：Gemini 保留 Antigravity 全套原生工具；Codex 默认开放 `workspace-write` 隔离 Git Worktree、完整终端测试、`web_search="live"` 实时联网、`exec resume` 多轮续接与原生 `exec review`。
- 严格区分显式触发与日常对话：仅在用户输入 `/dc` 或 `/dc:*` 指令时激活双模型协作协议，不误触发普通自然语言消息。
- 物理防重叠红线：开工前经 `validate_division()` 校验互斥路径，合流前经 `merge_into_target()` Diff 闸门强制校验 `changed_files ⊆ owned_paths`，绝不越界覆盖主工作区未提交改动。
- 开发时运行 `python3 -m pytest -q`、`ruff check src tests` 与真实 MCP stdio / Git Worktree 集成测试；临时任务数据保留在已忽略的 `data/`，绝不读写或复制模型凭据。
