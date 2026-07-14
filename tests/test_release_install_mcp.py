"""Artifact-level install and stdio MCP regression coverage.

These tests deliberately build and execute the distribution outside this
checkout; they do not accept an editable/source import as evidence of a release
working.
"""
from __future__ import annotations

import json
import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import tarfile
import threading
import venv
import zipfile
from importlib import metadata
from pathlib import Path

from skilllayer.mcp_config import build_config, validate_config
from skilllayer.mcp_server import mcp_tool_count


ROOT = Path(__file__).resolve().parents[1]


def _read_line_with_timeout(stream, timeout: float = 10.0) -> str:
    lines: queue.Queue[str] = queue.Queue()
    thread = threading.Thread(target=lambda: lines.put(stream.readline()), daemon=True)
    thread.start()
    try:
        return lines.get(timeout=timeout)
    except queue.Empty as exc:  # pragma: no cover - diagnostic assertion below
        raise AssertionError("MCP server did not respond before timeout") from exc


class _StdioMcpClient:
    def __init__(self, command: list[str], cwd: Path, *, extra_env: dict[str, str] | None = None) -> None:
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        if extra_env:
            env.update(extra_env)
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self.process.stdin is not None and self.process.stdout is not None

    def request(self, request_id: int, method: str, params: dict) -> dict:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        return json.loads(_read_line_with_timeout(self.process.stdout))

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()

    def close(self) -> tuple[int, str]:
        self.process.terminate()
        try:
            _, stderr = self.process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, stderr = self.process.communicate(timeout=10)
        return self.process.returncode, stderr


def _build_wheel_and_sdist(source: Path, destination: Path) -> tuple[Path, Path]:
    from setuptools.build_meta import build_sdist, build_wheel

    previous = Path.cwd()
    os.chdir(source)
    try:
        wheel = destination / build_wheel(str(destination))
        sdist = destination / build_sdist(str(destination))
    finally:
        os.chdir(previous)
    return wheel, sdist


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _release_allowlist() -> frozenset[str]:
    spec = importlib.util.spec_from_file_location("skilllayer_release_setup", ROOT / "setup.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PUBLIC_RUNTIME_MODULES


def _assert_top_level_modules_match_allowlist(wheel: Path, sdist: Path) -> None:
    expected = {f"skilllayer/{name}.py" for name in _release_allowlist()}
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    with tarfile.open(sdist) as archive:
        sdist_names = set(archive.getnames())
    wheel_top_level = {
        name for name in wheel_names
        if name.startswith("skilllayer/") and name.count("/") == 1 and name.endswith(".py")
    }
    sdist_top_level = {
        name.split("/src/", 1)[1]
        for name in sdist_names
        if "/src/skilllayer/" in name and name.count("/") == 3 and name.endswith(".py")
    }
    assert wheel_top_level == expected
    assert sdist_top_level == expected


def _tool_payload(response: dict) -> dict:
    """Extract FastMCP's structured result without trusting display text."""
    result = response["result"]
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for item in result.get("content", []):
        if item.get("type") == "text":
            return json.loads(item["text"])
    raise AssertionError(f"MCP tool response did not contain structured content: {response}")


def _initialize(client: _StdioMcpClient, request_id: int, *, expected_version: str | None = None) -> int:
    initialized = client.request(
        request_id,
        "initialize",
        {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "release-test", "version": "1"}},
    )
    assert initialized["result"]["serverInfo"]["name"] == "SkillLayer"
    assert initialized["result"]["serverInfo"]["version"] == (expected_version or metadata.version("skilllayer"))
    client.notify("notifications/initialized")
    return request_id + 1


def test_mcp_config_detects_relocated_executable(tmp_path: Path) -> None:
    executable = tmp_path / "python"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    config = build_config(executable, tool_count=mcp_tool_count())
    assert validate_config(config, tool_count_provider=mcp_tool_count)["ok"] is True
    executable.unlink()
    result = validate_config(config, tool_count_provider=mcp_tool_count)
    assert result["ok"] is False
    assert result["checks"]["executable_exists"] is False
    assert "Regenerate MCP config" in result["remediation"]


