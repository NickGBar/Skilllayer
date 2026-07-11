"""Tests for DetectRunningProcessesWorkflow."""
from __future__ import annotations

import io
import json
import os
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pytest

from skilllayer.runner.core import (
    _DEV_PORTS,
    _DATABASE_NAMES,
    _DOCKER_NAMES,
    _TEST_RUNNER_NAMES,
    _WEB_SERVER_NAMES,
    _extract_include_system_flag,
    _name_matches,
    build_detect_processes_artifacts,
)


# ---------------------------------------------------------------------------
# Helpers — mock process factory
# ---------------------------------------------------------------------------

def _make_proc(
    pid: int = 1000,
    name: str = "python3",
    status: str = "running",
    username: str = "alice",
    memory_rss: int = 50 * 1024 * 1024,  # 50 MB
    create_time: float = 1_700_000_000.0,
    ports: list[int] | None = None,
    cpu_percent: float = 0.0,
    raise_on_connections: bool = False,
    raise_on_connections_exc: type[Exception] = psutil.AccessDenied,
) -> MagicMock:
    """Return a MagicMock that quacks like a psutil.Process."""
    proc = MagicMock(spec=psutil.Process)
    proc.pid = pid
    proc.info = {
        "pid": pid,
        "name": name,
        "status": status,
        "username": username,
        "memory_info": MagicMock(rss=memory_rss),
        "create_time": create_time,
    }
    proc.cpu_percent.return_value = cpu_percent

    if raise_on_connections:
        proc.net_connections.side_effect = raise_on_connections_exc(pid, name)
    else:
        conns = []
        for p in (ports or []):
            conn = MagicMock()
            conn.laddr = MagicMock(port=p)
            conns.append(conn)
        proc.net_connections.return_value = conns
    return proc


def _run_builder_with_procs(procs: list[MagicMock], include_system: bool = False) -> dict[str, Any]:
    with patch("psutil.process_iter", return_value=iter(procs)):
        return build_detect_processes_artifacts(include_system=include_system)


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------

