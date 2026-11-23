"""Zero-dependency JSON-RPC 2.0 Stdio MCP server exposing full-power Codex Sub-Agent tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .codex import CodexManager, redact
from .router import analyze_and_route, validate_division

PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "codex_doctor",
        "description": "Inspect本机满血 Codex CLI 状态、版本、ChatGPT 认证及 Git 就绪情况。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "dual_route",
        "description": "根据任务类型智能分析 Gemini 与 Codex 各自优势，给出最优主/协分工与满血调度蓝图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "用户的开发任务或学习研学目标"},
                "mode": {"type": "string", "enum": ["auto", "dev", "learn", "review", "fast"], "default": "auto"},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dual_check_division",
        "description": "机械校验 Gemini 与 Codex 的分工表 subtasks[]：强制跨模型 owned_paths 绝对互斥、同波次无冲突、依赖无环，并输出并行 Waves。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "subtasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "owner": {"type": "string", "enum": ["gemini", "codex"]},
                            "title": {"type": "string"},
                            "owned_paths": {"type": "array", "items": {"type": "string"}},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "owner", "title", "owned_paths"],
                    },
                }
            },
            "required": ["subtasks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_spawn",
        "description": (
            "拉起一个全功能满血 Codex 子 Agent（默认 workspace-write + 真实 Git Worktree 物理隔离 + 完整终端测试 + live 联网搜索）。"
            "设 background=true 可与 Gemini 物理并行执行，随后用 codex_status(wait=true) 收网。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "给满血 Codex 子 Agent 的明确任务或提案指令"},
                "project_dir": {"type": "string", "description": "目标项目目录绝对路径（Git 仓库自动建隔离 Worktree）"},
                "sandbox": {"type": "string", "enum": ["workspace-write", "read-only"], "default": "workspace-write"},
                "owned_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Codex 允许修改的独占相对路径或目录前缀（如 ['src/core.py', 'tests/']），合流时强校验",
                },
                "isolate_worktree": {"type": "boolean", "default": True},
                "web_search": {"type": "boolean", "default": True},
                "model": {"type": "string", "description": "可选覆盖 Codex 模型名"},
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": "true 则异步后台并行启动，立即返回 job_id",
                },
                "timeout_s": {"type": "integer", "default": 360, "minimum": 30, "maximum": 1200},
                "role": {"type": "string", "default": "Codex Full-Power Sub-Agent"},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_reply",
        "description": "通过原生 `codex exec resume <thread_id>` 向同一个 Codex 子 Agent 继续发送反馈、互审辩论或返工要求（完整保留上下文与高缓存命中）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "prompt": {"type": "string"},
                "background": {"type": "boolean", "default": False},
                "timeout_s": {"type": "integer", "default": 360, "minimum": 30, "maximum": 1200},
                "web_search": {"type": "boolean", "default": True},
            },
            "required": ["job_id", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_status",
        "description": "查询后台 Codex 子 Agent 的实时状态、执行命令记录、变更文件、Token 统计；设 wait=true 可阻塞等待完成并拉取 diff。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait": {"type": "boolean", "default": False},
                "timeout_s": {"type": "integer", "default": 360, "minimum": 1, "maximum": 1200},
                "include_diff": {"type": "boolean", "default": True},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_review",
        "description": "调用 Codex 原生仓库级代码审查（`codex exec review`），直接对真实仓库的未提交更改、分支对比或指定提交执行满血深度审查。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["uncommitted", "base", "commit", "custom"],
                    "default": "uncommitted",
                },
                "target": {"type": "string", "description": "mode='base' 时的基准分支或 mode='commit' 时的提交 SHA"},
                "instructions": {"type": "string", "description": "mode='custom' 时的自定义审查焦点"},
                "model": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 240, "minimum": 30, "maximum": 600},
            },
            "required": ["project_dir"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_merge",
        "description": "运行机械防重叠 Diff 闸门：校验 Codex Worktree 的所有改动严格位于 owned_paths 内且不覆盖主工作区脏文件，通过后干净合入主分支。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "target_dir": {"type": "string", "description": "可选覆盖合并目标目录（默认原项目根目录）"},
                "owned_paths_override": {"type": "array", "items": {"type": "string"}},
                "dry_run": {"type": "boolean", "default": False},
                "cleanup_after": {"type": "boolean", "default": True},
                "allow_overwrite_dirty": {"type": "boolean", "default": False},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "codex_cleanup",
        "description": "列出、取消或清理 Codex 子 Agent 任务及其临时 Git Worktree。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "cancel", "cleanup"], "default": "list"},
                "job_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]


class StdioMCPServer:
    def __init__(self, data_dir: Path):
        self.manager = CodexManager(data_dir)

    def dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "codex_doctor":
            return self.manager.doctor()
        if name == "dual_route":
            return analyze_and_route(args["task"], mode=args.get("mode", "auto"))
        if name == "dual_check_division":
            return validate_division(args["subtasks"])
        if name == "codex_spawn":
            return self.manager.spawn(
                prompt=args["prompt"],
                project_dir=args.get("project_dir"),
                sandbox=args.get("sandbox", "workspace-write"),
                owned_paths=args.get("owned_paths"),
                isolate_worktree=bool(args.get("isolate_worktree", True)),
                web_search=bool(args.get("web_search", True)),
                model=args.get("model"),
                background=bool(args.get("background", False)),
                timeout_s=int(args.get("timeout_s", 360)),
                role=str(args.get("role", "Codex Full-Power Sub-Agent")),
            )
        if name == "codex_reply":
            return self.manager.reply(
                job_id=args["job_id"],
                prompt=args["prompt"],
                background=bool(args.get("background", False)),
                timeout_s=int(args.get("timeout_s", 360)),
                web_search=bool(args.get("web_search", True)),
            )
        if name == "codex_status":
            return self.manager.status(
                args["job_id"],
                wait=bool(args.get("wait", False)),
                timeout_s=int(args.get("timeout_s", 360)),
                include_diff=bool(args.get("include_diff", True)),
            )
        if name == "codex_review":
            return self.manager.review(
                project_dir=args["project_dir"],
                mode=args.get("mode", "uncommitted"),
                target=args.get("target"),
                instructions=args.get("instructions", ""),
                model=args.get("model"),
                timeout_s=int(args.get("timeout_s", 240)),
            )
        if name == "codex_merge":
            return self.manager.merge(
                job_id=args["job_id"],
                target_dir=args.get("target_dir"),
                owned_paths_override=args.get("owned_paths_override"),
                dry_run=bool(args.get("dry_run", False)),
                cleanup_after=bool(args.get("cleanup_after", True)),
                allow_overwrite_dirty=bool(args.get("allow_overwrite_dirty", False)),
            )
        if name == "codex_cleanup":
            action = args.get("action", "list")
            if action == "list":
                return {"jobs": self.manager.list_jobs()}
            if action == "cancel":
                return self.manager.cancel(args["job_id"])
            if action == "cleanup":
                return self.manager.cleanup(args["job_id"])
            raise ValueError(f"未知 action：{action}")
        raise ValueError(f"未知工具：{name}")

    def handle_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "notifications/initialized" or req_id is None:
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aisys-dual-codex", "version": __version__},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            try:
                result = self.dispatch_tool(tool_name, tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as err:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps({"error": redact(str(err))}, ensure_ascii=False)}
                        ],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def serve_stdio(self) -> None:
        """Serve newline-delimited JSON-RPC 2.0 cleanly over stdio."""
        for line in sys.stdin:
            text = line.strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            reply = self.handle_message(msg)
            if reply is not None:
                sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                sys.stdout.flush()
