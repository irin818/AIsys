# AIsys One-Key Trigger Protocol

When the user sends `/aisys`, `aisys`, `推进`, `继续双模型`, `启动双模型`, or asks to run/continue an AIsys dual-model session (from any workspace):

1. Activate the `aisys` skill immediately without asking clarifying questions unless the user has no active session AND gave no topic.
2. Ensure the local AIsys Web Studio (`http://127.0.0.1:8765`) is running:
   - Check `curl -sf http://127.0.0.1:8765/api/health`.
   - If not running, resolve the AIsys repo root (current workspace if it contains `src/aisys`, otherwise `~/Documents/AIsys`) and start `<repo>/.venv/bin/python -m aisys --data <repo>/data serve --open` as a detached background daemon so the user immediately sees live telemetry in their browser.
3. Prefer MCP tools `aisys_collab_*` from `aisys-bridge_orchestrator`; if the current chat started before MCP reloaded, fall back seamlessly to `<repo>/.venv/bin/python -m aisys --data <repo>/data collab ...`.
4. Drive ALL ready Gemini stages continuously in one turn (Stage 1 proposal → wait for Codex → Stage 2 cross-critique & non-overlapping division → pause cleanly at Stage 3 for user decision; after user decides/fuses → drive Stage 4 Gemini subtasks + Codex in parallel → Stage 5 mutual review & rework → final delivery).
5. Never ask the user to paste JSON or run manual CLI commands. Always respect `owned_paths` write locks and never fabricate Codex outputs.
