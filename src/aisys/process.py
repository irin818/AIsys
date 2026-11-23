"""Bounded, non-interactive child processes with isolated process groups."""
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

SECRET_PATTERNS = re.compile(
    r"(?is)-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)"
    r"|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w-]{12,}|github_pat_[\w-]{12,}|"
    r"AIza[\w-]{20,}|ya29\.[\w.-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)"
    r"|\b(?:authorization|password|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)"
    r"""["']?\s*[:=]\s*["']?(?:bearer\s+)?[^,\s"'}]+"""
)
OUTPUT_LIMIT = 2_000_000
PROCESS_INTERRUPTED = Event()


def redact(text: str) -> str:
    clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
    return SECRET_PATTERNS.sub("[REDACTED]", clean)


def cli_env() -> dict[str, str]:
    names = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "PATH", "TMPDIR", "CODEX_HOME")
    return {name: os.environ[name] for name in names if name in os.environ}


@dataclass(frozen=True)
class ProcessResult:
    code: int | None = None
    stdout: str = ""
    stderr: str = ""
    issue: str = ""
    duration: float = 0


def stop_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _drain_stdout_lines(
    fd: int,
    cursor: int,
    pending: bytes,
    callback: Callable[[str], None],
    *,
    flush_tail: bool = False,
) -> tuple[int, bytes]:
    total = min(os.fstat(fd).st_size, OUTPUT_LIMIT)
    if total > cursor:
        chunk = os.pread(fd, total - cursor, cursor)
        cursor += len(chunk)
        pending += chunk
    while b"\n" in pending:
        raw_line, pending = pending.split(b"\n", 1)
        try:
            callback(raw_line.decode(errors="replace"))
        except Exception:
            pass
    if flush_tail and pending:
        try:
            callback(pending.decode(errors="replace"))
        except Exception:
            pass
        pending = b""
    return cursor, pending


def run_process(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    prompt: str = "",
    *,
    on_start: Callable[[int], None] | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
) -> ProcessResult:
    started = time.monotonic()
    issue = ""
    with (
        tempfile.TemporaryFile() as stdin,
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        stdin.write(prompt.encode())
        stdin.seek(0)
        try:
            child = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
        except FileNotFoundError:
            return ProcessResult(issue="unavailable")
        except OSError:
            return ProcessResult(issue="failed")
        if on_start is not None:
            try:
                on_start(child.pid)
            except Exception:
                pass
        cursor = 0
        pending = b""
        try:
            while True:
                if on_stdout_line is not None:
                    cursor, pending = _drain_stdout_lines(
                        stdout.fileno(), cursor, pending, on_stdout_line
                    )
                size = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
                if PROCESS_INTERRUPTED.is_set():
                    issue = "interrupted"
                    break
                if size > OUTPUT_LIMIT:
                    issue = "invalid"
                    break
                if child.poll() is not None:
                    break
                if time.monotonic() - started >= timeout:
                    issue = "timeout"
                    break
                time.sleep(0.05)
        finally:
            # The main process can exit while descendants keep running.
            stop_group(child)
        if on_stdout_line is not None:
            _drain_stdout_lines(
                stdout.fileno(), cursor, pending, on_stdout_line, flush_tail=True
            )
        stdout.seek(0)
        stderr.seek(0)
        return ProcessResult(
            child.returncode,
            stdout.read(OUTPUT_LIMIT).decode(errors="replace"),
            stderr.read(OUTPUT_LIMIT).decode(errors="replace"),
            issue,
            round(time.monotonic() - started, 3),
        )
