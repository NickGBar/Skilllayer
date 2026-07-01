"""Tests for CheckPortAvailabilityWorkflow."""
from __future__ import annotations

import io
import os
import socket
import threading
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.runner.core import build_check_port_artifacts, _extract_port_check_params


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ServerSocket:
    """Context manager: bind a listening TCP socket, yield its port, close on exit."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._host = host
        self._sock: socket.socket | None = None
        self.port: int = 0

    def __enter__(self) -> "_ServerSocket":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        return self

    def __exit__(self, *_: object) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None


def _free_port() -> int:
    """Return a port number that is currently free (the socket is closed before return)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

class TestSchemaFields:
    def test_available_port_schema(self) -> None:
        port = _free_port()
        result = build_check_port_artifacts(port)
        assert "workflow" in result
        assert "port" in result
        assert "host" in result
        assert "available" in result
        assert "process" in result
        assert "checked_at" in result

    def test_workflow_name(self) -> None:
        port = _free_port()
        result = build_check_port_artifacts(port)
        assert result["workflow"] == "CheckPortAvailabilityWorkflow"

    def test_tests_run_false(self) -> None:
        result = build_check_port_artifacts(_free_port())
        assert result["tests_run"] is False

    def test_tests_passed_none(self) -> None:
        result = build_check_port_artifacts(_free_port())
        assert result["tests_passed"] is None

    def test_validation_status(self) -> None:
        result = build_check_port_artifacts(_free_port())
        assert result["validation_status"] == "not_applicable"


# ---------------------------------------------------------------------------
# Available port
# ---------------------------------------------------------------------------

class TestAvailablePort:
    def test_available_is_true(self) -> None:
        port = _free_port()
        result = build_check_port_artifacts(port)
        assert result["available"] is True

    def test_process_is_none_for_free_port(self) -> None:
        port = _free_port()
        result = build_check_port_artifacts(port)
        assert result["process"] is None

    def test_port_value_returned(self) -> None:
        port = _free_port()
        result = build_check_port_artifacts(port)
        assert result["port"] == port

    def test_host_default(self) -> None:
        result = build_check_port_artifacts(_free_port())
        assert result["host"] == "127.0.0.1"

    def test_checked_at_is_iso8601(self) -> None:
        import re
        result = build_check_port_artifacts(_free_port())
        ts = result["checked_at"]
        assert isinstance(ts, str)
        # ISO 8601 with timezone: 2024-01-01T12:00:00+00:00 or ...Z
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)


# ---------------------------------------------------------------------------
# Taken port
# ---------------------------------------------------------------------------

class TestTakenPort:
    def test_available_is_false(self) -> None:
        with _ServerSocket() as srv:
            result = build_check_port_artifacts(srv.port)
        assert result["available"] is False

    def test_port_value_returned(self) -> None:
        with _ServerSocket() as srv:
            result = build_check_port_artifacts(srv.port)
        assert result["port"] == srv.port

    def test_process_is_dict_or_none(self) -> None:
        # Process info requires psutil and sufficient OS permissions.
        # May be None on permission-restricted environments.
        with _ServerSocket() as srv:
            result = build_check_port_artifacts(srv.port)
        proc = result["process"]
        assert proc is None or isinstance(proc, dict)

    def test_process_fields_when_present(self) -> None:
        with _ServerSocket() as srv:
            result = build_check_port_artifacts(srv.port)
        proc = result["process"]
        if proc is not None:
            assert "pid" in proc
            assert "name" in proc
            assert "user" in proc
            assert isinstance(proc["pid"], int)
            assert isinstance(proc["name"], str)
            assert isinstance(proc["user"], str)

    def test_process_pid_is_current_process(self) -> None:
        with _ServerSocket() as srv:
            result = build_check_port_artifacts(srv.port)
        proc = result["process"]
        if proc is not None:
            assert proc["pid"] == os.getpid()

    def test_checked_at_present(self) -> None:
        with _ServerSocket() as srv:
            result = build_check_port_artifacts(srv.port)
        assert result.get("checked_at") is not None


