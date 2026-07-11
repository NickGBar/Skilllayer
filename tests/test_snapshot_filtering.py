"""Tests for snapshot_python_files filtering behavior."""

from __future__ import annotations

from pathlib import Path

from skilllayer.runner.core import snapshot_python_files


def test_venv_files_excluded(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1")
    venv = tmp_path / ".venv" / "lib" / "python3.13" / "site-packages" / "pkg"
    venv.mkdir(parents=True)
    (venv / "module.py").write_text("# dependency")

    snapshot = snapshot_python_files(tmp_path)

    assert "src/app.py" in snapshot
    assert not any(".venv" in k for k in snapshot)


def test_pycache_files_excluded(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1")
    cache = tmp_path / "src" / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-313.pyc").write_bytes(b"\x00")

    snapshot = snapshot_python_files(tmp_path)

    assert "src/app.py" in snapshot
    assert not any("__pycache__" in k for k in snapshot)


def test_nonexistent_repo_returns_empty(tmp_path: Path) -> None:
    assert snapshot_python_files(tmp_path / "does_not_exist") == {}
