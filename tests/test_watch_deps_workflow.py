"""Tests for WatchDependencyUpdatesWorkflow (build_watch_deps_artifacts).

All network calls are mocked — no real HTTP requests are made.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.skilllayer.router.cascade import SkillRouter
from src.skilllayer.runner.core import (
    _classify_bump,
    _fetch_npm_latest,
    _fetch_pypi_latest,
    _parse_package_json,
    _parse_pyproject_toml,
    _parse_requirements_txt,
    _parse_version_parts,
    build_watch_deps_artifacts,
)


# ---------------------------------------------------------------------------
# Helpers
#
# _fetch_pypi_latest/_fetch_npm_latest return (version, error_code) tuples —
# error_code is None only on genuine success. These helpers mirror the old
# single-value call sites: pass a version string for success, or None (or an
# explicit error_code) to simulate a failure.
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content, "utf-8")


def _patch_pypi(version: str | None, error_code: str | None = None):
    if version is None and error_code is None:
        error_code = "registry_lookup_failed"
    return patch(
        "src.skilllayer.runner.core._fetch_pypi_latest",
        return_value=(version, error_code),
    )


def _patch_npm(version: str | None, error_code: str | None = None):
    if version is None and error_code is None:
        error_code = "registry_lookup_failed"
    return patch(
        "src.skilllayer.runner.core._fetch_npm_latest",
        return_value=(version, error_code),
    )


# ---------------------------------------------------------------------------
# Unit: _parse_version_parts
# ---------------------------------------------------------------------------

class TestParseVersionParts:
    def test_simple_three_part(self):
        assert _parse_version_parts("1.2.3") == (1, 2, 3)

    def test_two_part(self):
        assert _parse_version_parts("2.1") == (2, 1, 0)

    def test_caret_prefix(self):
        assert _parse_version_parts("^1.5.0") == (1, 5, 0)

    def test_tilde_prefix(self):
        assert _parse_version_parts("~2.0.1") == (2, 0, 1)

    def test_invalid_returns_zeros(self):
        assert _parse_version_parts("not-a-version") == (0, 0, 0)


# ---------------------------------------------------------------------------
# Unit: _classify_bump
# ---------------------------------------------------------------------------

class TestClassifyBump:
    def test_major_bump(self):
        maj, mnr, pat = _classify_bump("1.9.9", "2.0.0")
        assert maj is True and mnr is False and pat is False

    def test_minor_bump(self):
        maj, mnr, pat = _classify_bump("1.2.3", "1.3.0")
        assert maj is False and mnr is True and pat is False

    def test_patch_bump(self):
        maj, mnr, pat = _classify_bump("1.2.3", "1.2.4")
        assert maj is False and mnr is False and pat is True

    def test_no_bump_same_version(self):
        maj, mnr, pat = _classify_bump("1.2.3", "1.2.3")
        assert maj is False and mnr is False and pat is False


# ---------------------------------------------------------------------------
# Unit: parsers
# ---------------------------------------------------------------------------

class TestParseRequirementsTxt:
    def test_parses_pinned_deps(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\nflask==2.0.1\n")
        deps = _parse_requirements_txt(tmp_path / "requirements.txt")
        assert ("requests", "2.28.0") in deps
        assert ("flask", "2.0.1") in deps

    def test_skips_unpinned(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests>=2.0\nnumpy\nflask==1.0.0\n")
        deps = _parse_requirements_txt(tmp_path / "requirements.txt")
        names = [d[0] for d in deps]
        assert "requests" not in names
        assert "numpy" not in names
        assert "flask" in names

    def test_skips_comments(self, tmp_path):
        _write(tmp_path, "requirements.txt", "# comment\nrequests==2.0.0\n")
        deps = _parse_requirements_txt(tmp_path / "requirements.txt")
        assert len(deps) == 1

    def test_skips_options(self, tmp_path):
        _write(tmp_path, "requirements.txt", "-r other.txt\nrequests==2.0.0\n")
        deps = _parse_requirements_txt(tmp_path / "requirements.txt")
        assert all(d[0] != "-r" for d in deps)


class TestParsePackageJson:
    def test_parses_pinned_deps(self, tmp_path):
        data = {"dependencies": {"express": "^4.18.0", "lodash": "~4.17.0"}}
        _write(tmp_path, "package.json", json.dumps(data))
        deps = _parse_package_json(tmp_path / "package.json")
        names = [d[0] for d in deps]
        assert "express" in names
        assert "lodash" in names

    def test_skips_unpinned_star(self, tmp_path):
        data = {"dependencies": {"foo": "*", "bar": "latest", "baz": "^1.0.0"}}
        _write(tmp_path, "package.json", json.dumps(data))
        deps = _parse_package_json(tmp_path / "package.json")
        names = [d[0] for d in deps]
        assert "foo" not in names
        assert "bar" not in names
        assert "baz" in names

    def test_parses_dev_dependencies(self, tmp_path):
        data = {"devDependencies": {"jest": "^29.0.0"}}
        _write(tmp_path, "package.json", json.dumps(data))
        deps = _parse_package_json(tmp_path / "package.json")
        assert any(d[0] == "jest" for d in deps)

    def test_skips_git_urls(self, tmp_path):
        data = {"dependencies": {"mymod": "git+https://github.com/x/y.git"}}
        _write(tmp_path, "package.json", json.dumps(data))
        deps = _parse_package_json(tmp_path / "package.json")
        assert not any(d[0] == "mymod" for d in deps)


# ---------------------------------------------------------------------------
# Integration: outdated / up-to-date identification
# ---------------------------------------------------------------------------

class TestOutdatedDetection:
    def test_outdated_dep_correctly_identified(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.20.0\n")
        with _patch_pypi("2.28.0"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = next(d for d in result["dependencies"] if d["name"] == "requests")
        assert dep["outdated"] is True
        assert dep["status"] == "outdated"
        assert dep["latest_version"] == "2.28.0"
        assert dep["error_code"] is None

    def test_up_to_date_dep_correctly_identified(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi("2.28.0"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = next(d for d in result["dependencies"] if d["name"] == "requests")
        assert dep["outdated"] is False
        assert dep["status"] == "up_to_date"
        assert dep["major_bump"] is False
        assert dep["minor_bump"] is False
        assert dep["patch_bump"] is False

    def test_major_bump_flagged(self, tmp_path):
        _write(tmp_path, "requirements.txt", "django==1.11.0\n")
        with _patch_pypi("4.0.0"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = next(d for d in result["dependencies"] if d["name"] == "django")
        assert dep["outdated"] is True
        assert dep["major_bump"] is True
        assert dep["minor_bump"] is False
        assert dep["patch_bump"] is False

    def test_minor_bump_flagged(self, tmp_path):
        _write(tmp_path, "requirements.txt", "flask==2.0.0\n")
        with _patch_pypi("2.3.0"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = next(d for d in result["dependencies"] if d["name"] == "flask")
        assert dep["outdated"] is True
        assert dep["major_bump"] is False
        assert dep["minor_bump"] is True
        assert dep["patch_bump"] is False

    def test_patch_bump_flagged(self, tmp_path):
        _write(tmp_path, "requirements.txt", "click==8.0.0\n")
        with _patch_pypi("8.0.4"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = next(d for d in result["dependencies"] if d["name"] == "click")
        assert dep["outdated"] is True
        assert dep["major_bump"] is False
        assert dep["minor_bump"] is False
        assert dep["patch_bump"] is True


# ---------------------------------------------------------------------------
# Unknown semantics: network failures must never become outdated/up_to_date
# ---------------------------------------------------------------------------

class TestUnknownSemantics:
    def test_generic_network_failure_is_unknown_not_up_to_date(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi(None):
            result = build_watch_deps_artifacts(tmp_path)
        dep = next(d for d in result["dependencies"] if d["name"] == "requests")
        assert dep["latest_version"] is None
        assert dep["outdated"] is False
        assert dep["status"] == "unknown"
        assert dep["error_code"] is not None

    def test_registry_timeout_is_unknown(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi(None, "registry_timeout"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = result["dependencies"][0]
        assert dep["status"] == "unknown"
        assert dep["error_code"] == "registry_timeout"
        assert dep["outdated"] is False

    def test_dns_network_failure_is_unknown(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi(None, "registry_network_error"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = result["dependencies"][0]
        assert dep["status"] == "unknown"
        assert dep["outdated"] is False

    def test_malformed_response_is_unknown(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi(None, "malformed_registry_response"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = result["dependencies"][0]
        assert dep["status"] == "unknown"
        assert dep["outdated"] is False

    def test_package_not_found_is_unsupported_never_up_to_date(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi(None, "package_not_found"):
            result = build_watch_deps_artifacts(tmp_path)
        dep = result["dependencies"][0]
        assert dep["status"] == "unsupported"
        assert dep["outdated"] is False
        assert dep["latest_version"] is None

    def test_network_failure_does_not_raise(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\nflask==2.0.0\n")
        with _patch_pypi(None):
            result = build_watch_deps_artifacts(tmp_path)
        # Must return a valid result dict, not raise
        assert "dependencies" in result
        assert result["unknown_count"] == 2

    def test_partial_failure_still_returns_results(self, tmp_path):
        """One package fails, another succeeds — both should be in output."""
        _write(tmp_path, "requirements.txt", "requests==2.28.0\nflask==2.0.0\n")

        def side_effect(pkg):
            return ("2.28.0", None) if pkg == "requests" else (None, "registry_lookup_failed")

        with patch("src.skilllayer.runner.core._fetch_pypi_latest", side_effect=side_effect):
            result = build_watch_deps_artifacts(tmp_path)

        names = {d["name"]: d for d in result["dependencies"]}
        assert names["requests"]["latest_version"] == "2.28.0"
        assert names["requests"]["status"] == "up_to_date"
        assert names["flask"]["latest_version"] is None
        assert names["flask"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# Aggregate execution budget
# ---------------------------------------------------------------------------

class TestAggregateBudget:
    def test_deadline_stops_checking_remaining_dependencies_marked_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.skilllayer.runner.core.DEPENDENCY_CHECK_DEADLINE_SECONDS", 0.0)
        _write(
            tmp_path, "requirements.txt",
            "requests==2.28.0\nflask==2.0.0\ndjango==4.0.0\n",
        )
        with _patch_pypi("9.9.9"):
            result = build_watch_deps_artifacts(tmp_path)
        assert result["complete"] is False
        assert all(d["error_code"] == "dependency_check_deadline_exceeded" for d in result["dependencies"])
        assert all(d["status"] == "unknown" for d in result["dependencies"])
        assert all(d["outdated"] is False for d in result["dependencies"])
        assert result["unknown_count"] == 3
        assert result["checked_count"] == 3

    def test_within_budget_is_complete(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi("2.28.0"):
            result = build_watch_deps_artifacts(tmp_path)
        assert result["complete"] is True

    def test_large_dependency_set_under_deadline_does_not_hang(self, tmp_path, monkeypatch):
        # Regression guard for "hundreds of seconds" runtime: even with a
        # trivial per-call deadline and many dependencies, this must return
        # promptly rather than making 50 sequential real-timeout-length calls.
        monkeypatch.setattr("src.skilllayer.runner.core.DEPENDENCY_CHECK_DEADLINE_SECONDS", 0.0)
        lines = "\n".join(f"pkg{i}==1.0.0" for i in range(50))
        _write(tmp_path, "requirements.txt", lines + "\n")
        import time as _time
        started = _time.monotonic()
        result = build_watch_deps_artifacts(tmp_path)
        elapsed = _time.monotonic() - started
        assert elapsed < 5.0
        assert result["complete"] is False
        assert result["checked_count"] == 50
        assert result["unknown_count"] == 50


# ---------------------------------------------------------------------------
# Unpinned deps skipped silently
# ---------------------------------------------------------------------------

class TestUnpinnedDepsSkipped:
    def test_unpinned_not_in_dependencies(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests>=2.0\nnumpy\nflask==1.0.0\n")
        with _patch_pypi("1.1.0"):
            result = build_watch_deps_artifacts(tmp_path)
        names = [d["name"] for d in result["dependencies"]]
        assert "requests" not in names
        assert "numpy" not in names
        assert "flask" in names

    def test_npm_star_skipped(self, tmp_path):
        data = {"dependencies": {"lodash": "*", "express": "^4.18.0"}}
        _write(tmp_path, "package.json", json.dumps(data))
        with _patch_npm("4.18.2"):
            result = build_watch_deps_artifacts(tmp_path)
        names = [d["name"] for d in result["dependencies"]]
        assert "lodash" not in names
        assert "express" in names


# ---------------------------------------------------------------------------
# No requirements file
# ---------------------------------------------------------------------------

class TestNoRequirementsFile:
    def test_returns_empty_gracefully(self, tmp_path):
        result = build_watch_deps_artifacts(tmp_path)
        assert result["dependencies"] == []
        assert result["package_manager"] == "none"
        assert result["outdated_count"] == 0
        assert result["up_to_date_count"] == 0
        assert result["unknown_count"] == 0
        assert result["unsupported_count"] == 0
        assert result["checked_count"] == 0
        assert result["complete"] is True

    def test_workflow_name_correct(self, tmp_path):
        result = build_watch_deps_artifacts(tmp_path)
        assert result["workflow"] == "WatchDependencyUpdatesWorkflow"

    def test_checked_at_present(self, tmp_path):
        result = build_watch_deps_artifacts(tmp_path)
        ts = result["checked_at"]
        assert "T" in ts and ("+00:00" in ts or "Z" in ts)


# ---------------------------------------------------------------------------
# Both PyPI and npm checked when both package files exist
# ---------------------------------------------------------------------------

class TestBothManagers:
    def test_package_manager_is_both(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        _write(tmp_path, "package.json", json.dumps({"dependencies": {"express": "^4.18.0"}}))
        with _patch_pypi("2.28.0"), _patch_npm("4.18.2"):
            result = build_watch_deps_artifacts(tmp_path)
        assert result["package_manager"] == "both"

    def test_both_pypi_and_npm_deps_present(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        _write(tmp_path, "package.json", json.dumps({"dependencies": {"express": "^4.18.0"}}))
        with _patch_pypi("2.28.0"), _patch_npm("4.18.2"):
            result = build_watch_deps_artifacts(tmp_path)
        names = {d["name"] for d in result["dependencies"]}
        assert "requests" in names
        assert "express" in names

    def test_sources_correct(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        _write(tmp_path, "package.json", json.dumps({"dependencies": {"express": "^4.18.0"}}))
        with _patch_pypi("2.28.0"), _patch_npm("4.18.2"):
            result = build_watch_deps_artifacts(tmp_path)
        sources = {d["name"]: d["source"] for d in result["dependencies"]}
        assert sources["requests"] == "pypi"
        assert sources["express"] == "npm"


# ---------------------------------------------------------------------------
# Summary counts
# ---------------------------------------------------------------------------

class TestSummaryCounts:
    def test_outdated_count(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.20.0\nflask==2.0.0\n")
        with patch(
            "src.skilllayer.runner.core._fetch_pypi_latest",
            side_effect=lambda p: ("2.28.0", None) if p == "requests" else ("2.0.0", None),
        ):
            result = build_watch_deps_artifacts(tmp_path)
        assert result["outdated_count"] == 1
        assert result["up_to_date_count"] == 1
        assert result["unknown_count"] == 0
        assert result["checked_count"] == 2

    def test_unknown_count_on_failure(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi(None):
            result = build_watch_deps_artifacts(tmp_path)
        assert result["unknown_count"] == 1
        assert result["up_to_date_count"] == 0

    def test_mixed_outdated_unknown_unsupported_aggregation(self, tmp_path):
        _write(
            tmp_path, "requirements.txt",
            "requests==2.20.0\nflask==2.0.0\ndjango==1.0.0\n",
        )

        def side_effect(pkg):
            if pkg == "requests":
                return ("2.28.0", None)  # outdated
            if pkg == "flask":
                return (None, "registry_timeout")  # unknown
            return (None, "package_not_found")  # unsupported

        with patch("src.skilllayer.runner.core._fetch_pypi_latest", side_effect=side_effect):
            result = build_watch_deps_artifacts(tmp_path)
        assert result["outdated_count"] == 1
        assert result["unknown_count"] == 1
        assert result["unsupported_count"] == 1
        assert result["checked_count"] == 3
        assert result["complete"] is True


# ---------------------------------------------------------------------------
# Zero LLM calls
# ---------------------------------------------------------------------------

class TestZeroLLMCalls:
    def test_no_llm_client_instantiated(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        with _patch_pypi("2.28.0"), \
             patch("src.skilllayer.runner.core.LLMClient") as mock_cls:
            build_watch_deps_artifacts(tmp_path)
            mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# No repository writes
# ---------------------------------------------------------------------------

class TestNoRepositoryWrites:
    def test_scan_does_not_modify_files(self, tmp_path):
        _write(tmp_path, "requirements.txt", "requests==2.28.0\n")
        before = (tmp_path / "requirements.txt").read_bytes()
        with _patch_pypi("2.28.0"):
            build_watch_deps_artifacts(tmp_path)
        after = (tmp_path / "requirements.txt").read_bytes()
        assert before == after
        assert list(tmp_path.iterdir()) == [tmp_path / "requirements.txt"]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class TestRouter:
    def setup_method(self):
        self.router = SkillRouter()

    def _route(self, text: str) -> str:
        return self.router.route(text).task_type

    def test_check_for_dependency_updates(self):
        assert self._route("check for dependency updates") == "watch_deps"

    def test_are_my_dependencies_outdated(self):
        assert self._route("are my dependencies outdated") == "watch_deps"

    def test_what_dependencies_need_updating(self):
        assert self._route("what dependencies need updating") == "watch_deps"

    def test_check_for_outdated_packages(self):
        assert self._route("check for outdated packages") == "watch_deps"

    def test_are_there_newer_versions_available(self):
        assert self._route("are there newer versions available") == "watch_deps"

    def test_watch_dependency_updates(self):
        assert self._route("watch dependency updates") == "watch_deps"
