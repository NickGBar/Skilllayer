"""Tests for the AssessDecompositionNeedWorkflow professional skill
(build_assess_decomposition_artifacts).

Advisory only: SkillLayer never decomposes or orchestrates a task itself. This
computes deterministic localization-confidence facts from a keyword/path search
and a default recommendation the calling harness may act on or override. Covers:
a single unambiguous file match, a filename named directly (path search, not
content search), an ambiguous multi-file match, no match at all, and MCP/direct
parity.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from skilllayer.mcp_server import skilllayer_assess_decomposition
from skilllayer.runner.core import build_assess_decomposition_artifacts


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _fixture(tmp_path: Path) -> Path:
    repo = tmp_path
    _git(["init"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "widget.py").write_text("def render_widget():\n    return 'widget'\n")
    (repo / "gadget.py").write_text("def render_gadget():\n    return 'gadget'\n")
    (repo / "gizmo.py").write_text("def render_gizmo():\n    return 'gizmo'\n")
    (repo / "README.md").write_text("# sample\n")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


class TestLocalizationConfidence:
    def test_a_single_matching_file_is_high_confidence_against_decomposition(self, tmp_path):
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "Fix render_widget so it returns the right label")
        assert r["localization_confidence"] == "HIGH"
        assert r["relevant_files"] == ["widget.py"]
        assert r["recommend_decomposition"] is False

    def test_a_bare_filename_is_matched_by_path_not_just_content(self, tmp_path):
        # build_search_artifacts matches file *contents* — a literal "widget.py"
        # does not appear inside widget.py's own text, so without the path-match
        # fallback this would find nothing at all.
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "Fix the bug in `widget.py`")
        assert "widget.py" in r["relevant_files"]
        assert r["localization_confidence"] == "HIGH"

    def test_several_matches_is_medium_confidence(self, tmp_path):
        # Two files defining the same symbol name — genuinely ambiguous, unlike a
        # single definition with incidental references elsewhere (still HIGH).
        repo = _fixture(tmp_path)
        (repo / "gadget.py").write_text("def render_widget():\n    return 'gadget-widget'\n")
        _git(["add", "-A"], cwd=repo)
        _git(["commit", "-m", "ambiguous rename"], cwd=repo)
        r = build_assess_decomposition_artifacts(repo, "Fix `render_widget`")
        assert r["relevant_file_count"] >= 2
        assert r["localization_confidence"] in ("MEDIUM", "LOW")
        assert r["recommend_decomposition"] is True

    def test_a_vague_description_with_no_symbol_is_low_confidence(self, tmp_path):
        # No backticked/CamelCase symbol and no matching filename — this tool is
        # honestly reporting it doesn't know, not asserting the task is hard.
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "Fix the rendering bug")
        assert r["localization_confidence"] == "LOW"
        assert r["recommend_decomposition"] is True

    def test_no_match_is_low_confidence_not_a_crash(self, tmp_path):
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "Improve the nonexistent frobnicator subsystem")
        assert r["relevant_file_count"] == 0
        assert r["localization_confidence"] == "LOW"
        assert r["recommend_decomposition"] is True

    def test_no_searchable_term_is_low_confidence_not_a_crash(self, tmp_path):
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "the and or")
        assert r["query_terms"] == []
        assert r["localization_confidence"] == "LOW"


class TestAdvisoryOnly:
    def test_never_writes_anything(self, tmp_path):
        repo = _fixture(tmp_path)
        before = sorted(p.relative_to(repo) for p in repo.rglob("*") if p.is_file())
        build_assess_decomposition_artifacts(repo, "Fix render_widget")
        after = sorted(p.relative_to(repo) for p in repo.rglob("*") if p.is_file())
        assert before == after

    def test_result_discloses_it_is_advisory_and_never_orchestrates(self, tmp_path):
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "Fix render_widget")
        assert r["advisory_only"] is True
        assert "does not orchestrate" in r["note"]

    def test_repo_file_count_is_reported_for_scale_context(self, tmp_path):
        repo = _fixture(tmp_path)
        r = build_assess_decomposition_artifacts(repo, "Fix render_widget")
        assert r["repo_file_count"] == 4


class TestMcpParity:
    def test_mcp_wrapper_matches_direct_call(self, tmp_path):
        repo = _fixture(tmp_path)
        direct = build_assess_decomposition_artifacts(repo, "Fix render_widget")
        via_mcp = skilllayer_assess_decomposition(str(repo), "Fix render_widget")
        assert via_mcp["localization_confidence"] == direct["localization_confidence"]
        assert via_mcp["recommend_decomposition"] == direct["recommend_decomposition"]
        assert via_mcp["relevant_files"] == direct["relevant_files"]

    def test_mcp_wrapper_reports_an_error_for_a_missing_repo_path(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        r = skilllayer_assess_decomposition(str(missing), "Fix render_widget")
        assert r["success"] is False
        assert "error" in r