def test_wheel_and_sdist_install_and_real_stdio_handshake(tmp_path: Path) -> None:
    artifacts = tmp_path / "dist"
    artifacts.mkdir()
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "runs", "build", "dist"))
    wheel, sdist = _build_wheel_and_sdist(source, artifacts)
    assert wheel.exists() and sdist.exists()
    _assert_top_level_modules_match_allowlist(wheel, sdist)

    wheel_contents = subprocess.check_output([sys.executable, "-m", "zipfile", "-l", str(wheel)], text=True)
    sdist_contents = subprocess.check_output(["tar", "-tzf", str(sdist)], text=True)
    for forbidden in ("benchmark_harness", "/tests/", "runs/", "fix_failing_test_v2_validation.py"):
        assert forbidden not in wheel_contents
        assert forbidden not in sdist_contents
    assert "skilllayer/mcp_server.py" in wheel_contents
    assert "licenses/LICENSE" in wheel_contents

    # The environment is fresh for SkillLayer. The test runner's already-declared
    # MCP dependency is made available only to the child server process, without
    # installing it or allowing a checkout import to satisfy the test.
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = _venv_python(environment)
    subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], check=True, capture_output=True, text=True)

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "app.py").write_text("def greet(name):\n    return f'hello {name}'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.email", "skilllayer-test@example.invalid"], cwd=fixture, check=True)
    subprocess.run(["git", "config", "user.name", "SkillLayer Test"], cwd=fixture, check=True)
    subprocess.run(["git", "add", "app.py"], cwd=fixture, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=fixture, check=True)

    outside = tmp_path / "outside"
    outside.mkdir()
    import_path = subprocess.check_output(
        [str(python), "-c", "import skilllayer; print(skilllayer.__file__)"], cwd=outside, text=True
    ).strip()
    assert "site-packages/skilllayer" in import_path
    assert str(ROOT) not in import_path
    installed_version = subprocess.check_output([str(python), "-c", "from importlib.metadata import version; print(version('skilllayer'))"], cwd=outside, text=True).strip()
    assert subprocess.check_output([str(python), "-m", "skilllayer", "--version"], cwd=outside, text=True).strip() == installed_version
    doctor = json.loads(subprocess.check_output([str(python), "-m", "skilllayer", "doctor", "--json"], cwd=outside, text=True))
    assert doctor["required_checks"]["product_version"]["value"] == installed_version
    subprocess.run([str(python), "-m", "skilllayer", "workflows", "--json"], cwd=outside, check=True, capture_output=True, text=True)

    runtime_site_packages = subprocess.check_output(
        [sys.executable, "-c", "import site; print(site.getsitepackages()[0])"], text=True
    ).strip()
    client = _StdioMcpClient(
        [str(python), "-m", "skilllayer.mcp_server"],
        outside,
        extra_env={"PYTHONPATH": runtime_site_packages},
    )
    saved_paths: list[str] = []
    try:
        request_id = _initialize(client, 1, expected_version=installed_version)
        tools = client.request(request_id, "tools/list", {})["result"]["tools"]
        request_id += 1
        names = {tool["name"] for tool in tools}
        assert {"skilllayer_inspect_repo", "skilllayer_search", "skilllayer_run"} <= names
        assert "skilllayer_profile_execution" not in names
        assert "skilllayer_measure_memory" not in names

        inspect_result = client.request(request_id, "tools/call", {"name": "skilllayer_inspect_repo", "arguments": {"repo_path": str(fixture)}})
        search_result = client.request(request_id + 1, "tools/call", {"name": "skilllayer_search", "arguments": {"repo_path": str(fixture), "query": "greet"}})
        git_result = client.request(request_id + 2, "tools/call", {"name": "skilllayer_run", "arguments": {"repo_path": str(fixture), "task": "Git status"}})
        assert all(item["result"].get("isError") is not True for item in (inspect_result, search_result, git_result))

        save_result = _tool_payload(client.request(request_id + 3, "tools/call", {
            "name": "skilllayer_save_context",
            "arguments": {"repo_path": str(fixture), "state": "release acceptance context", "open_questions": ["does restart rehydrate?"]},
        }))
        assert save_result["success"] is True
        assert save_result["write_behavior"] == "stateful"
        saved_paths = list(save_result["written_paths"])
        assert saved_paths and all(path.startswith(".skilllayer/") for path in saved_paths)

        malformed = client.request(request_id + 4, "tools/call", {})
        assert (
            "error" in malformed
            or (
                malformed.get("method") == "notifications/message"
                and malformed.get("params", {}).get("level") == "error"
            )
        )
        assert client.request(request_id + 5, "tools/list", {})["result"]["tools"]
    finally:
        returncode, stderr = client.close()
    assert returncode is not None
    assert "Traceback" not in stderr

    # A new installed server process must rehydrate the state written by the
    # first one; no source import or shared in-process cache may satisfy this.
    restarted = _StdioMcpClient(
        [str(python), "-m", "skilllayer.mcp_server"],
        outside,
        extra_env={"PYTHONPATH": runtime_site_packages},
    )
    try:
        request_id = _initialize(restarted, 20, expected_version=installed_version)
        rehydrated = _tool_payload(restarted.request(request_id, "tools/call", {
            "name": "skilllayer_rehydrate_context",
            "arguments": {"repo_path": str(fixture), "full_context": True},
        }))
        assert rehydrated["success"] is True
        assert "release acceptance context" in rehydrated["context"]
        healthy = _tool_payload(restarted.request(request_id + 1, "tools/call", {
            "name": "skilllayer_validate_memory",
            "arguments": {"repo_path": str(fixture)},
        }))
        assert healthy["success"] is True
        assert healthy["status"] == "healthy"
    finally:
        restart_returncode, restart_stderr = restarted.close()
    assert restart_returncode is not None
    assert "Traceback" not in restart_stderr

    actual_paths = sorted(
        str(path.relative_to(fixture)).replace(os.sep, "/")
        for path in (fixture / ".skilllayer").rglob("*")
        if path.is_file()
    )
    assert actual_paths == sorted(saved_paths)
    assert not (fixture / "runs").exists()
    assert not (fixture / ".gitignore").exists()
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=fixture, text=True) == "?? .skilllayer/\n"
