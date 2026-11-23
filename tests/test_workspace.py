import os
import sys

import pytest

from aisys.backends import Edit
from aisys.workspace import (
    apply_edits,
    artifacts,
    git,
    local_file,
    prepare_spaces,
    run_check,
)


def test_git_snapshot_uses_committed_files_and_keeps_source_intact(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    (source / "solution.py").write_text("# baseline\n")
    (source / ".env").write_text("placeholder\n")
    (source / "linked.py").symlink_to(source / "solution.py")
    git(source, "add", "--all")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@localhost",
        "commit", "--no-gpg-sign", "-m", "fixture")
    (source / "solution.py").write_text("# local edit\n")
    (source / "local.txt").write_text("not in HEAD")
    destination = tmp_path / "run"
    destination.mkdir()
    assert prepare_spaces(destination, {"a", "b"}, source) == git(source, "rev-parse", "HEAD")
    for name in ("a", "b"):
        space = destination / "workspaces" / name
        assert (space / "solution.py").read_text() == "# baseline\n"
        assert not (space / "linked.py").exists()
        assert not (space / ".env").exists()
        assert not (space / "local.txt").exists()
        assert not artifacts(space)
    assert (source / "solution.py").read_text() == "# local edit\n"


def test_edit_policy_rejects_batch_before_applying(tmp_path):
    source = tmp_path / "solution.py"
    source.write_text("# existing\n")
    sibling = tmp_path / "outside"
    sibling.mkdir()
    (tmp_path / "symlink").symlink_to(sibling, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        local_file(tmp_path, "symlink/unsafe.py")
    with pytest.raises(ValueError):
        apply_edits(tmp_path, [
            Edit(path="solution.py", content="# update\n"),
            Edit(path="denied.py", content="# denied\n"),
        ], ["solution.py"])
    assert source.read_text() == "# existing\n"
    with pytest.raises(ValueError, match="deletion-like"):
        apply_edits(tmp_path, [Edit(path="solution.py", content="")], ["solution.py"])
    assert apply_edits(tmp_path, [Edit(path="solution.py", content="# update\n")],
                       ["solution.py"]) == ["solution.py"]
    assert source.read_text() == "# update\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_checks_cannot_read_sibling_or_write_project_or_bind_network(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    secret = tmp_path / "observer.txt"
    secret.write_text("integration fixture")
    (workspace / "source.py").write_text("# project\n")
    monkeypatch.setenv("AISYS_PRIVATE_TEST", "inherited credentials fixture")
    script = f"""
from pathlib import Path
import os
import socket
assert 'AISYS_PRIVATE_TEST' not in os.environ
assert Path('source.py').read_text() == '# project\\n'
def denied(operation):
    try:
        operation()
    except PermissionError:
        return
    raise AssertionError('sandbox unexpectedly allowed access')
denied(lambda: Path({str(secret)!r}).read_bytes())
denied(lambda: Path('/System/Volumes/Data' + {str(secret)!r}).read_bytes())
denied(lambda: Path('source.py').write_text('changed'))
denied(lambda: socket.socket().bind(('127.0.0.1', 0)))
Path(os.environ['TMPDIR'], 'okay').write_text('scratch works')
print('sandbox boundaries verified')
"""
    # The Data volume alias is available for user directories under /Users.
    if not os.path.exists("/System/Volumes/Data" + str(secret)):
        script = "\n".join(line for line in script.splitlines() if "/System/Volumes/Data" not in line)
    result = run_check(["python", "-c", script], workspace, 10)
    assert result.code == 0, result.stderr
    assert result.stdout.strip() == "sandbox boundaries verified"
    assert (workspace / "source.py").read_text() == "# project\n"
