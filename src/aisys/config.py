"""Load and validate declarative workflow definitions."""
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Id = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,47}$")]
Status = Literal[
    "ok", "failed", "timeout", "rate_limit", "auth", "unavailable", "invalid",
    "interrupted", "rejected", "budget", "cancelled",
]
Verdict = Literal["pass", "fail", "none"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Agent(Model):
    role: str = Field(min_length=1, max_length=100)
    backend: Literal["codex", "google"]
    instructions: str = Field(min_length=1, max_length=12_000)
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    write_paths: list[str] = Field(default_factory=list, max_length=32)
    reuse_session: bool = False
    on_unavailable: Literal["stop", "handoff"] = "stop"


class Match(Model):
    result: Id | None = None
    category: Id | None = None
    status: Status | None = None
    verdict: Verdict | None = None


class Route(Model):
    when: Match
    to: Id


class ContextFile(Model):
    space: Id
    path: str


class Node(Model):
    kind: Literal["agent", "check", "approval", "route", "parallel", "report"]
    agent: Id | None = None
    instruction: str = Field(default="", max_length=12_000)
    space: Id = "main"
    files: list[str] = Field(default_factory=list, max_length=32)
    shared_files: list[ContextFile] = Field(default_factory=list, max_length=32)
    inputs: list[Id] = Field(default_factory=list, max_length=32)
    argv: list[str] = Field(default_factory=list, max_length=32)
    branches: list[Id] = Field(default_factory=list, max_length=16)
    timeout: int = Field(default=120, ge=1, le=3600)
    retries: int = Field(default=0, ge=0, le=5)
    max_visits: int = Field(default=1, ge=1, le=30)
    next: Id = "end"
    routes: list[Route] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_kind(self):
        if (self.agent is not None) != (self.kind == "agent"):
            raise ValueError("only agent nodes require an agent")
        if bool(self.argv) != (self.kind == "check"):
            raise ValueError("only check nodes require argv")
        if any(not arg or "\x00" in arg for arg in self.argv):
            raise ValueError("argv must contain nonempty strings without NUL")
        if self.kind == "parallel":
            if len(self.branches) < 2 or len(set(self.branches)) != len(self.branches):
                raise ValueError("parallel requires at least two distinct branches")
        elif self.branches:
            raise ValueError("branches belong to parallel nodes")
        if any(all(value is None for value in (route.when.category, route.when.status, route.when.verdict))
               for route in self.routes):
            raise ValueError("use next for the default route")
        return self


class Budget(Model):
    steps: int = Field(default=24, ge=1, le=120)
    model_calls: int = Field(default=8, ge=0, le=100)
    parallelism: int = Field(default=2, ge=1, le=8)
    wall_seconds: int = Field(default=86_400, ge=1, le=2_592_000)
    context_chars: int = Field(default=16_000, ge=500, le=65_536)


class Workflow(Model):
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=100)
    start: Id
    agents: dict[Id, Agent] = Field(default_factory=dict, max_length=32)
    nodes: dict[Id, Node] = Field(min_length=1, max_length=80)
    project_notes: str = Field(default="", max_length=8_000)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def validate_topology(self):
        from .workspace import relative_file

        if "end" in self.nodes or self.start not in self.nodes:
            raise ValueError("start must name a node and end is reserved")
        children = set()
        spaces = {node.space for node in self.nodes.values()}
        for name, node in self.nodes.items():
            if node.agent and node.agent not in self.agents:
                raise ValueError(f"{name}: unknown agent")
            for path in node.files:
                relative_file(path)
            for item in node.shared_files:
                relative_file(item.path)
                if item.space not in spaces:
                    raise ValueError(f"{name}: unknown shared workspace")
            for route in node.routes:
                if route.when.result is not None and route.when.result not in self.nodes:
                    raise ValueError(f"{name}: unknown result for route")
            destinations = [node.next, *(route.to for route in node.routes)]
            if set(destinations + node.inputs + node.branches) - self.nodes.keys() - {"end"}:
                raise ValueError(f"{name}: unknown node reference")
            if "end" in node.inputs or "end" in node.branches:
                raise ValueError(f"{name}: end is only a route destination")
            if node.kind == "parallel":
                branches = [self.nodes[branch] for branch in node.branches]
                for branch in branches:
                    if branch.kind not in {"agent", "check", "report"} or branch.next != "end" or branch.routes:
                        raise ValueError(f"{name}: parallel branches are leaf agent, check or report nodes")
                if len({branch.space for branch in branches}) != len(branches):
                    raise ValueError(f"{name}: parallel branches must use distinct spaces")
                for branch in branches:
                    siblings = {sibling.space for sibling in branches if sibling != branch}
                    if set(branch.inputs) & set(node.branches) or (
                        {item.space for item in branch.shared_files} & siblings
                    ):
                        raise ValueError(f"{name}: parallel branches cannot read sibling results or spaces")
                children.update(node.branches)
        entries = {self.start} | {
            dest
            for node in self.nodes.values()
            for dest in [node.next, *(route.to for route in node.routes)]
        }
        if children & entries:
            raise ValueError("parallel branch nodes must be entered through a parallel node")
        for agent in self.agents.values():
            if agent.backend == "google" and agent.reuse_session:
                raise ValueError("the Google handoff does not manage provider sessions")
            for path in agent.write_paths:
                relative_file(path)
        return self


class UniqueLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("YAML aliases are disabled")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = super().construct_mapping(node, deep=deep)
        if len(result) != len(node.value):
            raise ValueError("duplicate YAML keys")
        return result


def preset_names() -> list[str]:
    return sorted(path.name.removesuffix(".yaml") for path in files("aisys.presets").iterdir()
                  if path.name.endswith(".yaml"))


def configuration_text(reference: str) -> str:
    if reference in preset_names():
        return files("aisys.presets").joinpath(f"{reference}.yaml").read_text(encoding="utf-8")
    with Path(reference).open(encoding="utf-8") as stream:
        return stream.read(262_145)


def parse_config(content: str) -> Workflow:
    if len(content) > 262_144:
        raise ValueError("workflow YAML exceeds 256 Ki characters")
    try:
        return Workflow.model_validate(yaml.load(content, Loader=UniqueLoader))
    except yaml.YAMLError as error:
        line = getattr(error, "problem_mark", None)
        suffix = f" on line {line.line + 1}" if line else ""
        raise ValueError(f"invalid YAML{suffix}") from error
