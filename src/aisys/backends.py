"""Subscription CLI adapter and a provider-independent handoff contract."""
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import Field

from .config import Agent, Model, Status, Verdict
from .process import ProcessResult, cli_env, redact, run_process
from .workspace import write_atomic

BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")

REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "decision": {"type": "string", "enum": ["pass", "fail", "none"]},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    "required": ["summary", "decision", "files"],
}
GOOGLE_REASON = (
    "Google personal plans do not support wrapping Antigravity CLI in a third-party orchestrator, "
    "and Gemini CLI consumer sign-in has been retired. Run Google nodes via Antigravity host "
    "orchestration (aisys exchange) or manual handoff. See docs/discovery.md."
)
USAGE_FIELDS = {
    "input_tokens", "output_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "reasoning_output_tokens",
}


class Edit(Model):
    path: str
    content: str


class Reply(Model):
    summary: str = Field(min_length=1, max_length=32_000)
    decision: Verdict
    files: list[Edit] = Field(max_length=32)


class BackendResult(Model):
    status: Status
    reply: Reply | None = None
    error: str = ""
    duration: float = 0
    session_id: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    exit_code: int | None = None


class Backend(Protocol):
    name: str
    handoff_reason: str | None

    def health(self) -> BackendResult: ...

    def run(self, *, prompt: str, agent: Agent, cwd: Path, timeout: float,
            session_id: str | None = None) -> BackendResult: ...


class GoogleBackend:
    name = "google"
    handoff_reason = GOOGLE_REASON

    def health(self) -> BackendResult:
        return BackendResult(status="unavailable", error=GOOGLE_REASON)

    def run(self, **kwargs) -> BackendResult:
        return self.health()


def codex_executable() -> str | None:
    return shutil.which("codex") or (
        shutil.which(str(BUNDLED_CODEX)) if sys.platform == "darwin" else None
    )


class CodexBackend:
    name = "codex"
    handoff_reason = None

    def health(self) -> BackendResult:
        exe = codex_executable()
        if not exe:
            return BackendResult(status="unavailable", error="Codex CLI is not on PATH")
        result = run_process([exe, "login", "status"], Path.home(), cli_env(), 5)
        if result.issue:
            return BackendResult(status=result.issue, error="Could not check Codex login")
        if result.code != 0 or "logged in using chatgpt" not in (
            result.stdout + result.stderr
        ).lower():
            return BackendResult(status="auth", error="Codex requires an existing ChatGPT login")
        return BackendResult(status="ok")

    def _invoke(
        self,
        *,
        prompt: str,
        schema: dict,
        cwd: Path,
        timeout: float,
        model: str | None = None,
        reuse_session: bool = False,
        session_id: str | None = None,
    ) -> ProcessResult | BackendResult:
        start = time.monotonic()
        ready = self.health()
        if ready.status != "ok":
            return ready
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            return BackendResult(status="timeout", error="Codex login check used the time limit")
        exe = codex_executable()
        if not exe:
            return BackendResult(status="unavailable", error="Codex CLI is not on PATH")
        schema_path = cwd / "reply-schema.json"
        write_atomic(schema_path, json.dumps(schema).encode())
        cmd = [exe, "exec"]
        if session_id:
            cmd.append("resume")
        cmd.extend([
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json",
            "--output-schema", str(schema_path),
            "-c", 'model_provider="openai"',
            "-c", 'forced_login_method="chatgpt"',
            "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"',
            "-c", 'mcp_servers={}',
            "-c", 'agents.enabled=false',
            "-c", 'skills.bundled.enabled=false',
            "-c", 'project_doc_max_bytes=0',
            "-c", 'default_permissions="aisys"',
            "-c", 'permissions.aisys.filesystem={":minimal"="read"}',
            "--disable", "shell_tool", "--disable", "unified_exec",
            "--disable", "plugins", "--disable", "remote_plugin",
            "--disable", "apps", "--disable", "hooks",
            "--disable", "memories", "--disable", "request_permissions_tool",
        ])
        if model:
            cmd.extend(["--model", model])
        if not reuse_session:
            cmd.append("--ephemeral")
        if session_id:
            cmd.append(str(UUID(session_id)))
        return run_process([*cmd, "-"], cwd, cli_env(), remaining, prompt)

    def run(self, *, prompt: str, agent: Agent, cwd: Path, timeout: float,
            session_id: str | None = None) -> BackendResult:
        raw = self._invoke(
            prompt=prompt, schema=REPLY_SCHEMA, cwd=cwd, timeout=timeout,
            model=agent.model, reuse_session=agent.reuse_session, session_id=session_id,
        )
        if isinstance(raw, BackendResult):
            return raw
        return parse_events(raw)

    def run_json(self, *, prompt: str, schema: dict, cwd: Path, timeout: float,
                 model: str | None = None) -> tuple[BackendResult, dict | None]:
        raw = self._invoke(prompt=prompt, schema=schema, cwd=cwd, timeout=timeout, model=model)
        if isinstance(raw, BackendResult):
            return raw, None
        return parse_json_events(raw)