# ---------------------------------------------------------------------------
# Invalid port
# ---------------------------------------------------------------------------

class TestInvalidPort:
    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    def test_invalid_port_returns_error_code(self, port: int) -> None:
        result = build_check_port_artifacts(port)
        assert result.get("error_code") == "invalid_port"

    @pytest.mark.parametrize("port", [0, -1, 65536])
    def test_invalid_port_has_error_message(self, port: int) -> None:
        result = build_check_port_artifacts(port)
        assert isinstance(result.get("error"), str)
        assert len(result["error"]) > 0

    @pytest.mark.parametrize("port", [0, -1, 65536])
    def test_invalid_port_no_available_field(self, port: int) -> None:
        result = build_check_port_artifacts(port)
        # available should be absent or None for error cases
        assert result.get("available") is None

    def test_valid_boundary_ports(self) -> None:
        # 1 and 65535 are valid (we just check they don't return invalid_port)
        r1 = build_check_port_artifacts(1)
        assert r1.get("error_code") != "invalid_port"
        r2 = build_check_port_artifacts(65535)
        assert r2.get("error_code") != "invalid_port"


# ---------------------------------------------------------------------------
# Host parameter
# ---------------------------------------------------------------------------

class TestHostParameter:
    def test_default_host(self) -> None:
        result = build_check_port_artifacts(_free_port())
        assert result["host"] == "127.0.0.1"

    def test_custom_host_reflected_in_result(self) -> None:
        port = _free_port()
        result = build_check_port_artifacts(port, host="127.0.0.1")
        assert result["host"] == "127.0.0.1"

    def test_localhost_alias(self) -> None:
        # Test that localhost resolves (may be 127.0.0.1 or ::1 depending on OS)
        port = _free_port()
        result = build_check_port_artifacts(port, host="localhost")
        assert result["host"] == "localhost"
        # Should not error — just a probe
        assert "available" in result


# ---------------------------------------------------------------------------
# psutil unavailable fallback
# ---------------------------------------------------------------------------

class TestPsutilFallback:
    def test_no_process_info_when_psutil_missing(self) -> None:
        with _ServerSocket() as srv:
            with patch.dict("sys.modules", {"psutil": None}):
                result = build_check_port_artifacts(srv.port)
        assert result["process"] is None
        assert "note" in result
        assert "psutil" in result["note"].lower()

    def test_process_null_note_present(self) -> None:
        with _ServerSocket() as srv:
            import builtins
            real_import = builtins.__import__

            def mock_import(name: str, *args: object, **kwargs: object) -> object:
                if name == "psutil":
                    raise ImportError("mocked absence")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                result = build_check_port_artifacts(srv.port)
        assert result["process"] is None
        assert result.get("note") is not None


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_builder_does_not_call_skilllayer(self) -> None:
        from skilllayer import SkillLayer
        with patch.object(SkillLayer, "run", side_effect=AssertionError("SkillLayer.run called")):
            result = build_check_port_artifacts(_free_port())
        assert result["workflow"] == "CheckPortAvailabilityWorkflow"


# ---------------------------------------------------------------------------
# Port extractor helper
# ---------------------------------------------------------------------------

