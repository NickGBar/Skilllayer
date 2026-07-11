from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_TEST_TIMEOUT_SECONDS = 300

# On timeout, a killed direct child leaves grandchildren (e.g. npm's node
# workers, pytest-xdist workers) running unless the whole process GROUP is
# terminated, not just the one PID subprocess.run()/Popen.kill() targets.
# POSIX only — os.setsid/os.killpg don't exist on Windows; that limitation
# is reported explicitly via process_group_supported rather than silently
# pretending group cleanup happened.
PROCESS_GROUP_SUPPORTED = hasattr(os, "setsid") and hasattr(os, "killpg")

# After SIGTERM to the process group, how long to wait for a graceful exit
# before escalating to SIGKILL.
_GRACEFUL_TERMINATE_WAIT_SECONDS = 3.0
# Bound on waiting for the (already SIGKILLed) process to be reaped — never
# block the caller indefinitely even in a pathological case.
_POST_KILL_REAP_TIMEOUT_SECONDS = 5.0


@dataclass
class TestRunner:
    command: list[str] | None = None
    # Configurable per instance (constructor), per SkillLayer.run() call
    # (test_timeout_seconds kwarg or config execution.test_timeout_seconds),
    # or via skilllayer.yaml. 300s comfortably covers most real test suites;
    # a large suite should raise this explicitly rather than hit a silent 60s
    # ceiling and get a confusing timeout instead of real test output.
    timeout: int = DEFAULT_TEST_TIMEOUT_SECONDS

    def detect(self, repo_path: Path) -> list[str] | None:
        if self.command:
            return self.command
        test_files = [path for path in repo_path.rglob("*.py") if path.name.startswith("test_") or "tests" in path.parts]
        if test_files:
            test_dir = self._conventional_test_dir_name(repo_path)
            if self._has_pytest_signal(repo_path) or importlib.util.find_spec("pytest") is not None:
                command = [sys.executable, "-m", "pytest", "-q"]
                if test_dir is not None:
                    command.append(test_dir)
                return command
            if test_dir is not None:
                return [sys.executable, "-m", "unittest", "discover", "-s", test_dir]
            return [sys.executable, "-m", "unittest", "discover"]
        node_command = self._detect_node_test_command(repo_path)
        if node_command:
            return node_command
        return None

    def _conventional_test_dir_name(self, repo_path: Path) -> str | None:
        """Prefer a conventional tests/ or test/ directory over a bare,
        repo-wide invocation. A bare `pytest -q` (no path) sweeps in anything
        under the repo root that looks like a test — including unrelated
        scratch/output directories a real project may have accumulated. It
        also means a test file that itself shells out to "run the tests" gets
        re-discovered and re-executed by its own nested run. Scoping to the
        conventional directory both matches what "run the tests" normally
        means and shrinks (though does not by itself eliminate — see
        _nested_pytest_guard) the self-recursion surface."""
        for name in ("tests", "test"):
            if (repo_path / name).is_dir():
                return name
        return None

    def _has_pytest_signal(self, repo_path: Path) -> bool:
        """Return True if the repo has any indicator that it uses pytest."""
        # Explicit pytest config files
        for name in ("pytest.ini", "conftest.py"):
            if (repo_path / name).exists():
                return True
        # pyproject.toml: [tool.pytest.ini_options] section or pytest as a dependency
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text(encoding="utf-8")
                if "[tool.pytest" in content:
                    return True
                # Match pytest as a quoted dependency: "pytest", "pytest>=8", etc.
                if re.search(r'["\']pytest\b', content):
                    return True
            except OSError:
                pass
        # setup.cfg: [tool:pytest] section
        setup_cfg = repo_path / "setup.cfg"
        if setup_cfg.exists():
            try:
                if "[tool:pytest]" in setup_cfg.read_text(encoding="utf-8"):
                    return True
            except OSError:
                pass
        # tox.ini: [pytest] section
        tox_ini = repo_path / "tox.ini"
        if tox_ini.exists():
            try:
                if "[pytest]" in tox_ini.read_text(encoding="utf-8"):
                    return True
            except OSError:
                pass
        # pytest binary present in the repo's own venv
        for candidate in (repo_path / ".venv" / "bin" / "pytest", repo_path / ".venv" / "Scripts" / "pytest.exe"):
            if candidate.exists():
                return True
        return False

    def _nested_pytest_guard(self, repo_path: Path, command: list[str]) -> dict[str, Any] | None:
        """Refuse to spawn a test-runner subprocess if doing so would recurse
        into the test that is calling us right now.

        Pytest sets PYTEST_CURRENT_TEST for the duration of the currently-
        running test ("path/to/test_file.py::test_name (call)"). Its presence
        alone is NOT enough to block on — nearly every call into TestRunner in
        this project's own test suite happens from inside a pytest test, most
        of them against small, unrelated tmp_path fixture repos that carry no
        recursion risk at all. Blocking on "are we inside pytest" unconditionally
        would refuse those too.

        The actual risk is narrower: recursion only happens when the
        currently-running test file lives INSIDE the repo_path we are about to
        spawn a nested test run against — because that nested run would
        rediscover and re-execute the very test calling us, which shells out
        again, forever. So we only block when the currently-running test's
        file path is inside repo_path; a call targeting some other, unrelated
        directory (e.g. a tmp_path fixture) is safe and must proceed normally.

        Scoping the test command to a tests/ directory (see
        _conventional_test_dir_name) does NOT by itself prevent this, since the
        recursive test typically lives inside that same directory.
        """
        current_test = os.environ.get("PYTEST_CURRENT_TEST")
        if not current_test:
            return None
        current_test_file = current_test.split("::", 1)[0].strip()
        try:
            current_test_path = Path(current_test_file).resolve()
            repo_path_resolved = repo_path.resolve()
            current_test_path.relative_to(repo_path_resolved)
        except (OSError, ValueError):
            # Either path couldn't be resolved, or the currently-running test
            # is not inside repo_path — not a recursion risk, let it proceed.
            return None
        return {
            "available": False,
            "command": command,
            "returncode": None,
            "test_count": None,
            "no_tests_discovered": False,
            "passed": 0,
            "failed": 0,
            "runtime_seconds": 0.0,
            "duration_ms": 0,
            "stdout": "",
            "stderr": "",
            "nested_execution_blocked": True,
            "blocked_reason": (
                "Refused to run: already executing inside a pytest test "
                f"({current_test}) whose file is inside the target repository. "
                "Running the test suite from within one of its own "
                "currently-running tests would recursively spawn nested test runs."
            ),
        }

    def _detect_node_test_command(self, repo_path: Path) -> list[str] | None:
        package_json = repo_path / "package.json"
        if not package_json.exists():
            return None
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            return None
        scripts = payload.get("scripts")
        if not isinstance(scripts, dict):
            return None
        test_script = str(scripts.get("test", "")).strip()
        if not test_script or test_script.lower() in {"echo \"error: no test specified\" && exit 1", "echo 'error: no test specified' && exit 1"}:
            return None
        if (repo_path / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
            return ["pnpm", "test"]
        if (repo_path / "yarn.lock").exists() and shutil.which("yarn"):
            return ["yarn", "test"]
        if shutil.which("npm"):
            return ["npm", "test"]
        return None

    def run(self, repo_path: Path) -> dict[str, Any]:
        command = self.detect(repo_path)
        if command is None:
            return {
                "available": False,
                "returncode": None,
                "passed": 0,
                "failed": 0,
                "stdout": "",
                "stderr": "no pytest/unittest tests detected",
            }
        return self.run_command(repo_path, command)

    def run_command(self, repo_path: Path, command: list[str], *, allow_nested: bool = False) -> dict[str, Any]:
        """Run a resolved test command.

        allow_nested=True opts out of the recursion guard for callers that
        target one specific, explicitly-named test (SingleTestWorkflow,
        MonitorTestFlakinessWorkflow with an explicit target) rather than the
        whole suite. Repeatedly running one named, unrelated test carries none
        of the "rediscovers and re-executes everything, including itself"
        recursion risk the guard exists for — only a broad, directory-or-repo
        -wide invocation does.
        """
        if not allow_nested:
            guard = self._nested_pytest_guard(repo_path, command)
            if guard is not None:
                return guard
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_pytest_addopts = env.get("PYTEST_ADDOPTS", "").strip()
        cache_disable = "-p no:cacheprovider"
        env["PYTEST_ADDOPTS"] = f"{existing_pytest_addopts} {cache_disable}".strip() if cache_disable not in existing_pytest_addopts else existing_pytest_addopts
        started = time.perf_counter()

        popen_kwargs: dict[str, Any] = {}
        if PROCESS_GROUP_SUPPORTED:
            # New session -> the child becomes its own process-group leader,
            # so any descendants it spawns (npm's node workers, pytest-xdist
            # workers, ...) share that group and can be killed as one unit.
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(
            command,
            cwd=repo_path,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
                output = f"{stdout}\n{stderr}"
                test_count = detect_test_count(output)
                no_tests_discovered = test_count == 0
                return {
                    "available": True,
                    "command": command,
                    "returncode": process.returncode,
                    "test_count": test_count,
                    "no_tests_discovered": no_tests_discovered,
                    "passed": sum(int(match) for match in re.findall(r"(\d+) passed", output)),
                    "failed": sum(int(match) for match in re.findall(r"(\d+) failed", output))
                    + sum(int(match) for match in re.findall(r"(\d+) error", output)),
                    "runtime_seconds": time.perf_counter() - started,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                }
            except subprocess.TimeoutExpired:
                partial_stdout, partial_stderr = self._terminate_and_reap(process)
                return {
                    "available": True,
                    "command": command,
                    "returncode": 124,
                    "test_count": None,
                    "no_tests_discovered": False,
                    "passed": 0,
                    "failed": 1,
                    "runtime_seconds": self.timeout,
                    "duration_ms": self.timeout * 1000,
                    # Whatever the process had already written before being
                    # killed, within the same caps as the normal path — never
                    # discarded just because the run didn't finish.
                    "stdout": partial_stdout[-4000:],
                    "stderr": (f"Test suite exceeded {self.timeout}s timeout.\n{partial_stderr}")[-4000:],
                    "timed_out": True,
                    "timeout_seconds": self.timeout,
                    "process_group_supported": PROCESS_GROUP_SUPPORTED,
                }
        finally:
            # Belt-and-suspenders: communicate() already closes these on the
            # success path, but never leave a pipe open on any other exit.
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    with contextlib.suppress(Exception):
                        pipe.close()

    def _terminate_and_reap(self, process: subprocess.Popen) -> tuple[str, str]:
        """Timeout cleanup: SIGTERM the process group (or just the direct
        child where group kill isn't supported), wait briefly for a graceful
        exit, SIGKILL if still alive, then reap — so no child or grandchild
        process is left running. Returns whatever partial stdout/stderr the
        process had already produced."""
        pgid = process.pid if PROCESS_GROUP_SUPPORTED else None

        def _signal(sig: int) -> None:
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, sig)
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    process.send_signal(sig)

        _signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=_GRACEFUL_TERMINATE_WAIT_SECONDS)
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            pass

        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

        try:
            stdout, stderr = process.communicate(timeout=_POST_KILL_REAP_TIMEOUT_SECONDS)
            return stdout or "", stderr or ""
        except subprocess.TimeoutExpired:
            # Should not happen after SIGKILL — reap without blocking the
            # caller forever if it somehow does.
            with contextlib.suppress(Exception):
                process.wait(timeout=2.0)
            return "", ""


def detect_test_count(output: str) -> int | None:
    patterns = [
        r"\bRan\s+(\d+)\s+tests?\b",
        r"\bcollected\s+(\d+)\s+items?\b",
        r"\b(\d+)\s+passed\b",
        r"\b(\d+)\s+failed\b",
        r"\b(\d+)\s+errors?\b",
        r"\b(\d+)\s+tests?\b",
        r"\bTests:\s+(\d+)\s+total\b",
        r"\bTest Suites:\s+\d+\s+\w+,\s+(\d+)\s+total\b",
    ]
    counts: list[int] = []
    lower = output.lower()
    if "no tests ran" in lower:
        return 0
    for pattern in patterns:
        for match in re.finditer(pattern, output, flags=re.IGNORECASE):
            try:
                counts.append(int(match.group(1)))
            except Exception:
                continue
    if not counts:
        return None
    return max(counts)