def _extract_codex_message(result: ProcessResult) -> tuple[BackendResult | None, str | None, dict]:
    common = {"duration": result.duration, "exit_code": result.code}
    if result.issue:
        return BackendResult(status=result.issue, error=f"Codex process: {result.issue}", **common), None, common
    errors = [result.stderr]
    session_id = None
    final = None
    usage = {}
    completed = False
    unexpected_tool = False
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            try:
                session_id = str(UUID(str(event.get("thread_id"))))
            except ValueError:
                pass
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                final = item["text"]
            if item.get("type") in {
                "command_execution", "file_change", "mcp_tool_call", "web_search",
                "collab_tool_call", "dynamic_tool_call",
            }:
                unexpected_tool = True
        elif event_type == "turn.completed":
            completed = True
            counters = event.get("usage")
            if isinstance(counters, dict):
                usage = {k: v for k, v in counters.items()
                         if k in USAGE_FIELDS and type(v) is int and v >= 0}
        elif event_type in {"error", "turn.failed"}:
            errors.append(json.dumps(event))
    meta = {**common, "session_id": session_id, "usage": usage}
    if result.code != 0 or not completed:
        text = "\n".join(errors).lower()
        if re.search(r"rate.limit|usage limit|quota|too many requests|\b429\b", text):
            status = "rate_limit"
        elif re.search(r"unauthoriz|authentication|sign in|not logged in|\b401\b", text):
            status = "auth"
        elif result.code == 0:
            status = "invalid"
        else:
            status = "failed"
        return BackendResult(
            status=status,
            error=f"Codex run did not complete ({status}, exit {result.code})",
            **meta,
        ), None, meta
    if unexpected_tool:
        return BackendResult(status="invalid", error="Codex reported a disallowed tool event", **common), None, meta
    return None, final, meta


def parse_json_events(result: ProcessResult) -> tuple[BackendResult, dict | None]:
    failure, final, meta = _extract_codex_message(result)
    if failure is not None:
        return failure, None
    try:
        parsed = json.loads(final or "")
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
    except ValueError:
        return BackendResult(status="invalid", error="Codex response failed JSON parsing", **meta), None
    return BackendResult(status="ok", **meta), parsed


def parse_events(result: ProcessResult) -> BackendResult:
    failure, final, meta = _extract_codex_message(result)
    if failure is not None:
        return failure
    try:
        reply = Reply.model_validate_json(final or "")
        reply.summary = redact(reply.summary)
    except ValueError:
        return BackendResult(status="invalid", error="Codex response failed the reply schema", **meta)
    return BackendResult(status="ok", reply=reply, **meta)


def default_backends() -> dict[str, Backend]:
    return {"codex": CodexBackend(), "google": GoogleBackend()}
