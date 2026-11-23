"""Private run metadata and an execution journal alongside graph checkpoints."""
import fcntl
import json
import shutil
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field

from .config import Model, Status, Verdict, Workflow
from .process import redact
from .workspace import prepare_spaces, write_atomic


class Result(Model):
    node: str
    backend: str
    agent_role: str
    task_id: str
    status: Status
    output: str = ""
    verdict: Verdict = "none"
    review_feedback: str = ""
    artifacts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration: float = 0
    session_id: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    exit_code: int | None = None
    space: str
    attempt: int = 1


class Run:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.id = self.root.name
        self.db = self.root / "state.sqlite"
        self.meta = json.loads((self.root / "run.json").read_text(encoding="utf-8"))
        self.workflow = Workflow.model_validate(self.meta["workflow"])

    @classmethod
    def create(cls, data: Path, workflow: Workflow, task: str, category: str, source: Path | None):
        if not task.strip() or len(task) > 12_000:
            raise ValueError("task must contain 1–12000 characters")
        root = data.resolve() / str(uuid4())
        root.mkdir(mode=0o700, parents=True)
        try:
            commit = prepare_spaces(root, {node.space for node in workflow.nodes.values()}, source)
            (root / "provider").mkdir(mode=0o700)
            database = root / "state.sqlite"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE control (
                        id INTEGER PRIMARY KEY CHECK(id = 1), value TEXT NOT NULL
                    );
                    INSERT INTO control VALUES (1, '');
                    CREATE TABLE effects (
                        node TEXT NOT NULL, visit INTEGER NOT NULL, attempt INTEGER NOT NULL,
                        model_call INTEGER NOT NULL, started REAL NOT NULL, data TEXT NOT NULL,
                        result TEXT, PRIMARY KEY(node, visit, attempt)
                    );
                """)
            database.chmod(0o600)
            write_atomic(root / "run.json", json.dumps({
                "workflow": workflow.model_dump(mode="json"),
                "task": redact(task),
                "task_id": str(uuid4()),
                "category": category,
                "created": time.time(),
                "source_commit": commit,
            }, ensure_ascii=False).encode())
        except Exception:
            shutil.rmtree(root)
            raise
        return cls(root)

    @classmethod
    def open(cls, data: Path, run_id: str):
        try:
            key = str(UUID(run_id, version=4))
        except ValueError as error:
            raise ValueError("expected a run UUID") from error
        if key != run_id or not (data / key / "run.json").is_file():
            raise ValueError("run does not exist")
        return cls(data / key)

    @property
    def deadline(self) -> float:
        return self.meta["created"] + self.workflow.budget.wall_seconds

    def workspace(self, space: str) -> Path:
        parent = self.root / "workspaces"
        path = parent / space
        if parent.is_symlink() or path.is_symlink() or not path.is_dir():
            raise ValueError("run workspace is absent or no longer isolated")
        return path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.db, timeout=10)) as db, db:
            yield db

    @contextmanager
    def exclusive(self):
        with self.root.joinpath("driver.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("run already has an active driver") from error
            yield

    def active(self) -> bool:
        try:
            with self.exclusive():
                return False
        except ValueError:
            return True

    def control(self) -> str:
        with self.connect() as db:
            return db.execute("SELECT value FROM control WHERE id=1").fetchone()[0]

    def request(self, value: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE control SET value=? WHERE id=1 AND value != 'cancel'", (value,))

    def begin_effect(self, visit: int, record: Result, model_call: bool) -> Result | None:
        key = (record.node, visit, record.attempt)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT result FROM effects WHERE node=? AND visit=? AND attempt=?", key,
            ).fetchone()
            if row is not None:
                if row[0] is not None:
                    return Result.model_validate_json(row[0])
                unknown = record.model_copy(update={
                    "status": "interrupted",
                    "errors": ["The previous process stopped after reserving this attempt. "
                               "Inspect the workspace before routing to another attempt."],
                })
                db.execute("UPDATE effects SET result=? WHERE node=? AND visit=? AND attempt=?",
                           (unknown.model_dump_json(), *key))
                return unknown
            used = db.execute("SELECT COALESCE(SUM(model_call), 0) FROM effects").fetchone()[0]
            if model_call and used >= self.workflow.budget.model_calls:
                return record.model_copy(update={"status": "budget", "errors": ["Model call limit reached"]})
            db.execute("INSERT INTO effects VALUES (?, ?, ?, ?, ?, ?, NULL)", (
                *key, model_call, time.time(), record.model_dump_json(),
            ))
            return None

    def finish_effect(self, visit: int, record: Result) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE effects SET result=? WHERE node=? AND visit=? AND attempt=?",
                (record.model_dump_json(), record.node, visit, record.attempt),
            )

    def events(self) -> list[dict]:
        with self.connect() as db:
            return [
                {**json.loads(result or initial), "visit": visit, "started": started,
                 "in_progress": result is None}
                for visit, started, initial, result in db.execute(
                    "SELECT visit, started, data, result FROM effects ORDER BY started"
                )
            ]


def list_runs(data: Path) -> list[Run]:
    runs = []
    if data.is_dir():
        for entry in data.iterdir():
            try:
                run = Run.open(data, entry.name)
                if run.db.is_file():
                    runs.append(run)
            except (ValueError, OSError):
                continue
    return sorted(runs, key=lambda run: run.meta["created"], reverse=True)
