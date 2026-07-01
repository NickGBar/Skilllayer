from __future__ import annotations

from pathlib import Path
from typing import Any

from ..verifier import TestRunner


IGNORED_PARTS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}


def inspect_repo(repo_path: str | Path, test_runner: TestRunner | None = None) -> dict[str, Any]:
    repo = Path(repo_path)
    files = [path for path in repo.rglob("*") if path.is_file() and not is_ignored(path.relative_to(repo))]
    python_files = [path for path in files if path.suffix == ".py"]
    test_files = [path for path in python_files if is_test_file(path.relative_to(repo))]
    loc = 0
    for path in python_files:
        loc += count_loc(path.read_text(encoding="utf-8", errors="ignore"))
    runner = test_runner or TestRunner()
    command = runner.detect(repo)
    return {
        "repo_path": str(repo.resolve()),
        "file_count": len(files),
        "python_file_count": len(python_files),
        "loc_estimate": loc,
        "test_files": [str(path.relative_to(repo)) for path in test_files],
        "detected_test_command": command,
    }


def is_ignored(path: Path) -> bool:
    return any(part in IGNORED_PARTS for part in path.parts)


def is_test_file(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_") or path.name.endswith("_test.py")


def count_loc(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))
