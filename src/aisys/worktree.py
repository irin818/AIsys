"""Real Git Worktree physical isolation and mechanical anti-overlap Diff Gate."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .router import normalize_owned_path, paths_overlap


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("LANG", "C.UTF-8")
    return env


def run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=_git_env(),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {msg}")
    return proc


def find_git_root(path: Path) -> Path | None:
    resolved = path.resolve()
    if not resolved.is_dir():
        return None
    proc = run_git(resolved, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def has_commits(repo: Path) -> bool:
    return run_git(repo, "rev-parse", "--verify", "HEAD^{commit}", check=False).returncode == 0


def head_sha(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").stdout.strip()


@dataclass
class WorktreeHandle:
    repo_root: Path | None
    worktree_path: Path
    branch: str | None
    base_sha: str
    is_git_worktree: bool


def create_isolated_workspace(
    project_dir: Path | None,
    job_dir: Path,
    job_id: str,
) -> WorktreeHandle:
    """Create a real Git Worktree for Codex (or a fresh isolated Git dir for non-Git tasks)."""
    wt_path = (job_dir / "worktree").resolve()
    job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    git_root = find_git_root(project_dir) if project_dir else None
    if git_root is not None and has_commits(git_root):
        base = head_sha(git_root)
        branch = f"aisys/codex-{job_id}"
        run_git(git_root, "worktree", "add", "-b", branch, str(wt_path), base)
        return WorktreeHandle(
            repo_root=git_root,
            worktree_path=wt_path,
            branch=branch,
            base_sha=base,
            is_git_worktree=True,
        )

    # Non-Git directory (or pure learning/research task): create clean isolated Git workspace
    wt_path.mkdir(mode=0o755, parents=True, exist_ok=True)
    run_git(wt_path, "init", "-q", "-b", "main")
    run_git(wt_path, "config", "user.name", "AIsys Codex")
    run_git(wt_path, "config", "user.email", "codex@local")
    run_git(wt_path, "commit", "--allow-empty", "-q", "-m", "aisys: base workspace")
    base = head_sha(wt_path)
    return WorktreeHandle(
        repo_root=project_dir.resolve() if project_dir and project_dir.is_dir() else None,
        worktree_path=wt_path,
        branch="main",
        base_sha=base,
        is_git_worktree=False,
    )


def inspect_changes(worktree_path: Path, base_sha: str) -> dict[str, Any]:
    """List ALL files created, modified, or deleted in the worktree vs base_sha."""
    if not worktree_path.is_dir():
        return {"changed_files": [], "deleted_files": [], "diff_stat": "", "diff": ""}

    changed: set[str] = set()
    deleted: set[str] = set()

    # 1. Tracked changes vs base_sha (covers both committed and unstaged/staged edits!)
    ns = run_git(worktree_path, "diff", "--name-status", base_sha, check=False)
    for line in ns.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            if len(parts) >= 3:
                old_p, new_p = parts[1], parts[2]
                changed.add(new_p)
                if status.startswith("R"):
                    changed.add(old_p)
                    deleted.add(old_p)
        elif len(parts) >= 2:
            rel = parts[1]
            changed.add(rel)
            if status == "D":
                deleted.add(rel)

    # 2. Untracked new files
    untracked = run_git(worktree_path, "ls-files", "-z", "--others", "--exclude-standard", check=False)
    for rel in untracked.stdout.split("\0"):
        if rel:
            changed.add(rel)
            deleted.discard(rel)

    diff_stat = run_git(worktree_path, "diff", "--stat", base_sha, check=False).stdout.strip()
    tracked_diff = run_git(worktree_path, "diff", "--no-ext-diff", "--no-textconv", base_sha, check=False).stdout

    diff_parts = [tracked_diff] if tracked_diff.strip() else []
    for rel in sorted(changed):
        fpath = worktree_path / rel
        if rel in untracked.stdout.split("\0") and fpath.is_file() and not fpath.is_symlink():
            try:
                raw = fpath.read_bytes()
                if b"\0" in raw:
                    diff_parts.append(f"diff --git a/{rel} b/{rel}\nBinary file ({len(raw)} bytes)")
                else:
                    text = raw[:24_000].decode("utf-8", errors="replace")
                    body = "\n".join(f"+{ln}" for ln in text.splitlines())
                    diff_parts.append(f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n{body}")
            except OSError:
                continue

    return {
        "changed_files": sorted(changed),
        "deleted_files": sorted(deleted),
        "diff_stat": diff_stat,
        "diff": "\n\n".join(diff_parts)[:60_000],
    }


def check_ownership(changed_files: list[str], owned_paths: list[str]) -> dict[str, Any]:
    """Verify every changed file is strictly covered by owned_paths (mechanical anti-overlap)."""
    normalized_owned = [normalize_owned_path(p) for p in owned_paths]
    violations: list[str] = []
    allowed_files: list[str] = []

    for rel in changed_files:
        try:
            norm_rel = normalize_owned_path(rel)
        except ValueError:
            violations.append(rel)
            continue
        if any(paths_overlap(norm_rel, spec) for spec in normalized_owned):
            allowed_files.append(norm_rel)
        else:
            violations.append(rel)

    return {
        "allowed": not violations,
        "owned_paths": normalized_owned,
        "allowed_files": sorted(allowed_files),
        "violations": sorted(violations),
    }


def _target_dirty_files(target_root: Path) -> set[str]:
    if not find_git_root(target_root):
        return set()
    proc = run_git(target_root, "status", "--porcelain", "-z", check=False)
    dirty: set[str] = set()
    entries = proc.stdout.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        rel = entry[3:]
        if rel:
            dirty.add(rel)
        if "R" in status or "C" in status:
            if i < len(entries) and entries[i]:
                dirty.add(entries[i])
            i += 1
    return dirty


def merge_into_target(
    *,
    target_root: Path,
    worktree_path: Path,
    base_sha: str,
    owned_paths: list[str],
    dry_run: bool = False,
    allow_overwrite_dirty: bool = False,
) -> dict[str, Any]:
    """Mechanically enforce owned_paths Diff Gate and merge Codex files into Gemini's workspace."""
    info = inspect_changes(worktree_path, base_sha)
    changed = info["changed_files"]
    deleted = set(info["deleted_files"])

    if not changed:
        return {
            "merged": False,
            "reason": "no_changes",
            "changed_files": [],
            "violations": [],
            "dirty_conflicts": [],
        }

    if owned_paths:
        gate = check_ownership(changed, owned_paths)
        if not gate["allowed"]:
            return {
                "merged": False,
                "reason": "ownership_violation",
                "changed_files": changed,
                "violations": gate["violations"],
                "dirty_conflicts": [],
                "message": (
                    "机械防重叠闸门拦截：Codex 修改了未授权路径 "
                    f"{gate['violations']}（允许路径：{gate['owned_paths']}）。"
                    "请使用 codex_reply 要求 Codex 回退越界改动后再合并。"
                ),
            }
    else:
        # Even without explicit owned_paths, reject sensitive/escaping paths
        for rel in changed:
            normalize_owned_path(rel)

    # Protect Gemini's uncommitted edits in target_root from accidental overwrite
    dirty_conflicts: list[str] = []
    if not allow_overwrite_dirty:
        dirty = _target_dirty_files(target_root)
        dirty_conflicts = sorted(p for p in changed if p in dirty)
        if dirty_conflicts:
            return {
                "merged": False,
                "reason": "target_dirty_conflict",
                "changed_files": changed,
                "violations": [],
                "dirty_conflicts": dirty_conflicts,
                "message": (
                    f"主工作区以下文件存在未提交修改，拒绝直接覆盖：{dirty_conflicts}。"
                    "请先检查/提交主工作区更改或确认所有权后重试。"
                ),
            }

    if dry_run:
        return {
            "merged": False,
            "dry_run": True,
            "reason": "ready_to_merge",
            "changed_files": changed,
            "deleted_files": sorted(deleted),
            "violations": [],
            "dirty_conflicts": [],
        }

    target_resolved = target_root.resolve()
    applied: list[str] = []
    removed: list[str] = []
    for rel in changed:
        dst = (target_resolved / rel).resolve()
        if not dst.is_relative_to(target_resolved):
            raise ValueError(f"目标路径越界：{rel}")
        src = (worktree_path / rel).resolve()
        if rel in deleted or not src.exists():
            if dst.is_file() or dst.is_symlink():
                dst.unlink()
                removed.append(rel)
            continue
        if src.is_symlink():
            raise ValueError(f"拒绝合并符号链接：{rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        applied.append(rel)

    return {
        "merged": True,
        "reason": "merged",
        "applied_files": sorted(applied),
        "removed_files": sorted(removed),
        "changed_files": changed,
        "violations": [],
        "dirty_conflicts": [],
        "diff_stat": info["diff_stat"],
    }


def remove_isolated_workspace(
    *,
    repo_root: Path | None,
    worktree_path: Path,
    branch: str | None,
    is_git_worktree: bool,
) -> dict[str, Any]:
    """Safely prune temporary Git worktree and temporary branch."""
    removed_wt = False
    removed_branch = False
    if is_git_worktree and repo_root and repo_root.is_dir():
        if worktree_path.exists():
            res = run_git(repo_root, "worktree", "remove", "--force", str(worktree_path), check=False)
            removed_wt = res.returncode == 0
        run_git(repo_root, "worktree", "prune", check=False)
        if branch and branch.startswith("aisys/codex-"):
            bres = run_git(repo_root, "branch", "-D", branch, check=False)
            removed_branch = bres.returncode == 0
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
        removed_wt = True
    return {"removed_worktree": removed_wt, "removed_branch": removed_branch}
