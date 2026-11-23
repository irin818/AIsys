# AIsys Explicit Command Trigger Protocol (`/dc`)

Trigger the `dc` dual full-power skill **ONLY** when the user explicitly starts a message with one of these commands:

- `/dc <task>` — Auto-routed dual full-power collaboration (Gemini Host + Codex Sub-Agent)
- `/dc:dev <task>` — Full-power dual engineering mode (Git Worktree isolation + Diff Gate + native `codex review`)
- `/dc:learn <topic>` — Full-power dual learning & research mode (both models with live web search + runnable verified code → structured Markdown study guide)
- `/dc:review [target]` — Instant dual full-power code review (`codex_review` + Gemini independent audit)
- `/dc:fast <task>` — Express capability-routed dual full-power execution (skip Stage 2 debate, parallel execution + cross-review)

Strict rules:
1. **Do NOT trigger** on ordinary natural-language prompts that do not contain `/dc` or `/dc:*`.
2. When triggered, IMMEDIATELY read and follow the `dc` skill (`skills/dc/SKILL.md`).
3. **Always keep BOTH models at 100% full power**: Gemini keeps all native Antigravity tools; Codex ALWAYS runs via `aisys-dual_aisys_codex` MCP tools with full shell, full repository visibility, live web search (`web_search: true`), isolated Git Worktree (`sandbox: "workspace-write"`), multi-turn `codex_reply` (`resume`), and native `codex_review`.
4. **Always route smartly first (`dual_route`)** and **mechanically enforce zero overlap** (`dual_check_division` before parallel execution + `codex_merge` Diff Gate before integrating Codex changes).
