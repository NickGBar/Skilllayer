"""Tests for ProjectTools.path() repo-escape guard.

Verifies that the guard uses relative_to() rather than a string-prefix check,
so sibling directories whose names share a common prefix with the repo path
are correctly rejected.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from skilllayer.tools.execution import ProjectTools


def test_valid_path_inside_repo(tmp_path: Path) -> None:
    tools = ProjectTools(tmp_path)
    (tmp_path / "src").mkdir()
    result = tools.path("src")
    assert result == tmp_path / "src"


def test_path_traversal_blocked(tmp_path: Path) -> None:
    tools = ProjectTools(tmp_path)
    with pytest.raises(ValueError, match="path escapes repo"):
        tools.path("../../etc/passwd")


def test_sibling_with_prefix_name_blocked(tmp_path: Path) -> None:
    # This is the case the old string-prefix check missed.
    # repo_path = /tmp/pytest-xxx/test_sibling0/project
    # target    = /tmp/pytest-xxx/test_sibling0/projectevil/file.py
    # str(target).startswith(str(repo_path)) is True — wrong!
    # relative_to() correctly raises ValueError.
    repo = tmp_path / "project"
    repo.mkdir()
    sibling = tmp_path / "projectevil"
    sibling.mkdir()
    (sibling / "file.py").write_text("evil")

    tools = ProjectTools(repo)
    with pytest.raises(ValueError, match="path escapes repo"):
        tools.path("../projectevil/file.py")
