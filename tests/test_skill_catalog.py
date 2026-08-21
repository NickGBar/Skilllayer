"""Tests for the Skills v0.1 catalog registration (docs/SKILLS.md).

Release Readiness and Safe Code Change are pre-existing, already-implemented
workflows (build_release_readiness_artifacts / build_safe_change_artifacts) —
this only covers their new discovery metadata in skilllayer_list_skills(), not
their execution. Covers: catalog shape parity with verified_task_execution,
required fields present, and that registration changed no runtime behavior
(mcp_tool_count is unaffected; the underlying tools still execute identically).
"""
from __future__ import annotations

from skilllayer.mcp_server import mcp_tool_count, skilllayer_list_skills

REQUIRED_FIELDS = (
    "name", "purpose", "activation_examples", "non_activation_examples",
    "required_mcp_tools", "safety_guarantees", "known_limitations",
)


class TestCatalogMembership:
    def test_four_skills_registered(self):
        names = {s["name"] for s in skilllayer_list_skills()["professional_skills"]}
        assert names == {
            "verified_task_execution", "release_readiness", "safe_code_change", "codebase_health",
        }

    def test_registration_added_exactly_one_new_mcp_tool(self):
        # Part A (release_readiness, safe_code_change) was discovery metadata
        # only. Part B adds codebase_health, a genuinely new tool composing
        # existing primitives — one new MCP tool, not new primitives.
        assert mcp_tool_count() == 50


class TestCatalogShape:
    def _entry(self, name: str) -> dict:
        catalog = {s["name"]: s for s in skilllayer_list_skills()["professional_skills"]}
        return catalog[name]

    def test_release_readiness_has_required_fields(self):
        entry = self._entry("release_readiness")
        for field in REQUIRED_FIELDS:
            assert field in entry, f"missing {field}"
        assert entry["required_mcp_tools"] == ["skilllayer_release_readiness"]
        assert entry["supported_modes"] == ["bounded", "deep"]

    def test_safe_code_change_has_required_fields(self):
        entry = self._entry("safe_code_change")
        for field in REQUIRED_FIELDS:
            assert field in entry, f"missing {field}"
        assert entry["required_mcp_tools"] == ["skilllayer_safe_change"]
        assert entry["supported_lifecycle"] == ["plan", "validate"]

    def test_codebase_health_has_required_fields(self):
        entry = self._entry("codebase_health")
        for field in REQUIRED_FIELDS:
            assert field in entry, f"missing {field}"
        assert entry["required_mcp_tools"] == ["skilllayer_codebase_health"]
        assert entry["supported_modes"] == ["bounded", "deep"]

    def test_non_activation_examples_cross_reference_the_other_skills(self):
        # Each skill's own "don't pick me for this" list should include the
        # others' territory — the whole point of a catalog is telling them apart.
        readiness = self._entry("release_readiness")
        change = self._entry("safe_code_change")
        health = self._entry("codebase_health")
        assert any("change" in ex.lower() for ex in readiness["non_activation_examples"])
        assert any("release" in ex.lower() for ex in change["non_activation_examples"])
        assert any("release" in ex.lower() for ex in health["non_activation_examples"])


class TestNoRuntimeChange:
    def test_release_readiness_tool_still_returns_its_own_verdict_shape(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_release_readiness

        result = skilllayer_release_readiness(str(tmp_path))
        assert "verdict" in result
        assert result["skill"] == "release_readiness"

    def test_safe_change_tool_still_returns_its_own_verdict_shape(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_safe_change

        result = skilllayer_safe_change(str(tmp_path), "fix something", phase="plan")
        assert "verdict" in result
        assert result["skill"] == "safe_code_change"

    def test_codebase_health_tool_returns_its_own_verdict_shape(self, tmp_path):
        from skilllayer.mcp_server import skilllayer_codebase_health

        result = skilllayer_codebase_health(str(tmp_path))
        assert "verdict" in result
        assert result["skill"] == "codebase_health"
