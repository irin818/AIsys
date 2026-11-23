"""Zero-dependency local stdio MCP server exposing AIsys exchange workflows."""
import json
import re
import sys
from pathlib import Path
from typing import TextIO

from .backends import Edit, default_backends
from .config import available_workflow_names, configuration_text, parse_config
from .engine import Engine
from .process import redact
from .protocol import (
    TaskSubmission,
    build_exchange_snapshot,
    record_exchange_snapshot,
    submit_task,
)
from .state import Run
from .workspace import artifacts, git

SUPPORTED_PROTOCOLS = ("2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "aisys-bridge", "version": "0.1.0"}

TOOLS = [
    {
        "name": "aisys_doctor",
        "description": "Check local Codex and Google backend readiness without sending prompts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "aisys_list_presets",
        "description": "List all bundled AIsys YAML workflow presets.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "aisys_validate_workflow",
        "description": "Validate a preset name or YAML file path and return its node topology.",
        "inputSchema": {
            "type": "object",
            "properties": {"workflow": {"type": "string"}},
            "required": ["workflow"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_start_run",
        "description": (
            "Create and drive an AIsys workflow run until it pauses at a pending task "
            "or completes. Returns the unified ExchangeSnapshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow": {"type": "string", "description": "Preset name or YAML path"},
                "task": {"type": "string", "description": "Task objective"},
                "category": {"type": "string", "default": "general"},
                "project": {"type": "string", "description": "Optional Git repository path"},
                "data_dir": {"type": "string", "description": "Optional run store directory"},
            },
            "required": ["workflow", "task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_get_pending_tasks",
        "description": "Read pending TaskEnvelope items awaiting host execution or human approval.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "data_dir": {"type": "string"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_submit_task_result",
        "description": (
            "Submit a validated TaskSubmission for a pending handoff (or human-confirmed approval), "
            "advance the workflow, and return the next ExchangeSnapshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "data_dir": {"type": "string"},
                "interrupt_id": {"type": "string"},
                "task_id": {"type": "string"},
                "accept": {"type": "boolean"},
                "status": {"type": "string", "enum": ["completed", "rejected", "failed"]},
                "result": {"type": "string"},
                "verdict": {"type": "string", "enum": ["pass", "fail", "none"]},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
                "review_feedback": {"type": "string"},
                "error": {"type": "string"},
                "human_confirmed": {"type": "boolean", "default": False},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_resume_run",
        "description": "Resume a paused or crash-recoverable run without re-running ambiguous attempts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "data_dir": {"type": "string"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_inspect_run",
        "description": "Inspect the full ExchangeSnapshot for a run (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "data_dir": {"type": "string"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_get_diff",
        "description": "Inspect the Git diff and changed paths in a run's isolated workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "space": {"type": "string", "default": "main"},
                "data_dir": {"type": "string"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "aisys_get_report",
        "description": "Render the Markdown summary report for a run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "data_dir": {"type": "string"},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
]


class AIsysMCPServer:
    def __init__(self, default_data: Path, backends=None):
        self.default_data = default_data.resolve()
        self.backends = backends

    def _data_dir(self, args: dict) -> Path:
        raw = args.get("data_dir")
        return Path(raw).expanduser().resolve() if raw else self.default_data

    def _engine(self, run: Run) -> Engine:
        return Engine(run, self.backends)

    def _call_tool(self, name: str, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        match name:
            case "aisys_doctor":
                providers = self.backends if self.backends is not None else default_backends()
                return {
                    provider: {
                        "status": health.status,
                        "error": health.error,
                    }
                    for provider, backend in providers.items()
                    for health in [backend.health()]
                }
            case "aisys_list_presets":
                return {"presets": available_workflow_names(self._data_dir(args))}
            case "aisys_validate_workflow":
                wf = parse_config(configuration_text(args["workflow"], data=self._data_dir(args)))
                return {
                    "name": wf.name,
                    "start": wf.start,
                    "agents": {k: v.model_dump(mode="json") for k, v in wf.agents.items()},
                    "nodes": {k: v.model_dump(mode="json") for k, v in wf.nodes.items()},
                }
            case "aisys_start_run":
                category = args.get("category", "general")
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", category):
                    raise ValueError("category must be a lowercase identifier")
                data_dir = self._data_dir(args)
                wf = parse_config(configuration_text(args["workflow"], data=data_dir))
                project = Path(args["project"]).expanduser() if args.get("project") else None
                run = Run.create(data_dir, wf, args["task"], category, project)
                engine = self._engine(run)
                view = engine.drive()
                snap = build_exchange_snapshot(engine, view)
                record_exchange_snapshot(engine, snap, event="started")
                return snap.model_dump(mode="json")
            case "aisys_get_pending_tasks":
                run = Run.open(self._data_dir(args), args["run_id"])
                snap = build_exchange_snapshot(self._engine(run))
                return {
                    "run_id": snap.run_id,
                    "status": snap.status,
                    "node": snap.node,
                    "pending_tasks": [t.model_dump(mode="json") for t in snap.pending_tasks],
                }
            case "aisys_submit_task_result":
                run = Run.open(self._data_dir(args), args["run_id"])
                engine = self._engine(run)
                files = [Edit.model_validate(item) for item in args.get("files", [])]
                submission = TaskSubmission(
                    task_id=args.get("task_id"),
                    interrupt_id=args.get("interrupt_id"),
                    accept=args.get("accept"),
                    status=args.get("status", "completed"),
                    result=args.get("result", ""),
                    verdict=args.get("verdict", "none"),
                    files=files,
                    review_feedback=args.get("review_feedback", ""),
                    error=args.get("error", ""),
                    human_confirmed=bool(args.get("human_confirmed", False)),
                )
                snap = submit_task(engine, submission, interrupt_id=args.get("interrupt_id"))
                return snap.model_dump(mode="json")
            case "aisys_resume_run":
                run = Run.open(self._data_dir(args), args["run_id"])
                engine = self._engine(run)
                view = engine.drive()
                snap = build_exchange_snapshot(engine, view)
                record_exchange_snapshot(engine, snap, event="resumed")
                return snap.model_dump(mode="json")
            case "aisys_inspect_run":
                run = Run.open(self._data_dir(args), args["run_id"])
                return build_exchange_snapshot(self._engine(run)).model_dump(mode="json")
            case "aisys_get_diff":
                run = Run.open(self._data_dir(args), args["run_id"])
                space = args.get("space", "main")
                if space not in {node.space for node in run.workflow.nodes.values()}:
                    raise ValueError("unknown workspace")
                workspace = run.workspace(space)
                return {
                    "run_id": run.id,
                    "space": space,
                    "workspace": str(workspace),
                    "changed_paths": artifacts(workspace),
                    "diff": redact(git(workspace, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--")),
                }
            case "aisys_get_report":
                from .cli import render_report

                run = Run.open(self._data_dir(args), args["run_id"])
                return {"run_id": run.id, "report": redact(render_report(self._engine(run)))}
            case _:
                raise ValueError(f"unknown tool: {name}")

    def handle_message(self, message: dict) -> dict | None:
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        # JSON-RPC notifications have no id and expect no reply.
        if msg_id is None:
            return None

        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
            if method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments") or {}
                try:
                    payload = self._call_tool(tool_name, arguments)
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{
                                "type": "text",
                                "text": json.dumps(payload, ensure_ascii=False, indent=2),
                            }],
                            "isError": False,
                        },
                    }
                except (KeyError, OSError, ValueError) as error:
                    return {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": redact(str(error))}],
                            "isError": True,
                        },
                    }
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as error:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": redact(str(error))},
            }


def serve_stdio(default_data: Path, stdin: TextIO | None = None,
                stdout: TextIO | None = None, backends=None) -> int:
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout
    server = AIsysMCPServer(default_data, backends=backends)
    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC message must be an object")
        except ValueError as error:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {error}"},
            }
            out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            out_stream.flush()
            continue
        response = server.handle_message(message)
        if response is not None:
            out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            out_stream.flush()
    return 0
