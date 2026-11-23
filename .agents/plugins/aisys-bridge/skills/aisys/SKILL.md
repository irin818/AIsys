---
name: aisys
description: >-
  One-key driver for the local AIsys 5-Stage Dual-Model Co-Creation Studio (Gemini in Antigravity + Codex CLI). Use immediately when the user types /aisys, asks to advance/continue AIsys, or wants Gemini and Codex to co-create a study guide or engineering task.
---

# AIsys One-Key Dual-Model Studio (`/aisys`)

Operate the local AIsys engine (current workspace if it contains `src/aisys`, otherwise `~/Documents/AIsys`, using `<repo>/data` state store) with minimum user friction.

## Fast CLI / MCP Helper

- Prefer MCP tools (`aisys_collab_create`, `aisys_collab_next`, `aisys_collab_run_codex`, `aisys_collab_submit_gemini`, `aisys_collab_decide`, `aisys_collab_study_guide`).
- CLI fallback (works from any workspace):
  ```bash
  AISYS_ROOT="${AISYS_ROOT:-$HOME/Documents/AIsys}"
  AISYS="$AISYS_ROOT/.venv/bin/python -m aisys --data $AISYS_ROOT/data"
  ```

## One-Key Execution Loop

1. **Ensure Web Studio is alive** (so the user sees live telemetry and can decide in Stage 3):
   - Check `curl -sf http://127.0.0.1:8765/api/health`.
   - If unreachable, start `$AISYS serve --open` as a background daemon and wait until `/api/health` returns `{"app":"aisys","status":"ok"}`.
2. **Inspect or create session**:
   - If the user supplied a new topic/task after `/aisys`, call `aisys_collab_create` (choose `mode="learn"` for learning/research/study guides, `mode="dev"` for code/repo tasks) and trigger Codex immediately.
   - Otherwise, list active sessions via `$AISYS collab list` and pick the newest non-completed/non-cancelled session.
3. **Run continuous stage loop**:
   - Call `aisys_collab_next(session_id)` (or `$AISYS collab next <id>`).
   - If `codex_tasks` has ready items and Codex is not already busy, trigger Codex (`aisys_collab_run_codex` or `$AISYS collab run-codex <id>`) in parallel with Gemini's work.
   - For each `gemini_tasks` item:
     - `propose`: Write Gemini's independent proposal (`summary`, `approach`, `strengths`, `risks`) and submit via `propose`.
     - `critique`: Read Codex's Stage 1 proposal carefully, critique it honestly, adopt its best parts, propose a strict **non-overlapping** `subtasks[]` table (disjoint `owned_paths` across owners and concurrent waves, acyclic `depends_on`), and submit via `critique`.
     - `merge`: Synthesize the user's `fusion_notes` with both models' refined plans into one conflict-free `subtasks[]` table and submit via `merge`.
     - `execute_subtask`: Produce complete, high-craft files strictly within `owned_paths` (never touch Codex's files) and submit via `execute_subtask`.
     - `review_peer`: Audit Codex's actual produced files critically; return `verdict="pass"` only when genuinely sound, or `verdict="fail"` with specific `fix_requests`.
   - Wait briefly if Codex is still finishing a paired step so you can immediately advance into Stage 2 or Stage 5 in the same turn.
4. **Stop cleanly at human checkpoints**:
   - **Stage 3 (`deciding`)**: Stop and tell the user both upgraded plans and the verified non-overlapping division are live at `http://127.0.0.1:8765` for their 1-click decision (Adopt Gemini / Adopt Codex / Fusion Tweak).
   - **Stage 5 (`completed`)**: Report the final summary, review highlights, and exact path/link to `study-guide.md` (learn mode) or `git diff` (dev mode).
