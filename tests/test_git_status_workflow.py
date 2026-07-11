"""Tests for GitStatusWorkflow — stable-tier verification.

Covers the four repo states required for stable promotion:
  1. Clean repo (committed, nothing changed)
  2. Dirty repo (staged + unstaged + untracked)
  3. Detached HEAD
  4. Non-git directory

For each state: schema consistency, correct field values, non-empty summary,
and no file modification.  Integration tests verify llm_calls == 0 and
correct routing via SkillLayer().run().
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skilllayer import SkillLayer
from skilllayer.config.defaults import WORKFLOW_METADATA
from skilllayer.router import SkillRouter
from skilllayer.runner.core import build_git_status_artifacts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = frozenset({
    "workflow",
    "is_git_repo",
    "repo_root",
    "branch",
    "clean",
    "staged_count",
    "unstaged_count",
    "untracked_count",
    "changed_files",
    "diff_stat",
    "cached_diff_stat",
    "summary",
    "error_code",
})

_REQUIRED_CHANGED_FILE_FIELDS = frozenset({"path", "status", "staged", "unstaged"})


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Bare initialised git repo with user identity configured."""
    _git(["init"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=tmp_path)
    return tmp_path


@pytest.fixture
def clean_repo(git_repo: Path) -> Path:
    """One committed file, working tree clean."""
    (git_repo / "readme.txt").write_text("hello")
    _git(["add", "readme.txt"], cwd=git_repo)
    _git(["commit", "-m", "initial commit"], cwd=git_repo)
    return git_repo


@pytest.fixture
def dirty_repo(clean_repo: Path) -> Path:
    """Modified tracked file (unstaged), one new staged file, one untracked file."""
    (clean_repo / "readme.txt").write_text("modified content")       # unstaged
    (clean_repo / "staged_new.py").write_text("x = 1")               # staged new file
    _git(["add", "staged_new.py"], cwd=clean_repo)
    (clean_repo / "untracked.txt").write_text("not tracked")         # untracked
    return clean_repo


@pytest.fixture
def detached_head_repo(clean_repo: Path) -> Path:
    """Detached HEAD — git branch --show-current returns empty string."""
    _git(["checkout", "--detach"], cwd=clean_repo)
    return clean_repo


# ---------------------------------------------------------------------------
# Stability metadata
# ---------------------------------------------------------------------------


def test_workflow_stability_is_stable() -> None:
    meta = WORKFLOW_METADATA["GitStatusWorkflow"]
    assert meta["stability"] == "stable", (
        f"GitStatusWorkflow stability must be 'stable', got {meta['stability']!r}"
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def test_router_matches_git_status_phrases() -> None:
    for phrase in [
        "git status",
        "show git status",
        "what changed?",
        "summarize git changes",
        "check the working tree",
        "show repo changes",
    ]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type == "git_status", f"failed for: {phrase!r}"
        assert decision.workflow == "GitStatusWorkflow"
        assert decision.matched is True


def test_router_does_not_route_commit_to_git_status() -> None:
    for phrase in ["git commit", "stage my changes", "git push"]:
        decision = SkillRouter().route(phrase)
        assert decision.task_type != "git_status", f"should NOT match git_status: {phrase!r}"


# ---------------------------------------------------------------------------
# Schema consistency — all states must have identical top-level keys
# ---------------------------------------------------------------------------


def test_schema_clean_repo_has_all_required_fields(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"clean repo: missing field {field!r}"


def test_schema_dirty_repo_has_all_required_fields(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"dirty repo: missing field {field!r}"


def test_schema_detached_head_has_all_required_fields(detached_head_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(detached_head_repo)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"detached HEAD: missing field {field!r}"


def test_schema_non_git_has_all_required_fields(tmp_path: Path) -> None:
    artifacts, _ = build_git_status_artifacts(tmp_path)
    for field in _REQUIRED_FIELDS:
        assert field in artifacts, f"non-git: missing field {field!r}"


def test_schema_keys_identical_across_all_states(
    clean_repo: Path, dirty_repo: Path, detached_head_repo: Path, tmp_path: Path
) -> None:
    """All four states must produce the same set of top-level keys."""
    keys = [
        set(build_git_status_artifacts(clean_repo)[0].keys()),
        set(build_git_status_artifacts(dirty_repo)[0].keys()),
        set(build_git_status_artifacts(detached_head_repo)[0].keys()),
        set(build_git_status_artifacts(tmp_path)[0].keys()),
    ]
    assert keys[0] == keys[1] == keys[2] == keys[3], (
        f"Key sets differ across states: {keys}"
    )


# ---------------------------------------------------------------------------
# State 1: clean repo
# ---------------------------------------------------------------------------


def test_clean_repo_is_git_repo(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert artifacts["is_git_repo"] is True


def test_clean_repo_workflow_field(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert artifacts["workflow"] == "GitStatusWorkflow"


def test_clean_repo_clean_flag(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert artifacts["clean"] is True
    assert artifacts["staged_count"] == 0
    assert artifacts["unstaged_count"] == 0
    assert artifacts["untracked_count"] == 0
    assert artifacts["changed_files"] == []


def test_clean_repo_has_branch(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert isinstance(artifacts["branch"], str)
    assert len(artifacts["branch"]) > 0
    assert artifacts["branch"] != "(detached HEAD)"


def test_clean_repo_has_repo_root(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert artifacts["repo_root"] is not None
    assert Path(artifacts["repo_root"]).exists()


def test_clean_repo_summary_mentions_clean(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert "clean" in artifacts["summary"].lower()


def test_clean_repo_no_error_code(clean_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(clean_repo)
    assert artifacts["error_code"] is None


# ---------------------------------------------------------------------------
# State 2: dirty repo
# ---------------------------------------------------------------------------


def test_dirty_repo_is_git_repo(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert artifacts["is_git_repo"] is True


def test_dirty_repo_not_clean(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert artifacts["clean"] is False


def test_dirty_repo_staged_count(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert artifacts["staged_count"] >= 1


def test_dirty_repo_unstaged_count(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert artifacts["unstaged_count"] >= 1


def test_dirty_repo_untracked_count(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert artifacts["untracked_count"] >= 1


def test_dirty_repo_changed_files_present(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert len(artifacts["changed_files"]) >= 2


def test_dirty_repo_changed_file_schema(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    for entry in artifacts["changed_files"]:
        for field in _REQUIRED_CHANGED_FILE_FIELDS:
            assert field in entry, f"changed_files entry missing {field!r}: {entry}"
        assert isinstance(entry["staged"], bool)
        assert isinstance(entry["unstaged"], bool)
        assert isinstance(entry["path"], str) and entry["path"]
        assert isinstance(entry["status"], str) and entry["status"]


def test_dirty_repo_summary_mentions_counts(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    summary = artifacts["summary"]
    assert "staged" in summary or "unstaged" in summary, (
        f"dirty repo summary should mention staged/unstaged: {summary!r}"
    )


def test_dirty_repo_no_error_code(dirty_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(dirty_repo)
    assert artifacts["error_code"] is None


# ---------------------------------------------------------------------------
# State 3: detached HEAD
# ---------------------------------------------------------------------------


def test_detached_head_is_git_repo(detached_head_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(detached_head_repo)
    assert artifacts["is_git_repo"] is True


def test_detached_head_branch_label(detached_head_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(detached_head_repo)
    assert "detached" in artifacts["branch"].lower(), (
        f"detached HEAD branch label should contain 'detached', got {artifacts['branch']!r}"
    )


def test_detached_head_summary_accurate(detached_head_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(detached_head_repo)
    summary = artifacts["summary"].lower()
    # Old broken text — must not appear
    assert "on branch (detached)" not in summary, (
        f"summary must not say 'On branch (detached)': {artifacts['summary']!r}"
    )
    # Must mention detached state
    assert "detached" in summary, (
        f"summary must mention detached state: {artifacts['summary']!r}"
    )


def test_detached_head_clean(detached_head_repo: Path) -> None:
    # After checkout --detach on a clean repo the tree should still be clean
    artifacts, _ = build_git_status_artifacts(detached_head_repo)
    assert artifacts["clean"] is True


def test_detached_head_no_error_code(detached_head_repo: Path) -> None:
    artifacts, _ = build_git_status_artifacts(detached_head_repo)
    assert artifacts["error_code"] is None


# ---------------------------------------------------------------------------
# State 4: non-git directory — explicit error, no silent failure
# ---------------------------------------------------------------------------


def test_non_git_is_git_repo_false(tmp_path: Path) -> None:
    artifacts, _ = build_git_status_artifacts(tmp_path)
    assert artifacts["is_git_repo"] is False


def test_non_git_has_error_code(tmp_path: Path) -> None:
    artifacts, _ = build_git_status_artifacts(tmp_path)
    assert artifacts["error_code"] == "not_git_repository", (
        f"expected error_code='not_git_repository', got {artifacts['error_code']!r}"
    )


def test_non_git_summary_is_non_empty_string(tmp_path: Path) -> None:
    artifacts, _ = build_git_status_artifacts(tmp_path)
    assert isinstance(artifacts["summary"], str)
    assert len(artifacts["summary"]) > 0


def test_non_git_safe_defaults(tmp_path: Path) -> None:
    artifacts, _ = build_git_status_artifacts(tmp_path)
    assert artifacts["branch"] is None
    assert artifacts["repo_root"] is None
    assert artifacts["changed_files"] == []
    assert artifacts["staged_count"] == 0
    assert artifacts["unstaged_count"] == 0
    assert artifacts["untracked_count"] == 0


def test_non_git_counts_are_integers(tmp_path: Path) -> None:
    artifacts, _ = build_git_status_artifacts(tmp_path)
    assert isinstance(artifacts["staged_count"], int)
    assert isinstance(artifacts["unstaged_count"], int)
    assert isinstance(artifacts["untracked_count"], int)


# ---------------------------------------------------------------------------
# Zero LLM calls — purely deterministic
# ---------------------------------------------------------------------------


def test_llm_calls_zero_clean_repo(clean_repo: Path) -> None:
    result = SkillLayer().run(clean_repo, "git status")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_dirty_repo(dirty_repo: Path) -> None:
    result = SkillLayer().run(dirty_repo, "git status")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_detached_head(detached_head_repo: Path) -> None:
    result = SkillLayer().run(detached_head_repo, "git status")
    assert result["llm_calls"] == 0


def test_llm_calls_zero_non_git(tmp_path: Path) -> None:
    result = SkillLayer().run(tmp_path, "git status")
    assert result["llm_calls"] == 0


# ---------------------------------------------------------------------------
# SkillLayer().run() integration
# ---------------------------------------------------------------------------


def test_run_clean_repo_succeeds(clean_repo: Path) -> None:
    result = SkillLayer().run(clean_repo, "git status")
    assert result["success"] is True
    assert result["workflow"] == "GitStatusWorkflow"
    artifacts = result["artifacts"]
    assert artifacts["is_git_repo"] is True
    assert artifacts["clean"] is True


def test_run_dirty_repo_succeeds(dirty_repo: Path) -> None:
    result = SkillLayer().run(dirty_repo, "git status")
    assert result["success"] is True
    artifacts = result["artifacts"]
    assert artifacts["is_git_repo"] is True
    assert artifacts["clean"] is False
    assert artifacts["staged_count"] >= 1


def test_run_detached_head_succeeds(detached_head_repo: Path) -> None:
    result = SkillLayer().run(detached_head_repo, "git status")
    assert result["success"] is True
    artifacts = result["artifacts"]
    assert artifacts["is_git_repo"] is True
    assert "detached" in artifacts["branch"].lower()


def test_run_non_git_explicit_failure(tmp_path: Path) -> None:
    result = SkillLayer().run(tmp_path, "git status")
    assert result["success"] is False
    artifacts = result["artifacts"]
    assert artifacts["is_git_repo"] is False
    assert artifacts["error_code"] == "not_git_repository"


def test_run_result_always_has_artifacts(clean_repo: Path, tmp_path: Path) -> None:
    for repo in [clean_repo, tmp_path]:
        result = SkillLayer().run(repo, "git status")
        assert "artifacts" in result
        assert isinstance(result["artifacts"], dict)


# ---------------------------------------------------------------------------
# No file modification (read-only invariant)
# ---------------------------------------------------------------------------


def test_never_modifies_files_clean(clean_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    real_write = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_git_status_artifacts(clean_repo)
    assert writes == [], f"write_text was called on: {writes}"


def test_never_modifies_files_dirty(dirty_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    real_write = Path.write_text

    def spy_write(self: Path, *args: object, **kwargs: object) -> None:
        writes.append(str(self))
        return real_write(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", spy_write)
    build_git_status_artifacts(dirty_repo)
    assert writes == [], f"write_text was called on: {writes}"
