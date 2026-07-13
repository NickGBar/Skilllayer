from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shlex
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
_PYTHON_PROBE_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class PythonInterpreterSelection:
    """The interpreter decision for one target repository, with evidence."""

    selected_python: str | None
    selection_source: str
    target_environment_detected: bool
    target_environment_usable: bool
    fallback_used: bool
    warnings: tuple[str, ...] = ()
    error_code: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected_python": self.selected_python,
            "selection_source": self.selection_source,
            "target_environment_detected": self.target_environment_detected,
            "target_environment_usable": self.target_environment_usable,
            "fallback_used": self.fallback_used,
            "warnings": list(self.warnings),
            "error_code": self.error_code,
            "error": self.error,
        }


def _empty_remediation() -> dict[str, Any]:
    return {
        "remediation_available": False,
        "remediation_type": "none",
        "remediation_summary": None,
        "remediation_command": None,
        "remediation_commands": [],
        "remediation_argv": None,
        "remediation_notes": [],
        "requires_user_confirmation": False,
        "mutates_environment": False,
        "confidence": "none",
    }


def _render_remediation_command(argv: list[str]) -> str:
    """Render fixed argv safely for display; execution always uses argv."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _dependency_metadata(repo_path: Path) -> dict[str, Any]:
    """Read a small, fixed set of manifests without interpreting commands."""
    files = {
        name: (repo_path / name).is_file()
        for name in (
            "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
            "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "uv.lock",
            "pdm.lock", "environment.yml", "pyproject.toml",
        )
    }
    pyproject_text = ""
    if files["pyproject.toml"]:
        try:
            pyproject_text = (repo_path / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")[:200_000].lower()
        except OSError:
            pass
    manager: str | None = None
    if files["poetry.lock"] or "[tool.poetry" in pyproject_text or "poetry-core" in pyproject_text:
        manager = "poetry"
    elif files["uv.lock"] or "[tool.uv" in pyproject_text:
        manager = "uv"
    elif files["pdm.lock"] or "[tool.pdm" in pyproject_text:
        manager = "pdm"
    return {"files": files, "manager": manager}


def _preferred_dependency_command(repo_path: Path, selected_python: str | None) -> tuple[list[str] | None, str, str]:
    metadata = _dependency_metadata(repo_path)
    files = metadata["files"]
    if selected_python:
        for filename in ("requirements-test.txt", "requirements-dev.txt", "requirements.txt"):
            if files[filename]:
                return [selected_python, "-m", "pip", "install", "-r", filename], "install_project_dependencies", filename
    manager = metadata["manager"]
    if manager == "poetry":
        return ["poetry", "install"], "install_project_dependencies", "Poetry metadata"
    if manager == "uv":
        return ["uv", "sync"], "install_project_dependencies", "uv metadata"
    if manager == "pdm":
        return ["pdm", "install"], "install_project_dependencies", "PDM metadata"
    return None, "manual_review_required", "no supported dependency metadata"


def _safe_missing_module(output: str) -> str | None:
    match = re.search(r"No module named ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]", output)
    return match.group(1) if match else None


def build_environment_remediation(repo_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Return advice only; never creates environments or runs package tools."""
    environment = result.get("execution_environment") or {}
    code = result.get("environment_error_code") or environment.get("error_code")
    selected_python = result.get("selected_python") or environment.get("selected_python")
    if not code:
        return _empty_remediation()

    def mutating(argv: list[str], remediation_type: str, summary: str, confidence: str, notes: list[str]) -> dict[str, Any]:
        return {
            "remediation_available": True,
            "remediation_type": remediation_type,
            "remediation_summary": summary,
            "remediation_command": _render_remediation_command(argv),
            "remediation_commands": [_render_remediation_command(argv)],
            "remediation_argv": argv,
            "remediation_notes": notes + ["SkillLayer did not run this command. Run it only after reviewing it, then retry validation."],
            "requires_user_confirmation": True,
            "mutates_environment": True,
            "confidence": confidence,
        }

    if code == "pytest_not_installed_in_selected_environment":
        argv, remediation_type, evidence = _preferred_dependency_command(repo_path, selected_python)
        if argv:
            return mutating(
                argv, remediation_type,
                "pytest is missing from the selected target Python environment.",
                "high" if evidence != "no supported dependency metadata" else "medium",
                [f"Recommendation is based on {evidence}."],
            )
        if selected_python:
            return mutating(
                [selected_python, "-m", "pip", "install", "pytest"],
                "install_pytest_in_target_environment",
                "pytest is missing from the selected target Python environment.",
                "high",
                ["No project dependency manifest gave a more specific installation command."],
            )

    if code == "dependency_import_failed_before_collection":
        output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
        module = _safe_missing_module(output)
        argv, remediation_type, evidence = _preferred_dependency_command(repo_path, selected_python)
        if argv:
            note = f"Missing import observed: {module}." if module else "An import failed before collection."
            return mutating(
                argv, remediation_type,
                "A dependency import failed before tests started; code correctness was not established.",
                "medium",
                [note, f"Recommendation is based on {evidence}; SkillLayer did not guess a package from the import name."],
            )
        remediation = _empty_remediation()
        remediation.update({
            "remediation_available": bool(module and selected_python),
            "remediation_type": "inspect_import_failure",
            "remediation_summary": "A dependency import failed before tests started; inspect project dependency metadata before installing anything.",
            "remediation_command": _render_remediation_command([selected_python, "-c", f"import importlib.util; print(importlib.util.find_spec({module!r}))"]) if module and selected_python else None,
            "remediation_commands": [_render_remediation_command([selected_python, "-c", f"import importlib.util; print(importlib.util.find_spec({module!r}))"])] if module and selected_python else [],
            "remediation_argv": [selected_python, "-c", f"import importlib.util; print(importlib.util.find_spec({module!r}))"] if module and selected_python else None,
            "remediation_notes": ["SkillLayer did not infer a package name from the missing import.", "Review declared project dependencies, then retry validation."],
            "confidence": "low",
        })
        return remediation

    if code in {"target_python_unusable", "interpreter_launch_failed"}:
        candidate = selected_python
        if code == "target_python_unusable":
            for _, path in TestRunner._target_python_candidates(repo_path):
                if path.exists() or path.is_symlink():
                    candidate = str(path)
                    break
        argv = [candidate, "-c", "import sys; print(sys.executable)"] if candidate else None
        remediation = _empty_remediation()
        remediation.update({
            "remediation_available": bool(argv),
            "remediation_type": "manual_review_required",
            "remediation_summary": "The target Python environment could not be executed; repair or recreate it manually before validation.",
            "remediation_command": _render_remediation_command(argv) if argv else None,
            "remediation_commands": [_render_remediation_command(argv)] if argv else [],
            "remediation_argv": argv,
            "remediation_notes": ["This is a verification command only; SkillLayer will not recreate or modify the environment."],
            "confidence": "high",
        })
        return remediation

    remediation = _empty_remediation()
    remediation.update({
        "remediation_type": "manual_review_required",
        "remediation_summary": "The test command used an explicit or unknown Python environment; no safe install command can be inferred.",
        "remediation_notes": ["Review the selected interpreter and project dependency metadata, then retry validation."],
        "confidence": "low",
    })
    return remediation


