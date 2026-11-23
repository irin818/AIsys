"""Command-line entry points for workflow configuration and run control."""
import argparse
import json
import os
import re
import signal
import sys
from pathlib import Path

from .backends import default_backends
from .config import Workflow, configuration_text, parse_config, preset_names
from .engine import Engine
from .process import PROCESS_INTERRUPTED, redact
from .state import Run, list_runs
from .workspace import artifacts, git, write_atomic


def read_input(path: Path, limit: int) -> str:
    with path.open(encoding="utf-8") as stream:
        return stream.read(limit + 1)


def render_report(engine: Engine) -> str:
    view = engine.snapshot()
    lines = [
        f"# {view['workflow']}", "",
        f"Run: {view['id']}", f"Task: {view['task_id']}", f"Status: {view['status']}", "",
        "## Task", "", view["task"], "",
    ]
    if view["reason"]:
        lines.extend([view["reason"], ""])
    if view["source_commit"]:
        lines.extend([f"Source commit: `{view['source_commit']}`", ""])
    lines.extend(["## Routing", ""])
    for event in view["trace"]:
        rule = event["rule"] if event["rule"] is not None else "default"
        lines.append(f"- {event['node']} → {event['next']} · {event['status']} / {event['verdict']} · {rule}")
    lines.extend(["", "## Latest node results", ""])
    for name, result in view["results"].items():
        lines.extend([
            f"### {name}", "",
            f"{result['agent_role']} · {result['backend']} · {result['status']} · {result['verdict']}", "",
            result["output"], *result["errors"], "",
        ])
    lines.extend(["## Workspaces", ""])
    for space in sorted({node.space for node in engine.workflow.nodes.values()}):
        workspace = engine.run.workspace(space)
        lines.extend([f"- `{workspace}`", *(f"  - `{name}`" for name in artifacts(workspace))])
    return "\n".join(lines) + "\n"


