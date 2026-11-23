"""CLI entry point for AIsys v2 (MCP server, doctor, smart router, job management)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .codex import CodexManager
from .mcp_server import StdioMCPServer
from .router import analyze_and_route


def default_data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aisys", description="AIsys v2 Dual Full-Power Codex Bridge")
    p.add_argument("--data", type=Path, default=default_data_dir(), help="State/worktree directory")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("mcp", help="Run JSON-RPC 2.0 Stdio MCP server")
    sub.add_parser("doctor", help="Check full-power Codex CLI & Git health")

    rt = sub.add_parser("route", help="Analyze task and recommend optimal Gemini/Codex routing")
    rt.add_argument("task", help="Task description")
    rt.add_argument("--mode", choices=["auto", "dev", "learn", "review", "fast"], default="auto")

    sub.add_parser("jobs", help="List recent Codex sub-agent jobs")

    cl = sub.add_parser("cleanup", help="Clean up a finished Codex job and temporary Git worktree")
    cl.add_argument("job_id", help="Codex job ID")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "mcp":
        StdioMCPServer(args.data).serve_stdio()
        return 0
    if args.command == "doctor":
        info = CodexManager(args.data).doctor()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("ok") else 1
    if args.command == "route":
        print(json.dumps(analyze_and_route(args.task, mode=args.mode), ensure_ascii=False, indent=2))
        return 0
    if args.command == "jobs":
        print(json.dumps({"jobs": CodexManager(args.data).list_jobs()}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "cleanup":
        print(json.dumps(CodexManager(args.data).cleanup(args.job_id), ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
