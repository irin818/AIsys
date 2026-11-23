"""Full-power Codex Sub-Agent manager (spawn, async parallel, multi-turn resume, native review)."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .router import normalize_owned_path
from .worktree import (
    check_ownership,
    create_isolated_workspace,
    find_git_root,
    inspect_changes,
    merge_into_target,
    remove_isolated_workspace,
)

BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
SECRET_RE = re.compile(
    r"(?is)-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)"
    r"|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w-]{12,}|github_pat_[\w-]{12,}|"
    r"AIza[\w-]{20,}|ya29\.[\w.-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)"
    r"|\b(?:authorization|password|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)"
    r"""["']?\s*[:=]\s*["']?(?:bearer\s+)?[^,\s"'}]+"""
)
OUTPUT_LIMIT = 8_000_000


def redact(text: str) -> str:
    clean = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", str(text or ""))
    return SECRET_RE.sub("[REDACTED]", clean)


def codex_executable() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    if sys.platform == "darwin" and BUNDLED_CODEX.is_file() and os.access(BUNDLED_CODEX, os.X_OK):
        return str(BUNDLED_CODEX)
    return None


def _child_env() -> dict[str, str]:
    """Preserve essential macOS/dev environment so Codex runs real project tools & ChatGPT auth."""
    env = dict(os.environ)
    env.setdefault("LANG", "C.UTF-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _stop_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _summarize_codex_events(raw_lines: list[str]) -> dict[str, Any]:
    thread_id: str | None = None
    messages: list[str] = []
    commands: list[dict[str, Any]] = []
    file_changes: list[str] = []
    web_searches: list[str] = []
    errors: list[str] = []
    usage: dict[str, int] = {}
    turn_completed = False

    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "thread.started" and ev.get("thread_id"):
            thread_id = str(ev["thread_id"])
        elif etype == "item.completed":
            item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
            itype = item.get("type")
            if itype == "agent_message" and isinstance(item.get("text"), str):
                messages.append(redact(item["text"]))
            elif itype == "command_execution":
                commands.append(
                    {
                        "command": redact(str(item.get("command") or ""))[:180],
                        "exit_code": item.get("exit_code"),
                        "status": item.get("status"),
                    }
                )
            elif itype == "file_change":
                changes = item.get("changes") or []
                for ch in changes:
                    if isinstance(ch, dict) and ch.get("path"):
                        file_changes.append(str(ch["path"]))
            elif itype == "web_search":
                q = item.get("query") or item.get("text") or "web_search"
                web_searches.append(redact(str(q))[:160])
        elif etype == "turn.completed":
            turn_completed = True
            raw_u = ev.get("usage")
            if isinstance(raw_u, dict):
                usage = {k: int(v) for k, v in raw_u.items() if isinstance(v, int) and v >= 0}
        elif etype in {"error", "turn.failed"}:
            errors.append(redact(json.dumps(ev, ensure_ascii=False))[:400])

    return {
        "thread_id": thread_id,
        "final_message": messages[-1] if messages else "",
        "all_messages_count": len(messages),
        "commands_run": commands[-25:],
        "commands_count": len(commands),
        "file_change_events": sorted(set(file_changes)),
        "web_searches": web_searches[-10:],
        "usage": usage,
        "turn_completed": turn_completed,
        "errors": errors[-5:],
    }


class CodexManager:
    """Manages full-power Codex CLI sub-agent jobs with real Git Worktree isolation."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.jobs_dir = self.data_dir / "jobs"
        self.jobs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active_procs: dict[str, subprocess.Popen[bytes]] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_flags: set[str] = set()

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{8,32}", job_id):
            raise ValueError(f"非法 job_id：{job_id!r}")
        path = self.jobs_dir / job_id
        if not (path / "job.json").is_file():
            raise FileNotFoundError(f"未找到 Codex 子任务：{job_id}")
        return path

    def _load_job(self, job_id: str) -> dict[str, Any]:
        return json.loads((self._job_dir(job_id) / "job.json").read_text(encoding="utf-8"))

    def _save_job(self, job: dict[str, Any]) -> None:
        job["updated_at"] = round(time.time(), 3)
        _write_json_atomic(self.jobs_dir / job["job_id"] / "job.json", job)

    def _append_event(self, job_id: str, event: dict[str, Any]) -> None:
        ev_file = self.jobs_dir / job_id / "events.jsonl"
        try:
            with ev_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
        except OSError:
            pass

    def doctor(self) -> dict[str, Any]:
        exe = codex_executable()
        if not exe:
            return {
                "ok": False,
                "codex_installed": False,
                "error": "未找到 codex CLI（PATH 及 ChatGPT.app 内置路径均不存在）",
            }
        version = ""
        login_ok = False
        login_detail = ""
        try:
            vp = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
            version = redact(vp.stdout.strip().splitlines()[0]) if vp.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError) as err:
            version = str(err)
        try:
            lp = subprocess.run([exe, "login", "status"], capture_output=True, text=True, timeout=8)
            raw = redact((lp.stdout + " " + lp.stderr).strip())
            login_ok = lp.returncode == 0 and "logged in" in raw.lower()
            login_detail = raw
        except (OSError, subprocess.SubprocessError) as err:
            login_detail = str(err)

        git_ver = ""
        try:
            gp = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
            git_ver = gp.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass

        return {
            "ok": bool(exe and login_ok and git_ver),
            "codex_installed": True,
            "codex_executable": exe,
            "codex_version": version,
            "authenticated": login_ok,
            "auth_detail": login_detail,
            "git_version": git_ver,
            "full_power_profile": {
                "default_sandbox": "workspace-write",
                "web_search": "live",
                "shell_and_tests": "enabled",
                "multi_turn_resume": "enabled",
                "native_code_review": "enabled",
            },
        }

    def _run_codex_turn(
        self,
        *,
        job_id: str,
        cmd: list[str],
        cwd: Path,
        prompt: str,
        timeout_s: float,
        turn_index: int,
    ) -> None:
        started = time.monotonic()
        raw_lines: list[str] = []
        stderr_tail = ""
        issue = ""
        exit_code: int | None = None

        with (
            tempfile.TemporaryFile() as stdin_f,
            tempfile.TemporaryFile() as stdout_f,
            tempfile.TemporaryFile() as stderr_f,
        ):
            stdin_f.write(prompt.encode("utf-8"))
            stdin_f.seek(0)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=_child_env(),
                    stdin=stdin_f,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    start_new_session=True,
                )
            except OSError as err:
                job = self._load_job(job_id)
                job["status"] = "failed"
                job["error"] = redact(f"无法启动 Codex 进程：{err}")
                self._save_job(job)
                return

            with self._lock:
                self._active_procs[job_id] = proc

            job = self._load_job(job_id)
            job["pid"] = proc.pid
            job["status"] = "running"
            self._save_job(job)
            self._append_event(
                job_id,
                {
                    "ts": round(time.time(), 3),
                    "turn": turn_index,
                    "type": "process.spawned",
                    "pid": proc.pid,
                    "cwd": str(cwd),
                },
            )

            cursor = 0
            pending = b""
            try:
                while True:
                    total = min(os.fstat(stdout_f.fileno()).st_size, OUTPUT_LIMIT)
                    if total > cursor:
                        chunk = os.pread(stdout_f.fileno(), total - cursor, cursor)
                        cursor += len(chunk)
                        pending += chunk
                        while b"\n" in pending:
                            raw_b, pending = pending.split(b"\n", 1)
                            line = raw_b.decode("utf-8", errors="replace")
                            raw_lines.append(line)
                            self._ingest_live_line(job_id, turn_index, line)

                    with self._lock:
                        cancelled = job_id in self._cancel_flags
                    if cancelled:
                        issue = "cancelled"
                        break
                    if os.fstat(stdout_f.fileno()).st_size + os.fstat(stderr_f.fileno()).st_size > OUTPUT_LIMIT:
                        issue = "output_limit"
                        break
                    if proc.poll() is not None:
                        break
                    if time.monotonic() - started >= timeout_s:
                        issue = "timeout"
                        break
                    time.sleep(0.05)
            finally:
                _stop_group(proc)
                with self._lock:
                    self._active_procs.pop(job_id, None)
                    self._cancel_flags.discard(job_id)

            # Drain remaining bytes after process exit
            total = min(os.fstat(stdout_f.fileno()).st_size, OUTPUT_LIMIT)
            if total > cursor:
                pending += os.pread(stdout_f.fileno(), total - cursor, cursor)
            if pending:
                for tail_b in pending.splitlines():
                    line = tail_b.decode("utf-8", errors="replace")
                    raw_lines.append(line)
                    self._ingest_live_line(job_id, turn_index, line)

            exit_code = proc.returncode
            stderr_f.seek(0)
            stderr_tail = redact(stderr_f.read(8192).decode("utf-8", errors="replace")).strip()

        duration = round(time.monotonic() - started, 2)
        summary = _summarize_codex_events(raw_lines)

        job = self._load_job(job_id)
        if summary["thread_id"]:
            job["thread_id"] = summary["thread_id"]
        job["pid"] = None
        job["duration_s"] = round(float(job.get("duration_s") or 0.0) + duration, 2)
        job["last_turn_duration_s"] = duration
        job["last_exit_code"] = exit_code
        job["turns"].append(
            {
                "turn": turn_index,
                "duration_s": duration,
                "exit_code": exit_code,
                "issue": issue,
                "commands_count": summary["commands_count"],
                "commands_run": summary["commands_run"],
                "file_change_events": summary["file_change_events"],
                "web_searches": summary["web_searches"],
                "usage": summary["usage"],
                "final_message": summary["final_message"],
                "errors": summary["errors"],
            }
        )
        job["final_message"] = summary["final_message"] or job.get("final_message", "")
        job["usage"] = summary["usage"] or job.get("usage", {})

        if issue == "cancelled":
            job["status"] = "cancelled"
            job["error"] = "任务已手动取消"
        elif issue == "timeout":
            job["status"] = "failed"
            job["error"] = f"Codex 执行超时（>{int(timeout_s)}s）"
        elif exit_code != 0 and not summary["final_message"]:
            job["status"] = "failed"
            job["error"] = "; ".join(summary["errors"]) or stderr_tail or f"Codex exited with code {exit_code}"
        else:
            job["status"] = "completed"
            job["error"] = ""

        # Attach real worktree change snapshot & ownership gate preview
        if job.get("worktree_path") and job.get("base_sha"):
            wt = Path(job["worktree_path"])
            ch = inspect_changes(wt, job["base_sha"])
            job["changed_files"] = ch["changed_files"]
            job["deleted_files"] = ch["deleted_files"]
            job["diff_stat"] = ch["diff_stat"]
            if job.get("owned_paths"):
                job["ownership_check"] = check_ownership(ch["changed_files"], job["owned_paths"])
        self._save_job(job)

    def _ingest_live_line(self, job_id: str, turn_index: int, line: str) -> None:
        raw = line.strip()
        if not raw:
            return
        try:
            ev = json.loads(raw)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        etype = ev.get("type")
        if etype == "thread.started" and ev.get("thread_id"):
            tid = str(ev["thread_id"])
            try:
                job = self._load_job(job_id)
                job["thread_id"] = tid
                self._save_job(job)
            except (OSError, ValueError):
                pass
            self._append_event(
                job_id, {"ts": round(time.time(), 3), "turn": turn_index, "type": etype, "thread_id": tid}
            )
        elif etype in {"item.completed", "turn.completed", "error", "turn.failed"}:
            compact: dict[str, Any] = {"ts": round(time.time(), 3), "turn": turn_index, "type": etype}
            if etype == "item.completed" and isinstance(ev.get("item"), dict):
                it = ev["item"]
                compact["item_type"] = it.get("type")
                if it.get("command"):
                    compact["command"] = redact(str(it["command"]))[:160]
                if it.get("exit_code") is not None:
                    compact["exit_code"] = it.get("exit_code")
            elif etype == "turn.completed":
                compact["usage"] = ev.get("usage", {})
            self._append_event(job_id, compact)

    def _build_prompt(self, prompt: str, owned_paths: list[str], mode: str) -> str:
        if not owned_paths:
            return prompt
        lock_note = (
            "【AIsys 双满血协作契约】\n"
            "- 你以满血 Codex 子 Agent 运行，拥有完整全仓读取、终端测试验证与实时联网能力。\n"
            "- 你可以自由检索和阅读整个项目并运行测试，但最终保留在工作区中的文件改动必须严格限制在你的独占路径内："
            f"{', '.join(owned_paths)}\n"
            "- 合流闸门会在合并前通过 `git diff` 校验所有改动路径；请勿顺手修改其他归属 Gemini 的文件。\n\n"
        )
        return lock_note + prompt

    def spawn(
        self,
        *,
        prompt: str,
        project_dir: str | None = None,
        sandbox: str = "workspace-write",
        owned_paths: list[str] | None = None,
        isolate_worktree: bool = True,
        web_search: bool = True,
        model: str | None = None,
        background: bool = False,
        timeout_s: int = 360,
        role: str = "Codex Full-Power Sub-Agent",
    ) -> dict[str, Any]:
        exe = codex_executable()
        if not exe:
            raise RuntimeError("未找到本机 codex CLI，请确认 ChatGPT.app 或 codex 已安装并登录。")
        if sandbox not in {"workspace-write", "read-only"}:
            raise ValueError("sandbox 仅支持 'workspace-write' 或 'read-only'")

        norm_owned = [normalize_owned_path(p) for p in (owned_paths or [])]
        job_id = uuid4().hex[:12]
        job_dir = self.jobs_dir / job_id
        proj_path = Path(project_dir).expanduser().resolve() if project_dir else None

        if isolate_worktree and sandbox == "workspace-write":
            wt = create_isolated_workspace(proj_path, job_dir, job_id)
            run_cwd = wt.worktree_path
            repo_root = str(wt.repo_root) if wt.repo_root else None
            branch = wt.branch
            base_sha = wt.base_sha
            is_wt = wt.is_git_worktree
        else:
            job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            run_cwd = proj_path if (proj_path and proj_path.is_dir()) else job_dir
            git_root = find_git_root(run_cwd) if proj_path else None
            repo_root = str(git_root) if git_root else (str(proj_path) if proj_path else None)
            branch = None
            base_sha = ""
            is_wt = False

        cmd = [
            exe,
            "exec",
            "-C",
            str(run_cwd),
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--json",
            "-c",
            'approval_policy="never"',
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            f'web_search="{"live" if web_search else "disabled"}"',
            "--disable",
            "memories",
        ]
        if model and model.strip():
            cmd.extend(["--model", model.strip()])
        cmd.append("-")

        job: dict[str, Any] = {
            "job_id": job_id,
            "role": role,
            "status": "starting",
            "sandbox": sandbox,
            "web_search": web_search,
            "model": model or "default",
            "project_dir": str(proj_path) if proj_path else None,
            "repo_root": repo_root,
            "worktree_path": str(run_cwd),
            "branch": branch,
            "base_sha": base_sha,
            "is_git_worktree": is_wt,
            "owned_paths": norm_owned,
            "thread_id": None,
            "pid": None,
            "created_at": round(time.time(), 3),
            "updated_at": round(time.time(), 3),
            "duration_s": 0.0,
            "last_turn_duration_s": 0.0,
            "turns": [],
            "final_message": "",
            "changed_files": [],
            "deleted_files": [],
            "diff_stat": "",
            "usage": {},
            "error": "",
        }
        self._save_job(job)
        full_prompt = self._build_prompt(prompt, norm_owned, sandbox)

        if background:
            th = threading.Thread(
                target=self._run_codex_turn,
                kwargs={
                    "job_id": job_id,
                    "cmd": cmd,
                    "cwd": run_cwd,
                    "prompt": full_prompt,
                    "timeout_s": float(timeout_s),
                    "turn_index": 1,
                },
                name=f"aisys-codex-{job_id}",
                daemon=True,
            )
            with self._lock:
                self._threads[job_id] = th
            th.start()
            return self.status(job_id)

        self._run_codex_turn(
            job_id=job_id,
            cmd=cmd,
            cwd=run_cwd,
            prompt=full_prompt,
            timeout_s=float(timeout_s),
            turn_index=1,
        )
        return self.status(job_id, include_diff=True)

    def reply(
        self,
        *,
        job_id: str,
        prompt: str,
        background: bool = False,
        timeout_s: int = 360,
        web_search: bool = True,
    ) -> dict[str, Any]:
        """Continue the exact same full-power Codex thread via native `codex exec resume`."""
        exe = codex_executable()
        if not exe:
            raise RuntimeError("未找到本机 codex CLI")
        job = self._load_job(job_id)
        if job["status"] == "running":
            raise RuntimeError(f"任务 {job_id} 正在运行中，请先等待完成再续接")
        tid = job.get("thread_id")
        if not tid:
            raise RuntimeError(f"任务 {job_id} 没有可续接的 thread_id")
        run_cwd = Path(job["worktree_path"])
        if not run_cwd.is_dir():
            raise RuntimeError(f"任务 {job_id} 的工作目录已不存在：{run_cwd}")

        turn_index = len(job.get("turns") or []) + 1
        cmd = [
            exe,
            "exec",
            "resume",
            tid,
            "--skip-git-repo-check",
            "--json",
            "-c",
            'approval_policy="never"',
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            f'web_search="{"live" if web_search else "disabled"}"',
            "--disable",
            "memories",
            "-",
        ]
        job["status"] = "starting"
        self._save_job(job)

        if background:
            th = threading.Thread(
                target=self._run_codex_turn,
                kwargs={
                    "job_id": job_id,
                    "cmd": cmd,
                    "cwd": run_cwd,
                    "prompt": prompt,
                    "timeout_s": float(timeout_s),
                    "turn_index": turn_index,
                },
                name=f"aisys-codex-{job_id}-t{turn_index}",
                daemon=True,
            )
            with self._lock:
                self._threads[job_id] = th
            th.start()
            return self.status(job_id)

        self._run_codex_turn(
            job_id=job_id,
            cmd=cmd,
            cwd=run_cwd,
            prompt=prompt,
            timeout_s=float(timeout_s),
            turn_index=turn_index,
        )
        return self.status(job_id, include_diff=True)

    def review(
        self,
        *,
        project_dir: str,
        mode: str = "uncommitted",
        target: str | None = None,
        instructions: str = "",
        model: str | None = None,
        timeout_s: int = 240,
    ) -> dict[str, Any]:
        """Run Codex's native full-repository code reviewer (`codex exec review`)."""
        exe = codex_executable()
        if not exe:
            raise RuntimeError("未找到本机 codex CLI")
        cwd = Path(project_dir).expanduser().resolve()
        if not cwd.is_dir():
            raise ValueError(f"目录不存在：{cwd}")

        cmd = [
            exe,
            "exec",
            "review",
            "--json",
            "-c",
            'approval_policy="never"',
            "-c",
            "project_doc_max_bytes=0",
            "--disable",
            "memories",
        ]
        if model and model.strip():
            cmd.extend(["--model", model.strip()])
        if mode == "uncommitted":
            cmd.append("--uncommitted")
        elif mode == "base":
            if not target:
                raise ValueError("mode='base' 必须提供 target 分支名")
            cmd.extend(["--base", target])
        elif mode == "commit":
            if not target:
                raise ValueError("mode='commit' 必须提供 target commit SHA")
            cmd.extend(["--commit", target])
        elif mode == "custom":
            if not instructions.strip():
                raise ValueError("mode='custom' 必须提供审查说明 instructions")
            cmd.append(instructions.strip())
        else:
            raise ValueError("mode 必须为 'uncommitted' | 'base' | 'commit' | 'custom'")

        started = time.monotonic()
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=_child_env(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
        )
        duration = round(time.monotonic() - started, 2)
        parsed = _summarize_codex_events(proc.stdout.splitlines())
        return {
            "ok": proc.returncode == 0 or bool(parsed["final_message"]),
            "mode": mode,
            "target": target,
            "project_dir": str(cwd),
            "duration_s": duration,
            "exit_code": proc.returncode,
            "review": parsed["final_message"] or redact(proc.stderr[-1000:]),
            "commands_count": parsed["commands_count"],
            "commands_run": parsed["commands_run"],
            "usage": parsed["usage"],
        }

    def status(
        self,
        job_id: str,
        *,
        wait: bool = False,
        timeout_s: int = 300,
        include_diff: bool = False,
    ) -> dict[str, Any]:
        if wait:
            with self._lock:
                th = self._threads.get(job_id)
            if th and th.is_alive():
                th.join(timeout=max(1, timeout_s))

        job = self._load_job(job_id)
        out = dict(job)
        if include_diff and job.get("worktree_path") and job.get("base_sha"):
            wt = Path(job["worktree_path"])
            if wt.is_dir():
                ch = inspect_changes(wt, job["base_sha"])
                out["changed_files"] = ch["changed_files"]
                out["deleted_files"] = ch["deleted_files"]
                out["diff_stat"] = ch["diff_stat"]
                out["diff"] = redact(ch["diff"])
                if job.get("owned_paths"):
                    out["ownership_check"] = check_ownership(ch["changed_files"], job["owned_paths"])
        return out

    def merge(
        self,
        *,
        job_id: str,
        target_dir: str | None = None,
        owned_paths_override: list[str] | None = None,
        dry_run: bool = False,
        cleanup_after: bool = False,
        allow_overwrite_dirty: bool = False,
    ) -> dict[str, Any]:
        job = self._load_job(job_id)
        if job["status"] == "running":
            raise RuntimeError(f"任务 {job_id} 仍在运行中，无法合并")
        target = (
            Path(target_dir).expanduser().resolve()
            if target_dir
            else (Path(job["repo_root"]) if job.get("repo_root") else None)
        )
        if not target or not target.is_dir():
            raise ValueError("请提供有效的合并目标目录 target_dir")

        wt_path = Path(job["worktree_path"])
        base_sha = job.get("base_sha") or ""
        owned = owned_paths_override if owned_paths_override is not None else (job.get("owned_paths") or [])
        result = merge_into_target(
            target_root=target,
            worktree_path=wt_path,
            base_sha=base_sha,
            owned_paths=owned,
            dry_run=dry_run,
            allow_overwrite_dirty=allow_overwrite_dirty,
        )
        if result.get("merged") and cleanup_after:
            result["cleanup"] = self.cleanup(job_id)
        return result

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancel_flags.add(job_id)
            proc = self._active_procs.get(job_id)
        if proc is not None:
            _stop_group(proc)
        return self.status(job_id)

    def cleanup(self, job_id: str) -> dict[str, Any]:
        job = self._load_job(job_id)
        if job["status"] == "running":
            self.cancel(job_id)
        repo = Path(job["repo_root"]) if job.get("repo_root") else None
        wt_path = Path(job["worktree_path"])
        res = remove_isolated_workspace(
            repo_root=repo,
            worktree_path=wt_path,
            branch=job.get("branch"),
            is_git_worktree=bool(job.get("is_git_worktree")),
        )
        job["cleaned_up"] = True
        self._save_job(job)
        return {"job_id": job_id, **res}

    def list_jobs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not self.jobs_dir.is_dir():
            return items
        for entry in self.jobs_dir.iterdir():
            jf = entry / "job.json"
            if not jf.is_file():
                continue
            try:
                j = json.loads(jf.read_text(encoding="utf-8"))
                items.append(
                    {
                        "job_id": j["job_id"],
                        "role": j.get("role", ""),
                        "status": j.get("status", ""),
                        "thread_id": j.get("thread_id"),
                        "sandbox": j.get("sandbox"),
                        "project_dir": j.get("project_dir"),
                        "changed_files": j.get("changed_files", []),
                        "duration_s": j.get("duration_s", 0.0),
                        "created_at": j.get("created_at", 0.0),
                        "cleaned_up": j.get("cleaned_up", False),
                    }
                )
            except (OSError, ValueError, KeyError):
                continue
        return sorted(items, key=lambda x: x["created_at"], reverse=True)[:limit]
