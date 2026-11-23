"""Tests for AIsys v2: Smart routing, anti-overlap validator, real Git Worktree Diff Gate, MCP stdio, and CodexManager."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from aisys.codex import CodexManager
from aisys.router import analyze_and_route, validate_division
from aisys.worktree import (
    create_isolated_workspace,
    merge_into_target,
    remove_isolated_workspace,
    run_git,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_smart_capability_routing_preserves_dual_full_power():
    algo = analyze_and_route("排查异步并发死锁 bug 并重构核心状态机锁算法，补全 pytest 压测")
    assert algo["lead_model"] == "codex"
    assert algo["dual_full_power"] is True
    assert algo["codex_execution_profile"]["sandbox"] == "workspace-write"
    assert algo["codex_execution_profile"]["web_search"] == "live"

    ux = analyze_and_route("重新设计前端页面视觉美学、SVG 动效布局与系统架构文档")
    assert ux["lead_model"] == "gemini"
    assert ux["dual_full_power"] is True

    learn = analyze_and_route("帮我彻底搞懂 Rust 生命周期原理与教程概念", mode="learn")
    assert learn["mode"] == "learn"
    assert learn["lead_model"] == "gemini"

    rev = analyze_and_route("检查当前改动", mode="review")
    assert rev["mode"] == "review"
    assert rev["lead_model"] == "co_lead"
    assert rev["codex_execution_profile"]["worktree_isolation"] is False

    fast = analyze_and_route("给项目增加一个全栈缓存模块", mode="fast")
    assert fast["fast_track"] is True


def test_validate_division_mechanical_anti_overlap():
    ok = validate_division(
        [
            {"id": "T1", "owner": "gemini", "title": "UI & docs", "owned_paths": ["ui/", "README.md"]},
            {"id": "T2", "owner": "codex", "title": "Core engine", "owned_paths": ["src/core.py", "tests/"]},
            {
                "id": "T3",
                "owner": "gemini",
                "title": "Integration",
                "owned_paths": ["docs/guide.md"],
                "depends_on": ["T1", "T2"],
            },
        ]
    )
    assert ok["valid"] is True
    assert ok["waves"] == [["T1", "T2"], ["T3"]]

    # Cross-owner directory prefix overlap must be rejected
    cross = validate_division(
        [
            {"id": "T1", "owner": "gemini", "title": "A", "owned_paths": ["src/"]},
            {"id": "T2", "owner": "codex", "title": "B", "owned_paths": ["src/engine.py"]},
        ]
    )
    assert cross["valid"] is False
    assert any("跨模型路径重叠" in c for c in cross["conflicts"])

    # Same-owner concurrent wave overlap must be rejected
    concurrent = validate_division(
        [
            {"id": "T1", "owner": "codex", "title": "A", "owned_paths": ["src/app.py"]},
            {"id": "T2", "owner": "codex", "title": "B", "owned_paths": ["src/app.py"]},
        ]
    )
    assert concurrent["valid"] is False
    assert any("同波次路径冲突" in c for c in concurrent["conflicts"])

    # Cyclic dependencies rejected
    cycle = validate_division(
        [
            {"id": "T1", "owner": "gemini", "title": "A", "owned_paths": ["a.md"], "depends_on": ["T2"]},
            {"id": "T2", "owner": "codex", "title": "B", "owned_paths": ["b.py"], "depends_on": ["T1"]},
        ]
    )
    assert cycle["valid"] is False
    assert any("循环" in c for c in cycle["conflicts"])

    # Sensitive & escaping paths rejected
    bad_path = validate_division(
        [
            {"id": "T1", "owner": "codex", "title": "Leak", "owned_paths": [".env.local", "../outside.py"]},
        ]
    )
    assert bad_path["valid"] is False


def test_real_git_worktree_isolation_and_diff_gate(tmp_path):
    repo = tmp_path / "demo-repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "user.email", "test@local")
    (repo / "ui.md").write_text("# UI v1\n", encoding="utf-8")
    (repo / "core.py").write_text("def x(): return 1\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "init")

    # Gemini edits ui.md in main working tree simultaneously!
    (repo / "ui.md").write_text("# UI v2 by Gemini\n", encoding="utf-8")

    # Codex gets a physical isolated Git Worktree
    wt = create_isolated_workspace(repo, tmp_path / "job1", "abc12345")
    assert wt.is_git_worktree is True
    assert wt.worktree_path.is_dir()
    # Worktree does NOT see Gemini's uncommitted ui.md edit (clean physical isolation)
    assert (wt.worktree_path / "ui.md").read_text(encoding="utf-8") == "# UI v1\n"

    # Codex modifies its owned file + illegally touches Gemini's ui.md
    (wt.worktree_path / "core.py").write_text("def x(): return 42\n", encoding="utf-8")
    (wt.worktree_path / "tests").mkdir()
    (wt.worktree_path / "tests" / "test_core.py").write_text("assert True\n", encoding="utf-8")
    (wt.worktree_path / "ui.md").write_text("# Illegally touched by Codex\n", encoding="utf-8")

    # Diff Gate MUST reject out-of-bounds modification
    blocked = merge_into_target(
        target_root=repo,
        worktree_path=wt.worktree_path,
        base_sha=wt.base_sha,
        owned_paths=["core.py", "tests/"],
    )
    assert blocked["merged"] is False
    assert blocked["reason"] == "ownership_violation"
    assert blocked["violations"] == ["ui.md"]
    # Main repo untouched!
    assert (repo / "ui.md").read_text(encoding="utf-8") == "# UI v2 by Gemini\n"
    assert (repo / "core.py").read_text(encoding="utf-8") == "def x(): return 1\n"

    # Codex reverts out-of-bounds ui.md -> Diff Gate passes and merges cleanly!
    run_git(wt.worktree_path, "checkout", "--", "ui.md")
    merged = merge_into_target(
        target_root=repo,
        worktree_path=wt.worktree_path,
        base_sha=wt.base_sha,
        owned_paths=["core.py", "tests/"],
    )
    assert merged["merged"] is True
    assert set(merged["applied_files"]) == {"core.py", "tests/test_core.py"}
    assert (repo / "core.py").read_text(encoding="utf-8") == "def x(): return 42\n"
    assert (repo / "tests" / "test_core.py").read_text(encoding="utf-8") == "assert True\n"
    # Gemini's work is 100% preserved!
    assert (repo / "ui.md").read_text(encoding="utf-8") == "# UI v2 by Gemini\n"

    # Dirty conflict guard: if Gemini also dirtied core.py, refuse overwrite unless explicit
    (repo / "core.py").write_text("def x(): return 99\n", encoding="utf-8")
    dirty_block = merge_into_target(
        target_root=repo,
        worktree_path=wt.worktree_path,
        base_sha=wt.base_sha,
        owned_paths=["core.py", "tests/"],
    )
    assert dirty_block["merged"] is False
    assert dirty_block["reason"] == "target_dirty_conflict"

    # Cleanup removes temporary worktree & branch cleanly
    cleaned = remove_isolated_workspace(
        repo_root=wt.repo_root,
        worktree_path=wt.worktree_path,
        branch=wt.branch,
        is_git_worktree=wt.is_git_worktree,
    )
    assert cleaned["removed_worktree"] is True
    assert cleaned["removed_branch"] is True
    assert not wt.worktree_path.exists()


def test_codex_manager_async_spawn_resume_and_merge_with_fake_cli(tmp_path, monkeypatch):
    # Create a tiny fake codex binary speaking real Codex NDJSON protocol
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
cwd = Path(os.getcwd())
if "resume" in args:
    (cwd / "engine.py").write_text("VALUE = 2\\n", encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pytest -q", "exit_code": 0}}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Updated VALUE to 2 after feedback"}}), flush=True)
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 420, "output_tokens": 40}}), flush=True)
else:
    (cwd / "engine.py").write_text("VALUE = 1\\n", encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "engine.py"}]}}), flush=True)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Implemented VALUE = 1"}}), flush=True)
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 0, "output_tokens": 30}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr("aisys.codex.codex_executable", lambda: str(fake_codex))

    repo = tmp_path / "proj"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "user.email", "test@local")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "init")

    mgr = CodexManager(tmp_path / "data")
    started = mgr.spawn(
        prompt="Create engine.py",
        project_dir=str(repo),
        owned_paths=["engine.py"],
        background=True,
    )
    done = mgr.status(started["job_id"], wait=True, include_diff=True)
    assert done["status"] == "completed"
    assert done["thread_id"] == "11111111-1111-4111-8111-111111111111"
    assert done["changed_files"] == ["engine.py"]
    assert done["ownership_check"]["allowed"] is True

    # Multi-turn resume updates engine.py in the exact same worktree
    replied = mgr.reply(job_id=done["job_id"], prompt="Bump VALUE to 2")
    assert replied["status"] == "completed"
    assert len(replied["turns"]) == 2
    assert "Updated VALUE to 2" in replied["final_message"]

    # Merge via Diff Gate + auto cleanup
    merged = mgr.merge(job_id=done["job_id"], cleanup_after=True)
    assert merged["merged"] is True
    assert (repo / "engine.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert merged["cleanup"]["removed_worktree"] is True


def test_stdio_mcp_server_handshake_and_tools_from_tmp():
    run_mcp = REPO_ROOT / "scripts" / "run-mcp.sh"
    run_mcp.chmod(run_mcp.stat().st_mode | stat.S_IXUSR)
    reqs = (
        "\n".join(
            [
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "dual_route",
                            "arguments": {"task": "重构异步队列算法并补全 pytest 边界测试"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "dual_check_division",
                            "arguments": {
                                "subtasks": [
                                    {"id": "T1", "owner": "gemini", "title": "Spec", "owned_paths": ["docs/spec.md"]},
                                    {
                                        "id": "T2",
                                        "owner": "codex",
                                        "title": "Algo",
                                        "owned_paths": ["src/queue.py", "tests/"],
                                    },
                                ]
                            },
                        },
                    }
                ),
            ]
        )
        + "\n"
    )

    proc = subprocess.run(
        [str(run_mcp)],
        cwd="/tmp",
        input=reqs,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ},
    )
    assert proc.returncode == 0, proc.stderr
    replies = {json.loads(ln)["id"]: json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()}
    assert replies[1]["result"]["serverInfo"]["name"] == "aisys-dual-codex"
    tool_names = [t["name"] for t in replies[2]["result"]["tools"]]
    assert set(tool_names) == {
        "codex_doctor",
        "dual_route",
        "dual_check_division",
        "codex_spawn",
        "codex_reply",
        "codex_status",
        "codex_review",
        "codex_merge",
        "codex_cleanup",
    }
    route_res = json.loads(replies[3]["result"]["content"][0]["text"])
    assert route_res["lead_model"] == "codex"
    div_res = json.loads(replies[4]["result"]["content"][0]["text"])
    assert div_res["valid"] is True
    assert div_res["waves"] == [["T1", "T2"]]
