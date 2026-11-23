import signal
import subprocess
import sys
import time

import pytest

from aisys.config import Workflow
from aisys.engine import Engine
from aisys.state import Run


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
def test_sigterm_recovers_from_persisted_checkpoint(tmp_path):
    workflow = Workflow.model_validate({
        "name": "Signal recovery",
        "start": "record",
        "nodes": {
            "record": {"kind": "route", "next": "interrupted_check"},
            "interrupted_check": {
                "kind": "check", "timeout": 30,
                "argv": ["python", "-c", "import time; time.sleep(20)"],
                "routes": [{"when": {"status": "interrupted"}, "to": "approval"}],
            },
            "approval": {"kind": "approval", "next": "report"},
            "report": {"kind": "report", "inputs": ["interrupted_check"]},
        },
    })
    run = Run.create(tmp_path, workflow, "Capture durable recovery from process termination", "test", None)
    cmd = [sys.executable, "-m", "aisys", "--data", str(tmp_path), "resume", run.id]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as driver:
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if any(item["in_progress"] for item in run.events()):
                    break
                assert driver.poll() is None
                time.sleep(0.05)
            else:
                pytest.fail("child process never reserved its check")
            driver.send_signal(signal.SIGTERM)
            _, stderr = driver.communicate(timeout=6)
            assert driver.returncode == 130, stderr
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.wait(timeout=5)
    reopened = Engine(Run.open(tmp_path, run.id))
    if reopened.snapshot()["status"] == "recoverable":
        reopened.drive()
    assert reopened.snapshot()["status"] == "waiting"
    assert reopened.drive(accept=True)["status"] == "complete"
    assert [item["status"] for item in run.events()] == ["interrupted"]