@dataclass
class TestRunner:
    command: list[str] | None = None
    # Configurable per instance (constructor), per SkillLayer.run() call
    # (test_timeout_seconds kwarg or config execution.test_timeout_seconds),
    # or via skilllayer.yaml. 300s comfortably covers most real test suites;
    # a large suite should raise this explicitly rather than hit a silent 60s
    # ceiling and get a confusing timeout instead of real test output.
    timeout: int = DEFAULT_TEST_TIMEOUT_SECONDS

    @staticmethod
    def _target_python_candidates(repo_path: Path) -> tuple[tuple[str, Path], ...]:
        return (
            ("target_dot_venv", repo_path / ".venv" / "bin" / "python"),
            ("target_venv", repo_path / "venv" / "bin" / "python"),
            ("target_env", repo_path / "env" / "bin" / "python"),
            ("target_dot_venv", repo_path / ".venv" / "Scripts" / "python.exe"),
            ("target_venv", repo_path / "venv" / "Scripts" / "python.exe"),
            ("target_env", repo_path / "env" / "Scripts" / "python.exe"),
        )

    def select_python_interpreter(self, repo_path: Path) -> PythonInterpreterSelection:
        """Choose a usable project-local Python without creating or changing it.

        A local environment wins only after a short executable probe.  A venv's
        python is commonly a symlink to the base interpreter outside the repo;
        that is disclosed but accepted because the selected *environment entry
        point* itself is project-local.
        """
        repo_path = repo_path.resolve()
        detected = False
        rejected: list[str] = []
        warnings: list[str] = []
        for source, candidate in self._target_python_candidates(repo_path):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            detected = True
            try:
                if not candidate.is_file() or not os.access(candidate, os.X_OK):
                    rejected.append(f"{candidate}: not an executable regular file")
                    continue
                resolved = candidate.resolve()
                try:
                    resolved.relative_to(repo_path)
                except ValueError:
                    warnings.append("target_python_symlink_resolves_outside_repository")
                probe = subprocess.run(
                    [str(candidate), "-c", "import sys; print(sys.executable)"],
                    cwd=repo_path,
                    text=True,
                    capture_output=True,
                    timeout=_PYTHON_PROBE_TIMEOUT_SECONDS,
                    check=False,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    return PythonInterpreterSelection(
                        selected_python=str(candidate),
                        selection_source=source,
                        target_environment_detected=True,
                        target_environment_usable=True,
                        fallback_used=False,
                        warnings=tuple(warnings),
                    )
                detail = (probe.stderr or probe.stdout or f"probe exited {probe.returncode}").strip()
                rejected.append(f"{candidate}: {detail[:300]}")
            except (OSError, subprocess.TimeoutExpired) as exc:
                rejected.append(f"{candidate}: {exc}")

        if detected:
            warnings.append("target_python_unusable_fallback_to_current")
            return PythonInterpreterSelection(
                selected_python=sys.executable,
                selection_source="current_interpreter",
                target_environment_detected=True,
                target_environment_usable=False,
                fallback_used=True,
                warnings=tuple(warnings),
                error_code="target_python_unusable",
                error="; ".join(rejected) or "Target repository Python environment could not be probed.",
            )
        return PythonInterpreterSelection(
            selected_python=sys.executable,
            selection_source="current_interpreter",
            target_environment_detected=False,
            target_environment_usable=False,
            fallback_used=True,
        )

    @staticmethod
    def non_python_environment() -> dict[str, Any]:
        return PythonInterpreterSelection(
            selected_python=None,
            selection_source="non_python_command",
            target_environment_detected=False,
            target_environment_usable=False,
            fallback_used=False,
        ).as_dict()

    @staticmethod
    def _is_python_test_command(command: list[str]) -> bool:
        return len(command) >= 3 and command[1:3] in (["-m", "pytest"], ["-m", "unittest"])

    def _environment_for_command(self, repo_path: Path, command: list[str]) -> dict[str, Any]:
        if not self._is_python_test_command(command):
            return self.non_python_environment()
        selection = self.select_python_interpreter(repo_path).as_dict()
        selected = selection.get("selected_python")
        if selected and os.path.abspath(command[0]) == os.path.abspath(str(selected)):
            return selection
        # Explicit caller-supplied Python commands are executed as supplied;
        # do not silently rewrite them.  Report that fact rather than claiming
        # a target environment was selected.
        return {
            "selected_python": command[0],
            "selection_source": "execution_environment_unknown",
            "target_environment_detected": selection["target_environment_detected"],
            "target_environment_usable": selection["target_environment_usable"],
            "fallback_used": False,
            "warnings": ["explicit_python_test_command_not_rewritten"],
            "error_code": None,
            "error": None,
        }

    @staticmethod
    def _classify_environment_result(repo_path: Path, result: dict[str, Any]) -> dict[str, Any]:
        output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"
        lower = output.lower()
        collection_started = bool(re.search(r"\bcollected\s+\d+\s+items?\b", output, re.I))
        tests_started = bool(
            re.search(r"\b(?:\d+\s+(?:passed|failed|skipped)|ran\s+\d+\s+tests?)\b", lower)
        )
        environment_error = False
        environment_error_code: str | None = None
        environment_error_message: str | None = None
        if "no module named pytest" in lower:
            environment_error = True
            environment_error_code = "pytest_not_installed_in_selected_environment"
            environment_error_message = "pytest is not installed in the selected Python environment."
        elif not tests_started and ("modulenotfounderror" in lower or "importerror" in lower):
            environment_error = True
            environment_error_code = "dependency_import_failed_before_collection"
            environment_error_message = "An import failed before test collection; code correctness was not established."
        result["collection_started"] = collection_started
        result["tests_started"] = tests_started and not environment_error
        result["environment_error"] = environment_error
        result["environment_error_code"] = environment_error_code
        result["environment_error_message"] = environment_error_message
        result["remediation"] = build_environment_remediation(repo_path, result)
        return result

    def detect(self, repo_path: Path) -> list[str] | None:
        if self.command:
            return self.command
        test_files = [path for path in repo_path.rglob("*.py") if path.name.startswith("test_") or "tests" in path.parts]
        if test_files:
            test_dir = self._conventional_test_dir_name(repo_path)
            selected_python = self.select_python_interpreter(repo_path).selected_python or sys.executable
            if self._has_pytest_signal(repo_path) or importlib.util.find_spec("pytest") is not None:
                command = [selected_python, "-m", "pytest", "-q"]
                if test_dir is not None:
                    command.append(test_dir)
                return command
            if test_dir is not None:
                return [selected_python, "-m", "unittest", "discover", "-s", test_dir]
            return [selected_python, "-m", "unittest", "discover"]
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

    def run_command(
        self,
        repo_path: Path,
        command: list[str],
        *,
        allow_nested: bool = False,
        execution_environment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a resolved test command.

        allow_nested=True opts out of the recursion guard for callers that
        target one specific, explicitly-named test (SingleTestWorkflow,
        MonitorTestFlakinessWorkflow with an explicit target) rather than the
        whole suite. Repeatedly running one named, unrelated test carries none
        of the "rediscovers and re-executes everything, including itself"
        recursion risk the guard exists for — only a broad, directory-or-repo
        -wide invocation does.
        """
        execution_environment = execution_environment or self._environment_for_command(repo_path, command)
        if not allow_nested:
            guard = self._nested_pytest_guard(repo_path, command)
            if guard is not None:
                guard.update(execution_environment=execution_environment, **execution_environment)
                guard["remediation"] = build_environment_remediation(repo_path, guard)
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

        try:
            process = subprocess.Popen(
                command,
                cwd=repo_path,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **popen_kwargs,
            )
        except OSError as exc:
            launch_result = {
                "available": False, "command": command, "returncode": None,
                "test_count": None, "no_tests_discovered": False, "passed": 0, "failed": 0,
                "runtime_seconds": time.perf_counter() - started,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "stdout": "", "stderr": str(exc), "collection_started": False,
                "tests_started": False, "environment_error": True,
                "environment_error_code": "interpreter_launch_failed",
                "environment_error_message": "The selected test interpreter could not be launched.",
                "execution_environment": execution_environment, **execution_environment,
            }
            launch_result["remediation"] = build_environment_remediation(repo_path, launch_result)
            return launch_result
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
                output = f"{stdout}\n{stderr}"
                test_count = detect_test_count(output)
                no_tests_discovered = test_count == 0
                return self._classify_environment_result(repo_path, {
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
                    "execution_environment": execution_environment,
                    **execution_environment,
                })
            except subprocess.TimeoutExpired:
                partial_stdout, partial_stderr = self._terminate_and_reap(process)
                return self._classify_environment_result(repo_path, {
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
                    "execution_environment": execution_environment,
                    **execution_environment,
                })
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
