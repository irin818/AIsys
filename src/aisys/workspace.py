"""Independent Git snapshots, explicit context files and scoped text edits."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from .process import redact, run_process

DENIED = {
    ".git", ".codex", ".gemini", ".agents", ".mcp.json", ".ssh", ".aws", ".netrc",
    ".npmrc", ".pypirc", ".kube", "credentials", "auth.json", "secrets", "secret",
    "credentials.json", ".gitmodules", ".aisys-check",
}
DENIED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db"}
FILE_BYTES = 96_000
TOTAL_EDIT_BYTES = 256_000
GIT = "/usr/bin/git" if sys.platform == "darwin" else shutil.which("git")
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
GIT_ENV = {
    "PATH": TOOL_PATH,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C.UTF-8",
}
GOVERNANCE = {"agents.md", "project_charter.md", "architecture.md", "development_protocol.md"}


def relative_file(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if path.is_absolute() or str(path) != raw or not path.parts or len(raw) > 300:
        raise ValueError("expected a canonical relative file path")
    for part in path.parts:
        lowered = part.casefold()
        if (part in {".", ".."} or lowered in DENIED or lowered.startswith(".env")
                or any(ord(char) < 32 or ord(char) == 127 for char in part)
                or "\\" in part or ":" in part or PurePosixPath(part).suffix.casefold() in DENIED_SUFFIXES):
            raise ValueError("file path is disallowed")
    return path


def local_file(root: Path, raw: str) -> Path:
    path = root
    for part in relative_file(raw).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlinks are not allowed")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("file path leaves the workspace")
    return path


def git_args(*args: str) -> list[str]:
    if not GIT:
        raise ValueError("Git is not installed")
    return [GIT, "-c", "core.hooksPath=/dev/null", *args]


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        git_args(*args), cwd=cwd, env=GIT_ENV, capture_output=True, timeout=120,
        text=True, errors="replace",
    )
    if result.returncode:
        raise ValueError(f"git {args[0]} exited {result.returncode}")
    return result.stdout.rstrip("\n")


def prepare_spaces(root: Path, names: set[str], source: Path | None) -> str | None:
    archive = root / "snapshot.tar"
    commit = None
    if source:
        commit = git(source, "rev-parse", "--verify", "HEAD^{commit}")
        # A committed snapshot avoids worktree metadata and ignored or untracked files.
        with archive.open("wb") as stream:
            result = subprocess.run(
                git_args("archive", "--format=tar", commit), cwd=source,
                env=GIT_ENV, stdout=stream, stderr=subprocess.DEVNULL, timeout=120,
            )
        if result.returncode:
            raise ValueError("Git could not archive the specified project")
    try:
        for name in sorted(names):
            target = root / "workspaces" / name
            target.mkdir(parents=True)
            if commit:
                with tarfile.open(archive, "r:") as snapshot:
                    for entry in snapshot:
                        if not entry.isfile():
                            continue
                        try:
                            destination = local_file(target, entry.name)
                        except ValueError:
                            continue
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        snapshot.extract(entry, target, filter="data")
            git(target, "init", "--initial-branch=aisys", "--template=")
            git(target, "add", "--force", "--all")
            git(target, "-c", "user.name=AIsys Snapshot", "-c", "user.email=aisys@localhost",
                "commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "AIsys snapshot")
    finally:
        archive.unlink(missing_ok=True)
    return commit


def read_context(root: Path, raw: str, limit: int) -> str:
    path = local_file(root, raw)
    if not path.is_file():
        return "[file absent]"
    with path.open("rb") as stream:
        content = stream.read(min(FILE_BYTES, limit * 4) + 1)
    if b"\0" in content:
        raise ValueError("binary context files are unsupported")
    decoded = content.decode("utf-8", errors="replace")
    if len(decoded) > limit or len(content) > FILE_BYTES:
        return redact(decoded[:limit]) + "\n[truncated]"
    return redact(decoded)


def apply_edits(root: Path, changes, allowed: list[str]) -> list[str]:
    planned: dict[Path, tuple[str, bytes, bytes | None, int]] = {}
    total_bytes = 0
    for change in changes:
        raw = change.path
        path = local_file(root, raw)
        if raw not in allowed or path in planned or path.name.casefold() in GOVERNANCE:
            raise ValueError("edit path is outside the role allowlist or is protected")
        if path.exists() and (not path.is_file() or path.stat().st_size > FILE_BYTES):
            raise ValueError("existing edit destination is not a small regular file")
        content = change.content.encode("utf-8")
        total_bytes += len(content)
        if (len(content) > FILE_BYTES or total_bytes > TOTAL_EDIT_BYTES or b"\0" in content
                or redact(change.content) != change.content):
            raise ValueError("edit exceeds file limits or contains a credential-like value")
        before = path.read_bytes() if path.exists() else None
        if before is not None:
            before.decode("utf-8")
            if b"\0" in before or (before and not content):
                raise ValueError("binary files and deletion-like edits are disallowed")
        mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o644
        planned[path] = (raw, content, before, mode)

    applied = []
    try:
        for path, (_, content, before, mode) in planned.items():
            if content == before:
                continue
            write_atomic(path, content, mode)
            applied.append(path)
    except OSError:
        for path in reversed(applied):
            _, _, before, mode = planned[path]
            if before is None:
                path.unlink()
            else:
                write_atomic(path, before, mode)
        raise
    return [planned[path][0] for path in applied]


def write_atomic(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.chmod(staged, mode)
            staged.replace(path)
        finally:
            staged.unlink(missing_ok=True)


def run_check(argv: list[str], workspace: Path, timeout: float):
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("local checks require macOS Seatbelt")
    # Give the child its own writable temp, while protecting both Git metadata and source.
    scratch = workspace / ".aisys-check"
    if scratch.is_symlink():
        raise ValueError("check scratch path cannot be a symlink")
    scratch.mkdir(exist_ok=True)
    base_python = Path(sys._base_executable).resolve()
    executable = str(base_python) if argv[0] == "python" else argv[0]
    if not Path(executable).is_absolute():
        path = shutil.which(executable, path=TOOL_PATH)
        if not path:
            raise ValueError("configured check executable is not installed")
        executable = path
    executable = str(Path(executable).resolve())
    read_roots = [
        workspace, Path("/System/Library"), Path("/System/Cryptexes"),
        Path("/usr"), Path("/bin"), Path("/sbin"),
        Path("/opt/homebrew"), Path("/Library/Developer/CommandLineTools"), Path("/dev"),
    ]
    read_rules = " ".join(f"(subpath {json.dumps(str(path))})" for path in read_roots)
    rules = (
        "(version 1) (allow default) (deny network*) "
        "(deny file-read-data) (deny file-write*) (deny appleevent-send) (deny mach-lookup) "
        "(deny signal) (deny system-fcntl (fcntl-command 80 110)) "
        f'(allow file-read-data (literal "/") {read_rules}) '
        f"(allow file-write* (subpath {json.dumps(str(scratch))}) (literal \"/dev/null\")) "
        f"(deny file-read* (subpath {json.dumps(str(workspace / '.git'))})) "
    )
    env = {
        "PATH": TOOL_PATH, "HOME": str(scratch), "TMPDIR": str(scratch), "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    return run_process(
        ["/usr/bin/sandbox-exec", "-p", rules, executable, *argv[1:]], workspace, env, timeout,
    )


def artifacts(workspace: Path) -> list[str]:
    tracked = git(workspace, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
    untracked = git(workspace, "ls-files", "-z", "--others", "--exclude-standard")
    return sorted({
        name for name in (tracked + "\0" + untracked).split("\0")
        if name and not name.startswith(".aisys-check/")
    })[:200]
