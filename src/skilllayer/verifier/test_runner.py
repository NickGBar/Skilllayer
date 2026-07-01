from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TestRunner:
    command: list[str] | None = None
    timeout: int = 60

    def detect(self, repo_path: Path) -> list[str] | None:
        if self.command:
            return self.command
        test_files = [path for path in repo_path.rglob("*.py") if path.name.startswith("test_") or "tests" in path.parts]
        if test_files:
            if self._has_pytest_signal(repo_path) or importlib.util.find_spec("pytest") is not None:
                return [sys.executable, "-m", "pytest", "-q"]
            return [sys.executable, "-m", "unittest", "discover"]
        node_command = self._detect_node_test_command(repo_path)
        if node_command:
            return node_command
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

    def run_command(self, repo_path: Path, command: list[str]) -> dict[str, Any]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_pytest_addopts = env.get("PYTEST_ADDOPTS", "").strip()
        cache_disable = "-p no:cacheprovider"
        env["PYTEST_ADDOPTS"] = f"{existing_pytest_addopts} {cache_disable}".strip() if cache_disable not in existing_pytest_addopts else existing_pytest_addopts
        started = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                cwd=repo_path,
                env=env,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
            output = f"{result.stdout}\n{result.stderr}"
            test_count = detect_test_count(output)
            no_tests_discovered = test_count == 0
            return {
                "available": True,
                "command": command,
                "returncode": result.returncode,
                "test_count": test_count,
                "no_tests_discovered": no_tests_discovered,
                "passed": sum(int(match) for match in re.findall(r"(\d+) passed", output)),
                "failed": sum(int(match) for match in re.findall(r"(\d+) failed", output))
                + sum(int(match) for match in re.findall(r"(\d+) error", output)),
                "runtime_seconds": time.perf_counter() - started,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            }
        except subprocess.TimeoutExpired:
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
                "stdout": "",
                "stderr": "test command timed out",
                "timed_out": True,
            }


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
