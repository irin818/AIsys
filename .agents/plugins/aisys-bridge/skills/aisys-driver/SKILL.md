---
name: aisys-driver
description: >-
  Orchestrate local AIsys YAML workflows from inside Antigravity using the
  compliant Antigravity-master architecture and Unified Task Exchange Protocol.
  Use when the user asks to run, review, resume, or coordinate a dual-model
  (Antigravity/Gemini + Codex) workflow in AIsys.
---

# AIsys Dual-Model Driver Runbook

Drive AIsys YAML workflows directly from Antigravity so Gemini (native inside
Antigravity) and Codex (via AIsys's `CodexBackend`) collaborate over a shared,
SQLite-persisted workflow graph with Git snapshot isolation and macOS Seatbelt
checks.

## Compliance & Safety Boundaries

1. **Interactive user-initiated runs only**: Never set up unattended background
   polling loops or cron jobs to drain consumer quota.
2. **Never bypass Google client boundaries**: Never wrap `agy -p` inside AIsys
   or touch OAuth tokens/credentials. All Google roles execute natively inside
   this official Antigravity session.
3. **Never auto-approve human gates**: When a pending task has
   `kind == "approval"`, STOP immediately, present the prompt, workspace diff,
   and test/review context to the user, and only submit `human_confirmed: true`
   after explicit user approval or rejection.
4. **Never mutate AIsys workspaces out-of-band**: For `scoped_write` roles,
   return complete UTF-8 file contents in `TaskSubmission.files` so AIsys
   validates `write_paths`, redacts secrets, and applies edits atomically.

## Interface Priority

- If the `aisys-bridge` MCP server tools (`aisys_start_run`,
  `aisys_submit_task_result`, `aisys_inspect_run`, etc.) are available in your
  tool list, prefer them.
- Otherwise, invoke the identical CLI interface via `run_command` using
  `.venv/bin/aisys` (or `aisys` on PATH):
  - `aisys doctor`
  - `aisys exchange start <preset-or-yaml> --task "..." [--project /path/to/repo] [--category ...]`
  - `aisys exchange pending <run_id>`
  - `aisys exchange submit <run_id> --stdin [--interrupt <id>]`
  - `aisys exchange resume <run_id>`
  - `aisys exchange inspect <run_id>`
  - `aisys diff <run_id> [--space <space>]`
  - `aisys report <run_id>`

## Orchestration Loop

1. **Preflight**: Run `aisys doctor` (or `aisys_doctor`) to confirm `codex: ok`.
2. **Start**: Call `aisys exchange start <workflow> --task "..."` (with
   `--project` if targeting a Git repository; AIsys snapshots committed `HEAD`
   into an isolated workspace under `data/<run-id>/workspaces/`).
3. **Inspect the returned `ExchangeSnapshot`**:
   - If `status == "complete"`: go to Step 6.
   - If `status == "failed"` or `status == "cancelled"`: inspect `reason`,
     `completed_tasks`, and `events`, then report honestly to the user.
   - If `status == "recoverable"` or `status == "paused"`: inspect `events` and
     `aisys diff` first to verify whether an interrupted step left partial
     state, then call `aisys exchange resume <run_id>`.
   - If `status == "waiting"`: examine `pending_tasks`.
4. **Process `pending_tasks`**:
   - For any task with `kind == "approval"`: STOP and ask the user for an
     explicit approve/reject decision. Only submit with `human_confirmed: true`
     after the user replies.
   - For each task with `kind == "handoff"` (Google/Gemini role):
     - **Role isolation**: Execute strictly from the task's `objective`,
       `context.role_instructions`, `context.node_instruction`, `context.files`,
       `context.upstream_results`, and `permissions`. When executing distinct
       roles in the same run (e.g., `Planner` vs `Independent Reviewer`, or
       parallel candidates), prefer spawning a fresh `research` or `self`
       subagent (`invoke_subagent`) carrying ONLY the `TaskEnvelope` data so
       roles cannot leak private scratchpads.
     - Check `permissions.mode`:
       - `read_only`: keep `files: []`.
       - `scoped_write`: provide complete UTF-8 contents only for paths listed
         in `permissions.write_paths`.
     - Check `permissions.requires_verdict`:
       - When reviewing (`requires_verdict: true`), ALWAYS set `verdict` to
         `"pass"` or `"fail"` and put concrete actionable findings in
         `review_feedback`.
     - Build a `TaskSubmission` JSON:
       ```json
       {
         "protocol_version": 1,
         "task_id": "<envelope.task_id>",
         "interrupt_id": "<envelope.interrupt_id>",
         "status": "completed",
         "result": "<concise summary of plan, implementation, or review>",
         "verdict": "pass | fail | none",
         "files": [],
         "review_feedback": "<specific actionable findings when reviewing>"
       }
       ```
     - Submit it with `aisys exchange submit <run_id> --stdin` (or
       `aisys_submit_task_result`). AIsys validates permissions, persists the
       result to SQLite, and automatically advances Codex nodes, sandboxed
       checks, conditional routes, and bounded rework loops until the next
       pending task or completion.
5. Repeat Steps 3–4 until the run reaches a terminal status or awaits human
   approval.
6. **Deliver**: Run `aisys report <run_id>` and `aisys diff <run_id>` (for each
   modified workspace) and present the final summary, diff, and isolated
   workspace path to the user.