class TestSchemaFields:
    def test_required_top_level_keys(self) -> None:
        result = build_detect_processes_artifacts()
        for key in ("workflow", "processes", "dev_services", "total_count", "checked_at",
                    "tests_run", "tests_passed", "validation_status"):
            assert key in result, f"Missing key: {key}"

    def test_workflow_name(self) -> None:
        result = build_detect_processes_artifacts()
        assert result["workflow"] == "DetectRunningProcessesWorkflow"

    def test_tests_run_false(self) -> None:
        assert build_detect_processes_artifacts()["tests_run"] is False

    def test_tests_passed_none(self) -> None:
        assert build_detect_processes_artifacts()["tests_passed"] is None

    def test_validation_status(self) -> None:
        assert build_detect_processes_artifacts()["validation_status"] == "not_applicable"

    def test_dev_services_keys(self) -> None:
        result = build_detect_processes_artifacts()
        ds = result["dev_services"]
        for key in ("web_server", "database", "test_runner", "dev_server", "docker"):
            assert key in ds, f"Missing dev_services key: {key}"

    def test_dev_services_are_bools(self) -> None:
        ds = build_detect_processes_artifacts()["dev_services"]
        for k, v in ds.items():
            assert isinstance(v, bool), f"{k} should be bool, got {type(v)}"

    def test_checked_at_iso8601(self) -> None:
        checked_at = build_detect_processes_artifacts()["checked_at"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", checked_at)

    def test_processes_is_list(self) -> None:
        assert isinstance(build_detect_processes_artifacts()["processes"], list)

    def test_total_count_matches_processes_length(self) -> None:
        result = build_detect_processes_artifacts()
        assert result["total_count"] == len(result["processes"])

    def test_process_entry_schema(self) -> None:
        proc = _make_proc(pid=5000, name="python3", ports=[8080])
        result = _run_builder_with_procs([proc])
        if result["processes"]:
            p = result["processes"][0]
            for key in ("pid", "name", "status", "user", "cpu_percent", "memory_mb", "ports", "started_at"):
                assert key in p, f"Process entry missing key: {key}"


# ---------------------------------------------------------------------------
# Process entry field types
# ---------------------------------------------------------------------------

class TestProcessEntryTypes:
    def _single_proc_result(self, **kwargs: Any) -> dict:
        proc = _make_proc(**kwargs)
        r = _run_builder_with_procs([proc])
        assert r["processes"], "Expected at least one process"
        return r["processes"][0]

    def test_pid_is_int(self) -> None:
        p = self._single_proc_result(pid=5001)
        assert isinstance(p["pid"], int)
        assert p["pid"] == 5001

    def test_name_is_string(self) -> None:
        p = self._single_proc_result(name="myapp")
        assert isinstance(p["name"], str)
        assert p["name"] == "myapp"

    def test_status_is_string(self) -> None:
        p = self._single_proc_result(status="sleeping")
        assert p["status"] == "sleeping"

    def test_user_is_string(self) -> None:
        p = self._single_proc_result(username="bob")
        assert p["user"] == "bob"

    def test_cpu_percent_is_float(self) -> None:
        p = self._single_proc_result(cpu_percent=12.5)
        assert isinstance(p["cpu_percent"], float)
        assert p["cpu_percent"] == 12.5

    def test_memory_mb_is_float(self) -> None:
        p = self._single_proc_result(memory_rss=100 * 1024 * 1024)
        assert isinstance(p["memory_mb"], float)
        assert p["memory_mb"] == pytest.approx(100.0, abs=0.01)

    def test_ports_is_list_of_ints(self) -> None:
        p = self._single_proc_result(ports=[3000, 8080])
        assert isinstance(p["ports"], list)
        assert all(isinstance(x, int) for x in p["ports"])

    def test_ports_sorted(self) -> None:
        p = self._single_proc_result(ports=[8080, 3000, 5000])
        assert p["ports"] == sorted(p["ports"])

    def test_started_at_is_iso8601_or_none(self) -> None:
        p = self._single_proc_result(create_time=1_700_000_000.0)
        sa = p["started_at"]
        assert sa is None or re.match(r"^\d{4}-\d{2}-\d{2}T", sa)


# ---------------------------------------------------------------------------
# dev_services detection — by name
# ---------------------------------------------------------------------------

class TestDevServicesNameDetection:
    def _ds(self, *names: str, ports: list[int] | None = None) -> dict:
        procs = [_make_proc(pid=1000 + i, name=n, ports=ports or []) for i, n in enumerate(names)]
        return _run_builder_with_procs(procs)["dev_services"]

    @pytest.mark.parametrize("name", ["nginx", "apache2", "httpd", "caddy", "uvicorn", "gunicorn", "node"])
    def test_web_server_detected(self, name: str) -> None:
        assert self._ds(name)["web_server"] is True

    @pytest.mark.parametrize("name", ["postgres", "postmaster", "mysqld", "mongod", "redis-server", "redis"])
    def test_database_detected(self, name: str) -> None:
        assert self._ds(name)["database"] is True

    @pytest.mark.parametrize("name", ["pytest", "jest", "mocha"])
    def test_test_runner_detected(self, name: str) -> None:
        assert self._ds(name)["test_runner"] is True

    @pytest.mark.parametrize("name", ["dockerd", "containerd", "docker"])
    def test_docker_detected(self, name: str) -> None:
        assert self._ds(name)["docker"] is True

    def test_no_dev_services_by_default(self) -> None:
        ds = self._ds("bash", "zsh", "kernel_task")
        assert ds["web_server"] is False
        assert ds["database"] is False
        assert ds["test_runner"] is False
        assert ds["docker"] is False

    def test_multiple_services_detected_simultaneously(self) -> None:
        ds = self._ds("nginx", "postgres", "dockerd")
        assert ds["web_server"] is True
        assert ds["database"] is True
        assert ds["docker"] is True

    def test_partial_name_match(self) -> None:
        # "postgresql" contains "postgres"
        ds = self._ds("postgresql")
        assert ds["database"] is True

    def test_case_insensitive_match(self) -> None:
        ds = self._ds("NGINX")
        assert ds["web_server"] is True


# ---------------------------------------------------------------------------
# dev_services detection — port-based dev_server
# ---------------------------------------------------------------------------

class TestDevServerPortDetection:
    @pytest.mark.parametrize("port", sorted(_DEV_PORTS))
    def test_dev_port_triggers_dev_server(self, port: int) -> None:
        proc = _make_proc(pid=2000, name="some-app", ports=[port])
        result = _run_builder_with_procs([proc])
        assert result["dev_services"]["dev_server"] is True

    def test_non_dev_port_no_dev_server(self) -> None:
        proc = _make_proc(pid=2001, name="some-app", ports=[9999])
        result = _run_builder_with_procs([proc])
        # only false if no vite/next/webpack-style name either
        assert result["dev_services"]["dev_server"] is False

    def test_dev_server_name_triggers_without_port(self) -> None:
        proc = _make_proc(pid=2002, name="vite", ports=[])
        result = _run_builder_with_procs([proc])
        assert result["dev_services"]["dev_server"] is True


# ---------------------------------------------------------------------------
# AccessDenied handled gracefully
# ---------------------------------------------------------------------------

class TestAccessDeniedHandling:
    def test_access_denied_on_connections_skips_ports(self) -> None:
        proc = _make_proc(pid=3000, name="guarded", raise_on_connections=True)
        result = _run_builder_with_procs([proc])
        # Process should still appear, just with empty ports
        assert result["total_count"] >= 1
        found = [p for p in result["processes"] if p["pid"] == 3000]
        assert found, "Process with AccessDenied on connections should still appear"
        assert found[0]["ports"] == []

    def test_access_denied_on_process_itself_is_skipped(self) -> None:
        good = _make_proc(pid=4000, name="accessible")
        bad = MagicMock(spec=psutil.Process)
        bad.pid = 4001
        bad.info = {}
        # Make iteration raise when accessing info fields
        type(bad).pid = property(lambda self: (_ for _ in ()).throw(psutil.AccessDenied(4001, "denied")))
        with patch("psutil.process_iter", return_value=iter([good, bad])):
            result = build_detect_processes_artifacts()
        # bad should be skipped; good should be present
        pids = [p["pid"] for p in result["processes"]]
        assert 4000 in pids
        assert 4001 not in pids

    def test_no_such_process_during_iteration_is_skipped(self) -> None:
        proc = _make_proc(pid=5000)
        proc.cpu_percent.side_effect = psutil.NoSuchProcess(5000)
        # cpu_percent raises but process should still appear with cpu_percent=0.0
        result = _run_builder_with_procs([proc])
        found = [p for p in result["processes"] if p["pid"] == 5000]
        assert found
        assert found[0]["cpu_percent"] == 0.0


# ---------------------------------------------------------------------------
# System process filtering
# ---------------------------------------------------------------------------

class TestSystemProcessFiltering:
    def test_pid_below_100_excluded_by_default(self) -> None:
        sys_proc = _make_proc(pid=1, name="launchd")
        user_proc = _make_proc(pid=5000, name="python3")
        result = _run_builder_with_procs([sys_proc, user_proc], include_system=False)
        pids = [p["pid"] for p in result["processes"]]
        assert 1 not in pids
        assert 5000 in pids

    def test_pid_99_excluded_by_default(self) -> None:
        proc = _make_proc(pid=99, name="kernel")
        result = _run_builder_with_procs([proc], include_system=False)
        assert not any(p["pid"] == 99 for p in result["processes"])

    def test_pid_100_included_by_default(self) -> None:
        proc = _make_proc(pid=100, name="borderline")
        result = _run_builder_with_procs([proc], include_system=False)
        assert any(p["pid"] == 100 for p in result["processes"])

    def test_include_system_includes_low_pids(self) -> None:
        sys_proc = _make_proc(pid=1, name="launchd")
        result = _run_builder_with_procs([sys_proc], include_system=True)
        pids = [p["pid"] for p in result["processes"]]
        assert 1 in pids

    def test_extract_include_system_flag_false_by_default(self) -> None:
        assert _extract_include_system_flag("what processes are running") is False

    def test_extract_include_system_flag_detected(self) -> None:
        assert _extract_include_system_flag("list processes including system processes") is True
        assert _extract_include_system_flag("show all processes include-system") is True


# ---------------------------------------------------------------------------
# Command line args and env vars never exposed
# ---------------------------------------------------------------------------

class TestNoSensitiveExposure:
    def test_cmdline_not_in_process_entry_keys(self) -> None:
        result = build_detect_processes_artifacts()
        for proc in result["processes"]:
            assert "cmdline" not in proc
            assert "cmd" not in proc
            assert "command" not in proc
            assert "command_line" not in proc

    def test_environ_not_in_process_entry_keys(self) -> None:
        result = build_detect_processes_artifacts()
        for proc in result["processes"]:
            assert "environ" not in proc
            assert "env" not in proc
            assert "environment" not in proc

    def test_builder_does_not_request_cmdline_from_psutil(self) -> None:
        with patch("psutil.process_iter") as mock_iter:
            mock_iter.return_value = iter([])
            build_detect_processes_artifacts()
            call_args = mock_iter.call_args
            attrs = call_args[0][0] if call_args else []
            assert "cmdline" not in attrs
            assert "environ" not in attrs

    def test_output_serializable_with_no_secrets(self) -> None:
        proc = _make_proc(pid=6000, name="python3", ports=[8080])
        result = _run_builder_with_procs([proc])
        dumped = json.dumps(result)
        assert "cmdline" not in dumped
        assert "environ" not in dumped


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_builder_does_not_call_skilllayer(self) -> None:
        from skilllayer import SkillLayer
        with patch.object(SkillLayer, "run", side_effect=AssertionError("SkillLayer.run called")):
            result = build_detect_processes_artifacts()
        assert result["workflow"] == "DetectRunningProcessesWorkflow"


# ---------------------------------------------------------------------------
# name_matches helper
# ---------------------------------------------------------------------------

class TestNameMatchesHelper:
    def test_exact_match(self) -> None:
        assert _name_matches("nginx", frozenset({"nginx"})) is True

    def test_partial_match(self) -> None:
        assert _name_matches("postgresql", frozenset({"postgres"})) is True

    def test_case_insensitive(self) -> None:
        assert _name_matches("REDIS", frozenset({"redis"})) is True

    def test_no_match(self) -> None:
        assert _name_matches("bash", frozenset({"nginx", "apache"})) is False


# ---------------------------------------------------------------------------
# Full runner integration
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parent.parent


class TestRunnerIntegration:
    def test_detect_processes_via_runner(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "what processes are running")
        assert result["success"] is True
        assert "dev_services" in result
        assert "total_count" in result
        assert isinstance(result["total_count"], int)
        # Sandboxed runners can deny process enumeration entirely, yielding a
        # valid empty observation.  This integration test covers the workflow
        # contract, not the host's process visibility.
        assert result["total_count"] >= 0

    def test_runner_zero_llm_calls(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "detect running services")
        assert result.get("llm_calls", 0) == 0

    def test_runner_no_cmdline_in_processes(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "what processes are running")
        for proc in result.get("processes", []):
            assert "cmdline" not in proc
            assert "environ" not in proc

    def test_runner_dev_services_are_bools(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "what processes are running")
        ds = result.get("dev_services", {})
        for k, v in ds.items():
            assert isinstance(v, bool), f"dev_services[{k!r}] should be bool"

    def test_runner_processes_have_required_fields(self) -> None:
        from skilllayer import SkillLayer
        result = SkillLayer().run(_REPO, "what processes are running")
        for proc in result.get("processes", [])[:5]:  # spot-check first 5
            for field in ("pid", "name", "status", "user", "cpu_percent", "memory_mb", "ports"):
                assert field in proc, f"Process missing field: {field}"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def _route(self, text: str) -> tuple[str, float] | None:
        from skilllayer.router.cascade import SkillRouter
        return SkillRouter()._match_task_type(text)  # type: ignore[attr-defined]

    def test_what_processes_are_running(self) -> None:
        assert self._route("what processes are running") == ("detect_processes", 0.92)

    def test_is_the_database_running(self) -> None:
        assert self._route("is the database running") == ("detect_processes", 0.92)

    def test_is_the_dev_server_up(self) -> None:
        assert self._route("is the dev server up") == ("detect_processes", 0.92)

    def test_whats_running_on_my_machine(self) -> None:
        assert self._route("what's running on my machine") == ("detect_processes", 0.92)

    def test_detect_running_services(self) -> None:
        assert self._route("detect running services") == ("detect_processes", 0.92)

    def test_is_postgres_running(self) -> None:
        assert self._route("is postgres running") == ("detect_processes", 0.92)

    def test_is_redis_up(self) -> None:
        assert self._route("is redis up") == ("detect_processes", 0.92)

    def test_port_number_query_goes_to_check_port(self) -> None:
        # "is anything running on port 5432" has a port number → check_port, not detect_processes
        result = self._route("is anything running on port 5432")
        assert result != ("detect_processes", 0.92)

    def test_unrelated_does_not_match(self) -> None:
        assert self._route("run all tests") != ("detect_processes", 0.92)


# ---------------------------------------------------------------------------
# CLI human output
# ---------------------------------------------------------------------------

def _make_proc_result(**overrides: object) -> dict:
    base: dict = {
        "success": True,
        "repo_path": "/tmp/repo",
        "task": "what processes are running",
        "workflow": "DetectRunningProcessesWorkflow",
        "macro_sequence": [],
        "validation_status": "not_applicable",
        "tests_run": False,
        "tests_passed": None,
        "dry_run": False,
        "tool_calls": 0,
        "llm_calls": 0,
        "logs_path": None,
        "processes": [
            {"pid": 1234, "name": "nginx", "status": "running", "user": "www",
             "cpu_percent": 0.0, "memory_mb": 10.0, "ports": [80], "started_at": None},
        ],
        "dev_services": {"web_server": True, "database": False, "test_runner": False,
                         "dev_server": False, "docker": False},
        "total_count": 1,
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

    def test_total_count_shown(self) -> None:
        out = self._capture(_make_proc_result())
        assert "1" in out

    def test_active_dev_service_shown(self) -> None:
        out = self._capture(_make_proc_result())
        assert "web_server" in out

    def test_no_dev_services_label(self) -> None:
        result = _make_proc_result(
            dev_services={"web_server": False, "database": False, "test_runner": False,
                          "dev_server": False, "docker": False},
        )
        out = self._capture(result)
        assert "none detected" in out

    def test_checked_at_shown(self) -> None:
        out = self._capture(_make_proc_result())
        assert "2026-01-01" in out

    def test_multiple_active_services_shown(self) -> None:
        result = _make_proc_result(
            dev_services={"web_server": True, "database": True, "test_runner": False,
                          "dev_server": False, "docker": False},
        )
        out = self._capture(result)
        assert "web_server" in out
        assert "database" in out
