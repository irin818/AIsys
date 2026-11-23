"""A local terminal workbench for YAML authoring and checkpoint monitoring."""
import re
import shlex
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from .config import configuration_text, parse_config, preset_names
from .engine import Engine
from .state import Run, list_runs
from .workspace import artifacts


class Workbench(App):
    TITLE = "AIsys"
    BINDINGS = [("q", "quit", "Quit")]
    CSS = """
    Screen { background: #101711; color: #e5ede3; }
    Header, Footer { background: #18251c; }
    TabPane { padding: 1 2; }
    Label { color: #b4c8b4; margin-top: 1; }
    Input, Select { background: #17211a; margin-right: 1; }
    .row { height: auto; }
    .row > Input { width: 1fr; min-width: 14; }
    .row Button { margin-right: 1; width: auto; min-width: 8; }
    #yaml { height: 14; border: solid #48614e; }
    #topology, #details { padding: 1 1; border-left: solid #6e9370; margin-top: 1; }
    """

    def __init__(self, data: Path, initial_run: str | None = None):
        super().__init__()
        self.data = data.resolve()
        self.run_id = initial_run
        self.run_options = []
        self.drivers = []

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="runs" if self.run_id else "workflow"):
            with TabPane("Workflow", id="workflow"), VerticalScroll():
                yield Label("Preset")
                yield Select([(name, name) for name in preset_names()], value="blank",
                             allow_blank=False, id="presets")
                yield Label("Custom YAML path")
                with Horizontal(classes="row"):
                    yield Input(placeholder="my-workflow.yaml", id="config-path")
                    yield Button("Load", id="load")
                    yield Button("Save copy", id="save")
                yield Label("YAML")
                yield TextArea(configuration_text("blank"), id="yaml", show_line_numbers=True)
                yield Button("Validate & show graph", id="validate")
                yield Static(id="topology", markup=False)
                yield Label("Task")
                yield Input(placeholder="What should the workflow do?", id="task")
                yield Label("Project and category")
                with Horizontal(classes="row"):
                    yield Input(placeholder="Optional Git repository", id="project")
                    yield Input(value="general", id="category")
                yield Button("Start run", variant="success", id="start")
            with TabPane("Runs", id="runs"), VerticalScroll():
                yield Label("Run")
                yield Select([], prompt="Select a run", id="run-select")
                with Horizontal(classes="row"):
                    yield Button("Pause at boundary", id="pause")
                    yield Button("Cancel at boundary", id="cancel")
                yield Static("Choose a run to see its checkpoint.", id="details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.validate_yaml()
        self.refresh_runs()
        self.set_interval(1, self.refresh_runs)

    def refresh_runs(self) -> None:
        self.drivers[:] = [driver for driver in self.drivers if driver.poll() is None]
        options = [(f"{run.workflow.name} · {run.id}", run.id) for run in list_runs(self.data)]
        select = self.query_one("#run-select", Select)
        if options != self.run_options:
            select.set_options(options)
            self.run_options = options
        if self.run_id in {run_id for _, run_id in options} and select.value != self.run_id:
            select.value = self.run_id
        details = self.query_one("#details", Static)
        if not self.run_id:
            return
        try:
            run = Run.open(self.data, self.run_id)
            view = Engine(run).snapshot()
            lines = [
                f"{view['workflow']} · {view['status']}", f"Run  {run.id}",
                f"Task  {view['task']}", f"Node  {view['node']}",
            ]
            if view["reason"]:
                lines.append(view["reason"])
            for item in view["interrupts"]:
                lines.extend(["", f"{item['kind']} · {item['node']} · {item['id']}",
                              item.get("reason") or "",
                              f"aisys --data {shlex.quote(str(self.data))} prompt {run.id} --interrupt {item['id']}"])
            if view["trace"]:
                lines.extend(["", "Routing"] + [
                    f"  {item['node']} → {item['next']} · {item['status']}"
                    for item in view["trace"][-20:]
                ])
            if view["events"]:
                lines.extend(["", "Latest attempts"] + [
                    f"  {item['node']} · {item['agent_role']} · {item['backend']} · "
                    f"visit {item['visit']} / attempt {item['attempt']} · "
                    f"{'running' if item['in_progress'] and view['active'] else item['status']}"
                    for item in view["events"][-20:]
                ])
            for name, result in view["results"].items():
                lines.extend(["", f"{name} · {result['status']} · {result['verdict']}",
                              result["output"][:1500], *result["errors"]])
            if view["status"] in {"complete", "failed", "waiting", "paused", "cancelled"}:
                lines.extend(["", "Workspaces"])
                for space in sorted({node.space for node in run.workflow.nodes.values()}):
                    workspace = run.workspace(space)
                    lines.append(f"  {workspace}")
                    lines.extend(f"    {name}" for name in artifacts(workspace)[:20])
            details.update(Text("\n".join(lines)))
        except (OSError, ValueError) as error:
            details.update(Text(str(error)))

    @on(Select.Changed, "#presets")
    def choose_preset(self, message: Select.Changed) -> None:
        if isinstance(message.value, str):
            self.query_one("#yaml", TextArea).load_text(configuration_text(message.value))
            self.validate_yaml()

    @on(Select.Changed, "#run-select")
    def choose_run(self, message: Select.Changed) -> None:
        if isinstance(message.value, str):
            self.run_id = message.value
            self.refresh_runs()

    def validate_yaml(self):
        config = parse_config(self.query_one("#yaml", TextArea).text)
        lines = [f"{config.name} · start at {config.start}"]
        for name, node in config.nodes.items():
            role = config.agents.get(node.agent)
            lines.append(f"{name} [{role.role} / {role.backend}]" if role else f"{name} [{node.kind}]")
            lines.append(f"  → {node.next}" + (f" via {', '.join(node.branches)}" if node.branches else ""))
            for route in node.routes:
                lines.append(f"  → {route.to} when {route.when.model_dump(exclude_none=True)}")
        self.query_one("#topology", Static).update(Text("\n".join(lines)))
        return config

    @on(Button.Pressed)
    def pressed(self, message: Button.Pressed) -> None:
        try:
            match message.button.id:
                case "validate":
                    self.validate_yaml()
                case "load" | "save":
                    filename = self.query_one("#config-path", Input).value.strip()
                    if not filename:
                        raise ValueError("Enter a YAML file path")
                    if message.button.id == "load":
                        content = configuration_text(filename)
                        parse_config(content)
                        self.query_one("#yaml", TextArea).load_text(content)
                        self.validate_yaml()
                    else:
                        self.validate_yaml()
                        with Path(filename).open("x", encoding="utf-8") as target:
                            target.write(self.query_one("#yaml", TextArea).text)
                        self.notify(f"Saved {filename}")
                case "start":
                    workflow = self.validate_yaml()
                    task = self.query_one("#task", Input).value
                    category = self.query_one("#category", Input).value.strip()
                    project = self.query_one("#project", Input).value.strip()
                    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", category):
                        raise ValueError("Category must be a lowercase identifier")
                    message.button.disabled = True
                    self.start_run(workflow, task, category, Path(project).expanduser() if project else None)
                case "pause" | "cancel":
                    if not self.run_id:
                        raise ValueError("Select a run first")
                    run = Run.open(self.data, self.run_id)
                    if Engine(run).snapshot()["status"] in {"complete", "failed", "cancelled"}:
                        raise ValueError("Run is already terminal")
                    run.request(message.button.id)
                    self.notify(f"Requested {message.button.id}")
                    self.refresh_runs()
        except (OSError, ValueError) as error:
            self.notify(str(error), severity="error")

    @work(thread=True, exit_on_error=False)
    def start_run(self, workflow, task, category, project) -> None:
        try:
            run = Run.create(self.data, workflow, task, category, project)
            driver = subprocess.Popen(
                [sys.executable, "-m", "aisys", "--data", str(self.data), "resume", run.id],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.call_from_thread(self.open_run, run.id, driver)
        except (OSError, ValueError) as error:
            self.call_from_thread(self.notify, str(error), severity="error")
        finally:
            self.call_from_thread(self.enable_start)

    def enable_start(self) -> None:
        self.query_one("#start", Button).disabled = False

    def open_run(self, run_id: str, driver: subprocess.Popen) -> None:
        self.run_id = run_id
        self.drivers.append(driver)
        self.query_one(TabbedContent).active = "runs"
        self.refresh_runs()
