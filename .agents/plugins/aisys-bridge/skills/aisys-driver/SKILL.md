---
name: aisys-driver
description: >-
  Drive AIsys 5-Stage Dual-Model Co-Creation sessions (Gemini/Antigravity + Codex)
  with independent proposals, cross-critique, non-overlapping task division,
  user decision/fusion, collaborative execution, mutual cross-review, and
  structured Markdown study guides. Use when the user asks to start or continue
  a dual-model task or learning session in AIsys.
---

# AIsys 5-Stage Dual-Model Driver Runbook

Coordinate **Gemini (native in Antigravity)** and **Codex (via AIsys)** cleanly
with zero extra API cost and strict mechanical anti-overlap guarantees.

## Primary Workflow: 5-Stage Co-Creation (`aisys collab`)

Use `aisys_collab_*` MCP tools when available, or invoke `.venv/bin/aisys collab ...` directly via `run_command`:

1. **Create or Resume Session**:
   - New session: `aisys collab create --mode dev|learn --task "<user goal>" [--project /path/to/repo] --no-codex`
   - Resume existing web-created session: `aisys collab list` → `aisys collab next <session_id>`
2. **Stage 1 — Independent Initial Proposals (Strict Isolation)**:
   - Write Gemini's initial proposal BEFORE reading Codex's Stage 1 output!
   - Submit via `aisys collab submit <id> --action propose --stdin --no-codex`
     with JSON `{"summary", "approach", "strengths", "risks"}`.
   - Run `aisys collab run-codex <id>` so Codex produces its independent Stage 1
     proposal and automatically advances to Stage 2 Codex critique.
3. **Stage 2 — Cross-Critique, Upgraded Plan & Non-Overlapping Division**:
   - Inspect `aisys collab next <id>` to read Codex's Stage 1 proposal.
   - Produce Gemini's `critique_of_peer`, `adopted_from_peer`, refined `summary`,
     `approach`, and a **strictly non-overlapping** `subtasks[]` table:
     - Each subtask has `id`, `title`, `owner` (`"gemini"` or `"codex"`),
       `objective`, `owned_paths`, `depends_on`, `acceptance`.
     - **Hard rule**: Different owners MUST have disjoint `owned_paths`
       (code files in `dev` mode; `sections/01_*.md`, `sections/02_*.md`, ... in
       `learn` mode). Dependencies must be acyclic.
   - Submit via `aisys collab submit <id> --action critique --stdin --no-codex`
     and ensure Codex Stage 2 is complete (`aisys collab run-codex <id>`).
4. **Stage 3 — STOP for User Decision**:
   - Never decide for the user! Present both upgraded plans and their verified
     non-overlapping division tables (and note the live side-by-side Web Studio
     at `http://127.0.0.1:8765` if running).
   - Ask the user to choose: **Adopt Gemini**, **Adopt Codex**, or **Fuse & Tweak**
     (with optional tweak instructions).
   - If the user replies in chat, call `aisys collab decide <id> --choice ...`;
     if they clicked in the Web Studio, simply inspect `aisys collab next <id>`.
5. **Stage 4 — Non-Overlapping Collaborative Execution**:
   - If `stage == "merging"`, produce the fused conflict-free plan incorporating
     `decision.fusion_notes` and submit `--action merge`.
   - Loop until `stage != "executing"`:
     - Run `aisys collab run-codex <id>` to execute ready Codex subtasks (each
       locked strictly to its `owned_paths`).
     - For each `execute_subtask` in `next_action.pending_gemini`, generate
       complete UTF-8 contents ONLY for `owned_paths` and submit via
       `aisys collab submit <id> --action execute_subtask --stdin --no-codex`.
6. **Stage 5 — Mutual Cross-Review & Delivery**:
   - Codex automatically audits all Gemini-owned subtasks (`aisys collab run-codex <id>`).
   - Gemini independently audits all Codex-owned subtasks (prefer spawning a
     fresh read-only `research` subagent fed ONLY the `review_peer` prompt!)
     and submits `--action review_peer` (`verdict: "pass"|"fail"`, `summary`,
     `findings`, `fix_requests`).
   - If either side flags defects, loop back through Stage 4 fixes for only the
     flagged subtasks, then re-review.
   - When `stage == "completed"`:
     - In `learn` mode: present the compiled Markdown study guide path
       (`data/sessions/<id>/study-guide.md`) and summary.
     - In `dev` mode: present `aisys collab diff <id>` and isolated workspace path.

## Legacy YAML Workflows (`aisys exchange`)

For custom YAML workflow presets (`dual_auto`, `software`, `research`, etc.),
`aisys exchange start/pending/submit/resume/inspect` remains fully supported.
Never auto-approve `kind == "approval"` gates without explicit user confirmation.
