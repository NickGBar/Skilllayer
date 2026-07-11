"""Focused tests for subprocess lifecycle/timeout safety
(Runtime Safety Closure Stage 5): TestRunner.run_command() and its
process-group cleanup on timeout, used by RunTests, SingleTest,
ExplainFailure, MonitorTestFlakiness, and MeasureTestSuiteSpeed.

Real subprocess tests are kept bounded: short timeouts (1-2s) against
children that sleep much longer, so termination is unambiguously required
and each test finishes quickly.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from skilllayer.verifier.test_runner import PROCESS_GROUP_SUPPORTED, TestRunner
from skilllayer.runner.core import build_measure_test_speed_artifacts


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestNormalExit:
    def test_process_exits_normally(self, tmp_path: Path) -> None:
        runner = TestRunner(timeout=10)
        result = runner.run_command(tmp_path, [sys.executable, "-c", "print('ok')"])
        assert result["available"] is True
        assert result["returncode"] == 0
        assert result.get("timed_out", False) is False
        assert "ok" in result["stdout"]


class TestDirectTimeout:
    def test_timeout_result_success_false_and_bounded(self, tmp_path: Path) -> None:
        runner = TestRunner(timeout=1)
        started = time.monotonic()
        result = runner.run_command(
            tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        elapsed = time.monotonic() - started
        assert elapsed < 15, "termination took far longer than the grace window allows"
        assert result["timed_out"] is True
        assert result["returncode"] == 124
        assert result["passed"] == 0
        assert result["failed"] == 1


class TestPartialOutputRetained:
    def test_output_printed_before_timeout_is_captured(self, tmp_path: Path) -> None:
        runner = TestRunner(timeout=1)
        script = "import sys, time; print('BEFORE_TIMEOUT_MARKER'); sys.stdout.flush(); time.sleep(30)"
        result = runner.run_command(tmp_path, [sys.executable, "-c", script])
        assert result["timed_out"] is True
        assert "BEFORE_TIMEOUT_MARKER" in result["stdout"]


@pytest.mark.skipif(not PROCESS_GROUP_SUPPORTED, reason="process-group cleanup is POSIX-only")
class TestDescendantCleanup:
    def test_grandchild_process_is_not_left_running(self, tmp_path: Path) -> None:
        marker = tmp_path / "grandchild_pid.txt"
        script = (
            "import subprocess, sys, time\n"
            f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"open({str(marker)!r}, 'w').write(str(g.pid))\n"
            "time.sleep(30)\n"
        )
        runner = TestRunner(timeout=1)
        result = runner.run_command(tmp_path, [sys.executable, "-c", script])
        assert result["timed_out"] is True

        # Give the OS a brief moment to finish reaping after our own
        # SIGKILL — the grandchild must not still be alive.
        deadline = time.monotonic() + 5
        grandchild_pid = int(marker.read_text().strip())
        while time.monotonic() < deadline and _pid_alive(grandchild_pid):
            time.sleep(0.1)
        assert not _pid_alive(grandchild_pid), (
            f"grandchild pid {grandchild_pid} is still running after parent timeout"
        )

    def test_terminate_resistant_child_falls_back_to_kill(self, tmp_path: Path) -> None:
        marker = tmp_path / "child_pid.txt"
        script = (
            "import signal, sys, time, os\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n"
        )
        runner = TestRunner(timeout=1)
        started = time.monotonic()
        result = runner.run_command(tmp_path, [sys.executable, "-c", script])
        elapsed = time.monotonic() - started
        assert result["timed_out"] is True
        # SIGTERM is ignored, so this must have escalated to SIGKILL —
        # bounded by timeout + grace window + kill-reap window, not 30s.
        assert elapsed < 15
        child_pid = int(marker.read_text().strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(child_pid):
            time.sleep(0.1)
        assert not _pid_alive(child_pid)

    def test_repeated_timeouts_leave_no_orphan_process(self, tmp_path: Path) -> None:
        runner = TestRunner(timeout=1)
        pids: list[int] = []
        for i in range(3):
            marker = tmp_path / f"pid_{i}.txt"
            script = (
                "import subprocess, sys, time\n"
                f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"open({str(marker)!r}, 'w').write(str(g.pid))\n"
                "time.sleep(30)\n"
            )
            result = runner.run_command(tmp_path, [sys.executable, "-c", script])
            assert result["timed_out"] is True
            pids.append(int(marker.read_text().strip()))

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_pid_alive(p) for p in pids):
            time.sleep(0.1)
        alive = [p for p in pids if _pid_alive(p)]
        assert alive == [], f"orphaned processes remain: {alive}"


class TestNoBaselineAfterTimeout:
    def test_baseline_not_written_after_timed_out_run(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_slow.py").write_text(
            "import time\n\ndef test_slow():\n    time.sleep(30)\n"
        )
        # build_measure_test_speed_artifacts does a local
        # `from ..verifier.test_runner import TestRunner` on each call, so
        # the patch target is the source module's attribute, not a
        # runner.core-level binding (there isn't one).
        monkeypatch.setattr(
            "skilllayer.verifier.test_runner.TestRunner",
            lambda: TestRunner(timeout=1),
        )
        result = build_measure_test_speed_artifacts(tmp_path, persist_baseline=True)
        baseline_path = tmp_path / ".skilllayer" / "test_speed_baseline.json"
        assert not baseline_path.exists()
        assert result["state_write_performed"] is False
        assert result["written_paths"] == []