class TestExtractPortParams:
    def test_extracts_port_number(self) -> None:
        port, host = _extract_port_check_params("check port 8080")
        assert port == 8080

    def test_extracts_port_from_is_port_free(self) -> None:
        port, host = _extract_port_check_params("is port 3000 free")
        assert port == 3000

    def test_default_host(self) -> None:
        _, host = _extract_port_check_params("check port 8080")
        assert host == "127.0.0.1"

    def test_extracts_custom_host(self) -> None:
        _, host = _extract_port_check_params("check port 8080 on 10.0.0.1")
        assert host == "10.0.0.1"

    def test_no_port_returns_none(self) -> None:
        port, _ = _extract_port_check_params("no number here at all xyz abc")
        assert port is None

    def test_dev_server_phrase(self) -> None:
        port, _ = _extract_port_check_params("is the dev server running on port 8080")
        assert port == 8080


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def _route(self, text: str) -> tuple[str, float] | None:
        from skilllayer.router.cascade import SkillRouter
        return SkillRouter()._match_task_type(text)  # type: ignore[attr-defined]

    def test_is_port_free(self) -> None:
        assert self._route("is port 8080 free") == ("check_port", 0.92)

    def test_check_port(self) -> None:
        assert self._route("check port 3000") == ("check_port", 0.92)

    def test_anything_running_on_port(self) -> None:
        assert self._route("is anything running on 5432") == ("check_port", 0.92)

    def test_dev_server_running(self) -> None:
        assert self._route("is the dev server running on 8080") == ("check_port", 0.92)

    def test_port_availability_phrase(self) -> None:
        assert self._route("check port availability") == ("check_port", 0.92)

    def test_port_in_use(self) -> None:
        assert self._route("is port 5000 in use") == ("check_port", 0.92)

    def test_unrelated_does_not_match(self) -> None:
        result = self._route("run all tests")
        assert result != ("check_port", 0.92)


# ---------------------------------------------------------------------------
# Full runner integration
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parent.parent


class TestRunnerIntegration:
    def test_free_port_via_runner(self) -> None:
        from skilllayer import SkillLayer
        port = _free_port()
        result = SkillLayer().run(_REPO, f"check port {port}")
        assert result["success"] is True
        assert result.get("available") is True
        assert result.get("process") is None

    def test_taken_port_via_runner(self) -> None:
        from skilllayer import SkillLayer
        with _ServerSocket() as srv:
            result = SkillLayer().run(_REPO, f"check port {srv.port}")
        assert result["success"] is True
        assert result.get("available") is False

    def test_runner_zero_llm_calls(self) -> None:
        from skilllayer import SkillLayer
        port = _free_port()
        result = SkillLayer().run(_REPO, f"check port {port}")
        assert result.get("llm_calls", 0) == 0


# ---------------------------------------------------------------------------
# CLI human output
# ---------------------------------------------------------------------------

def _make_port_result(**overrides: object) -> dict:
    base: dict = {
        "success": True,
        "repo_path": "/tmp/repo",
        "task": "check port 8080",
        "workflow": "CheckPortAvailabilityWorkflow",
        "macro_sequence": [],
        "validation_status": "not_applicable",
        "tests_run": False,
        "tests_passed": None,
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
        "port": 8080,
        "host": "127.0.0.1",
        "available": True,
        "process": None,
        "checked_at": "2026-01-01T12:00:00+00:00",
    }
    base.update(overrides)
    return base


class TestCLIHumanOutput:
    def _capture(self, result: dict) -> str:
        from skilllayer.cli import print_human_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_human_run(result)
        return buf.getvalue()

    def test_port_shown(self) -> None:
        out = self._capture(_make_port_result())
        assert "8080" in out

    def test_host_shown(self) -> None:
        out = self._capture(_make_port_result())
        assert "127.0.0.1" in out

    def test_available_status_shown(self) -> None:
        out = self._capture(_make_port_result(available=True))
        assert "available" in out

    def test_in_use_status_shown(self) -> None:
        out = self._capture(_make_port_result(available=False))
        assert "in use" in out

    def test_process_info_shown_when_present(self) -> None:
        out = self._capture(_make_port_result(
            available=False,
            process={"pid": 1234, "name": "python3", "user": "alice"},
        ))
        assert "1234" in out
        assert "python3" in out
        assert "alice" in out

    def test_checked_at_shown(self) -> None:
        out = self._capture(_make_port_result())
        assert "2026-01-01" in out

    def test_note_shown_when_present(self) -> None:
        out = self._capture(_make_port_result(
            available=False,
            note="psutil not installed; process details unavailable.",
        ))
        assert "psutil" in out