def print_status(view: dict) -> None:
    print(f"{view['id']}  {view['status']}  {view['node']}")
    if view["reason"]:
        print(view["reason"])
    for item in view["interrupts"]:
        print(f"  {item['kind']}: {item['node']}  {item['id']}")
        if item.get("reason"):
            print(f"  {item['reason']}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="aisys", description=__doc__)
    root.add_argument("--data", type=Path, default=Path("data"), help="run store (default: ./data)")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check provider readiness without submitting a prompt")
    sub.add_parser("presets", help="list bundled workflows")
    sub.add_parser("runs", help="list local runs")
    sub.add_parser("schema", help="print the workflow JSON Schema")
    template = sub.add_parser("init", help="copy a preset into an editable YAML file")
    template.add_argument("preset", choices=preset_names())
    template.add_argument("output", type=Path)
    validation = sub.add_parser("validate", help="validate and display the workflow graph")
    validation.add_argument("workflow")
    start = sub.add_parser("run", help="start a workflow from a preset or YAML path")
    start.add_argument("workflow")
    tasks = start.add_mutually_exclusive_group(required=True)
    tasks.add_argument("--task")
    tasks.add_argument("--task-file", type=Path)
    start.add_argument("--category", default="general")
    start.add_argument("--project", type=Path, help="optional Git repository; uses committed HEAD")
    start.add_argument("--prepare-only", action="store_true", help="create the run without driving it")
    for name, help_text in {
        "show": "inspect checkpoint, node results and execution attempts",
        "resume": "continue a run or answer a pending interrupt",
        "prompt": "display the input for a pending handoff or approval",
        "pause": "request a pause at the next node boundary",
        "cancel": "request cancellation at the next node boundary",
        "report": "render the run as a Markdown report",
        "diff": "display tracked changes in an isolated workspace",
    }.items():
        sub.add_parser(name, help=help_text).add_argument("run_id")
    sub.choices["show"].add_argument("--json", action="store_true")
    sub.choices["prompt"].add_argument("--interrupt")
    resume = sub.choices["resume"]
    resume.add_argument("--interrupt")
    decision = resume.add_mutually_exclusive_group()
    decision.add_argument("--accept", dest="accept", action="store_true", default=None)
    decision.add_argument("--reject", dest="accept", action="store_false")
    handoff = resume.add_mutually_exclusive_group()
    handoff.add_argument("--file", type=Path)
    handoff.add_argument("--reply-json", type=Path)
    resume.add_argument("--verdict", choices=["pass", "fail", "none"], default="none")
    sub.choices["report"].add_argument("--out", type=Path)
    sub.choices["diff"].add_argument("--space", default="main")
    monitor = sub.add_parser("ui", help="open the local terminal workbench")
    monitor.add_argument("--run")
    return root


def invoke(args) -> int:
    match args.command:
        case "doctor":
            failed = False
            for name, backend in default_backends().items():
                result = backend.health()
                print(f"{name}: {result.status} {result.error}".rstrip())
                failed |= name == "codex" and result.status != "ok"
            return int(failed)
        case "schema":
            print(json.dumps(Workflow.model_json_schema(), ensure_ascii=False, indent=2))
        case "presets":
            print("\n".join(preset_names()))
        case "init":
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(configuration_text(args.preset))
            print(args.output.resolve())
        case "validate":
            workflow = parse_config(configuration_text(args.workflow))
            print(f"{workflow.name} · start: {workflow.start}")
            for name, node in workflow.nodes.items():
                suffix = f" · {workflow.agents[node.agent].backend}" if node.agent else ""
                print(f"{name} [{node.kind}{suffix}] → {node.next}")
                for branch in node.branches:
                    print(f"  branch: {branch}")
                for route in node.routes:
                    print(f"  {route.when.model_dump(exclude_none=True)} → {route.to}")
        case "runs":
            for run in list_runs(args.data):
                view = Engine(run).snapshot()
                print(f"{run.id}  {view['status']:<11}  {run.workflow.name}")
        case "ui":
            from .ui import Workbench

            Workbench(args.data, args.run).run()
        case "run":
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", args.category):
                raise ValueError("category must be a lowercase identifier")
            workflow = parse_config(configuration_text(args.workflow))
            task = args.task if args.task is not None else read_input(args.task_file, 12_000)
            run = Run.create(args.data, workflow, task, args.category, args.project)
            print(f"Run {run.id}", flush=True)
            if not args.prepare_only:
                view = Engine(run).drive()
                print_status(view)
                return int(view["status"] in {"failed", "cancelled"})
        case _:
            run = Run.open(args.data, args.run_id)
            engine = Engine(run)
            match args.command:
                case "show":
                    view = engine.snapshot()
                    if args.json:
                        print(json.dumps(view, ensure_ascii=False, indent=2))
                    else:
                        print_status(view)
                        for result in view["results"].values():
                            print(f"  {result['node']}: {result['status']} · {result['agent_role']}")
                case "prompt":
                    pending = engine.snapshot()["interrupts"]
                    candidates = [item for item in pending if args.interrupt in {None, item["id"]}]
                    if len(candidates) != 1:
                        raise ValueError("select one pending interrupt with --interrupt")
                    print(candidates[0].get("prompt", "Run paused at a node boundary."))
                case "resume":
                    response = read_input(args.file, 12_000) if args.file else ""
                    view = engine.drive(interrupt_id=args.interrupt, accept=args.accept,
                                        response=response, verdict=args.verdict,
                                        reply_json=read_input(args.reply_json, 262_144)
                                        if args.reply_json else None)
                    print_status(view)
                    return int(view["status"] in {"failed", "cancelled"})
                case "pause" | "cancel":
                    view = engine.snapshot()
                    if view["status"] in {"complete", "failed", "cancelled"}:
                        raise ValueError("run is already terminal")
                    run.request(args.command)
                    print_status(engine.snapshot())
                case "report":
                    report = render_report(engine)
                    if args.out:
                        write_atomic(args.out, report.encode())
                        print(args.out.resolve())
                    else:
                        print(report, end="")
                case "diff":
                    if args.space not in {node.space for node in run.workflow.nodes.values()}:
                        raise ValueError("unknown workspace")
                    workspace = run.workspace(args.space)
                    print(git(workspace, "diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"))
                    print("Changed paths:", ", ".join(artifacts(workspace)) or "(none)")
                    print(f"Workspace: {workspace}")
    return 0


def main() -> int:
    os.umask(0o077)

    def stop(signum, frame):
        PROCESS_INTERRUPTED.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        return invoke(parser().parse_args())
    except KeyboardInterrupt:
        print("Interrupted. Use aisys show and aisys resume to inspect the checkpoint.",
              file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        print(f"aisys: {redact(str(error))}", file=sys.stderr)
        return 2
