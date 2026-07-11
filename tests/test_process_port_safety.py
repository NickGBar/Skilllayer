"""Focused tests for Process and Port Safety (Runtime Safety Closure Stage 4).

Complements test_detect_processes_workflow.py and test_check_port_workflow.py.
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import psutil
import pytest

from skilllayer.mcp_server import skilllayer_check_port, skilllayer_detect_processes
from skilllayer.runner.core import build_check_port_artifacts, build_detect_processes_artifacts


# ---------------------------------------------------------------------------
# Process iterator failures
# ---------------------------------------------------------------------------

class TestProcessIteratorFailure:
    def test_top_level_iterator_permission_error_is_structured(self) -> None:
        with patch("psutil.process_iter", side_effect=psutil.AccessDenied(1, "denied")):
            result = build_detect_processes_artifacts()
        assert result["success"] is False
        assert result["status"] == "unsupported"
        assert result["error_code"] == "process_inspection_unavailable"
        assert result["processes"] == []
        assert result["permission_limited"] is True
        assert result["complete"] is False

    def test_top_level_iterator_oserror_is_structured_not_raised(self) -> None:
        with patch("psutil.process_iter", side_effect=OSError("blocked")):
            result = build_detect_processes_artifacts()
        assert result["success"] is False
        assert result["status"] == "unsupported"

    def test_mid_iteration_failure_is_counted_and_skipped(self) -> None:
        good = MagicMock(spec=psutil.Process)
        good.pid = 1000
        good.info = {
            "pid": 1000, "name": "python3", "status": "running", "username": "alice",
            "memory_info": MagicMock(rss=1024), "create_time": 1_700_000_000.0,
        }
        good.cpu_percent.return_value = 0.0
        good.net_connections.return_value = []

        class _FlakyIterator:
            def __init__(self, items):
                self._items = list(items)
                self._raised = False

            def __iter__(self):
                return self

            def __next__(self):
                if not self._raised:
                    self._raised = True
                    raise psutil.AccessDenied(9999, "denied mid-iteration")
                if self._items:
                    return self._items.pop(0)
                raise StopIteration

        with patch("psutil.process_iter", return_value=_FlakyIterator([good])):
            result = build_detect_processes_artifacts()
        assert result["success"] is True
        assert len(result["processes"]) == 1
        assert result["skipped_count"] == 1
        assert result["permission_limited"] is True
        assert result["complete"] is False

    def test_empty_process_environment_is_complete_and_healthy(self) -> None:
        with patch("psutil.process_iter", return_value=iter([])):
            result = build_detect_processes_artifacts()
        assert result["success"] is True
        assert result["processes"] == []
        assert result["skipped_count"] == 0
        assert result["permission_limited"] is False
        assert result["complete"] is True

    def test_no_raw_traceback_through_direct_mcp(self, tmp_path) -> None:
        with patch("psutil.process_iter", side_effect=psutil.AccessDenied(1, "denied")):
            result = skilllayer_detect_processes(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "process_inspection_unavailable"


# ---------------------------------------------------------------------------
# Port checks: loopback restriction
# ---------------------------------------------------------------------------

def _loopback_bind_capability() -> tuple[bool, str]:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        return False, f"loopback bind is unavailable in this environment: {exc}"
    finally:
        probe.close()
    return True, ""


_LOOPBACK_BIND_AVAILABLE, _LOOPBACK_SKIP_REASON = _loopback_bind_capability()

def _free_port() -> int:
    if not _LOOPBACK_BIND_AVAILABLE:
        pytest.skip(_LOOPBACK_SKIP_REASON)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestLoopbackRestriction:
    def test_loopback_127_0_0_1_succeeds(self) -> None:
        result = build_check_port_artifacts(_free_port(), host="127.0.0.1")
        assert result["success"] is True
        assert "available" in result

    def test_loopback_localhost_succeeds(self) -> None:
        result = build_check_port_artifacts(_free_port(), host="localhost")
        assert result["success"] is True

    def test_non_loopback_host_denied_by_default(self) -> None:
        result = build_check_port_artifacts(80, host="8.8.8.8")
        assert result["success"] is False
        assert result["error_code"] == "non_loopback_target_not_allowed"
        assert "available" not in result

    def test_non_loopback_host_allowed_with_explicit_opt_in(self) -> None:
        # Authorized, but the target host/port need not actually respond —
        # this only proves the gate itself is bypassed, not real connectivity.
        result = build_check_port_artifacts(1, host="127.0.0.1", allow_non_loopback=True)
        assert result.get("error_code") != "non_loopback_target_not_allowed"

    def test_direct_mcp_denies_non_loopback_by_default(self, tmp_path) -> None:
        result = skilllayer_check_port(str(tmp_path), 80, host="8.8.8.8")
        assert result["success"] is False
        assert result["error_code"] == "non_loopback_target_not_allowed"

    def test_direct_mcp_never_silently_probes_remote_host(self, tmp_path) -> None:
        with patch("socket.socket") as mock_socket:
            skilllayer_check_port(str(tmp_path), 80, host="93.184.216.34")
        mock_socket.assert_not_called()


# ---------------------------------------------------------------------------
# Socket errors -> structured results, never raised
# ---------------------------------------------------------------------------

class TestSocketErrors:
    def test_socket_creation_oserror_is_structured(self) -> None:
        with patch("socket.socket", side_effect=OSError("too many open files")):
            result = build_check_port_artifacts(32123, host="127.0.0.1")
        assert result["success"] is False
        assert result["error_code"] == "port_check_socket_error"

    def test_connect_permission_error_is_structured_not_raised(self) -> None:
        fake_sock = MagicMock()
        fake_sock.connect_ex.side_effect = PermissionError("denied")
        with patch("socket.socket", return_value=fake_sock):
            result = build_check_port_artifacts(32123, host="127.0.0.1")
        assert result["success"] is False
        assert result["error_code"] == "port_check_socket_error"

    def test_no_raw_traceback_through_direct_mcp(self, tmp_path) -> None:
        with patch("socket.socket", side_effect=OSError("boom")):
            result = skilllayer_check_port(str(tmp_path), 32123)
        assert result["success"] is False
        assert result["error_code"] == "port_check_socket_error"


# ---------------------------------------------------------------------------
# No repository writes
# ---------------------------------------------------------------------------

class TestNoRepositoryWrites:
    def test_detect_processes_writes_nothing(self, tmp_path) -> None:
        before = list(tmp_path.iterdir())
        build_detect_processes_artifacts()
        assert list(tmp_path.iterdir()) == before

    def test_check_port_writes_nothing(self, tmp_path) -> None:
        before = list(tmp_path.iterdir())
        build_check_port_artifacts(_free_port())
        assert list(tmp_path.iterdir()) == before
