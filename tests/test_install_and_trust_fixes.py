"""Regression tests for four install/trust issues found in external testing.

1. install.sh must install the required MCP runtime and fail rather than claim
   a successful reduced installation when that extra is unavailable.
2. doctor's pytest check must reflect the running interpreter's own
   environment, not a system pytest on PATH (no false green light).
3. Setup docs must use the HTTPS clone URL, not SSH.
4. Human-mode CLI output must print a workflow's summary artifact, never just a
   success flag plus a "use --json" tip.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Fix #1 — install.sh has one explicit runtime contract
# ---------------------------------------------------------------------------

class TestInstallScriptRuntimeContract:
    def _install_sh(self) -> str:
        return (_REPO / "scripts" / "install.sh").read_text(encoding="utf-8")

    def test_primary_install_includes_required_mcp_extra(self) -> None:
        assert 'pip install ".[mcp]" --no-build-isolation' in self._install_sh()

    def test_no_successful_cli_only_fallback_remains(self) -> None:
        text = self._install_sh()
        assert "falling back" not in text
        assert "MCP extra install failed" not in text

    def test_no_editable_install_remains(self) -> None:
        text = self._install_sh()
        assert "pip install -e" not in text

    def test_dev_extra_remains_available_for_maintainers(self) -> None:
        pyproject = (_REPO / "pyproject.toml").read_text(encoding="utf-8")
        assert "dev = [" in pyproject and "pytest" in pyproject


# ---------------------------------------------------------------------------
# Fix #2 — doctor checks the interpreter's env, not PATH
# ---------------------------------------------------------------------------

class TestDoctorPytestCheck:
    def test_path_pytest_does_not_create_false_green(self, monkeypatch) -> None:
        import skilllayer.cli as cli

        # Interpreter genuinely cannot import pytest ...
        monkeypatch.setattr(cli, "module_available", lambda name: False)
        # ... but a pytest exists on PATH (the old false-green trigger).
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/pytest")
        monkeypatch.delenv("SKILLLAYER_FORCE_PYTEST_MISSING", raising=False)
        assert cli.is_pytest_available() is False

    def test_importable_pytest_reports_true(self, monkeypatch) -> None:
        import skilllayer.cli as cli

        monkeypatch.setattr(cli, "module_available", lambda name: True)
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        monkeypatch.delenv("SKILLLAYER_FORCE_PYTEST_MISSING", raising=False)
        assert cli.is_pytest_available() is True

    def test_force_missing_override_still_works(self, monkeypatch) -> None:
        import skilllayer.cli as cli

        monkeypatch.setattr(cli, "module_available", lambda name: True)
        monkeypatch.setenv("SKILLLAYER_FORCE_PYTEST_MISSING", "1")
        assert cli.is_pytest_available() is False


# ---------------------------------------------------------------------------
# Fix #3 — docs use HTTPS clone URLs, not SSH
# ---------------------------------------------------------------------------

class TestDocsUseHttpsClone:
    _DOCS = [
        "README.md",
        "INSTALL.md",
        "TESTER_GUIDE.md",
        "docs/CLAUDE_CODE_SETUP.md",
    ]

    @pytest.mark.parametrize("rel", _DOCS)
    def test_no_ssh_clone_url(self, rel: str) -> None:
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "git@github.com" not in text, f"{rel} still uses an SSH clone URL"

    @pytest.mark.parametrize("rel", _DOCS)
    def test_uses_https_clone_url(self, rel: str) -> None:
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "https://github.com/NickGBar/Skilllayer.git" in text


# ---------------------------------------------------------------------------
# Fix #4 — human output prints the summary, not just success
# ---------------------------------------------------------------------------

def _capture(result: dict) -> str:
    from skilllayer.cli import print_human_run

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_human_run(result)
    return buf.getvalue()


class TestHumanOutputPrintsSummary:
    def test_no_branch_workflow_prints_summary(self) -> None:
        result = {
            "success": True,
            "workflow": "InspectRepoStructureWorkflow",
            "macro_sequence": ["MapDirectoryTree"],
            "dry_run": False,
            "tool_calls": 0,
            "llm_calls": 0,
            "logs_path": None,
            "summary": "265 files across 19 directories.",
            "artifacts": {"summary": "265 files across 19 directories."},
        }
        out = _capture(result)
        assert "summary: 265 files across 19 directories." in out

    def test_no_branch_workflow_not_just_success_flag(self) -> None:
        result = {
            "success": True,
            "workflow": "MapDependenciesWorkflow",
            "macro_sequence": ["ScanManifests"],
            "dry_run": False,
            "tool_calls": 0,
            "llm_calls": 0,
            "logs_path": None,
            "summary": "11 dependencies.",
            "artifacts": {"summary": "11 dependencies."},
        }
        out = _capture(result)
        assert "11 dependencies." in out

    def test_unknown_workflow_summary_is_surfaced(self) -> None:
        result = {
            "success": True,
            "workflow": "SomeFutureWorkflow",
            "macro_sequence": [],
            "dry_run": False,
            "tool_calls": 0,
            "llm_calls": 0,
            "logs_path": None,
            "summary": "future workflow summary text",
            "artifacts": {"summary": "future workflow summary text"},
        }
        assert "future workflow summary text" in _capture(result)

    def test_branch_workflow_does_not_double_print_summary(self) -> None:
        # GitStatusWorkflow renders its own block; the generic fallback must not
        # add a second summary line.
        result = {
            "success": True,
            "workflow": "GitStatusWorkflow",
            "macro_sequence": ["ReadGitStatus"],
            "dry_run": False,
            "tool_calls": 1,
            "llm_calls": 0,
            "logs_path": None,
            "is_git_repo": True,
            "branch": "main",
            "clean": True,
            "summary": "branch main, clean",
            "artifacts": {"summary": "branch main, clean"},
        }
        out = _capture(result)
        assert out.count("summary:") <= 1


class TestHumanOutputIntegration:
    def test_inspect_repo_structure_end_to_end(self) -> None:
        from skilllayer import SkillLayer

        result = SkillLayer().run(_REPO, "inspect repo structure")
        out = _capture(result)
        assert "success: True" in out
        assert "summary:" in out
        # The summary line must carry real content, not be empty.
        summary_line = next(ln for ln in out.splitlines() if ln.strip().startswith("summary:"))
        assert len(summary_line.split("summary:", 1)[1].strip()) > 10
