"""Regression tests for two accuracy fixes found in external testing.

Fix #2: `skilllayer stats` must visibly label savings numbers as estimates in the
human-readable output, not just via the JSON `estimated_savings_only` flag.

Fix #3: the README must not state the Gartner cost claim as an unsourced flat
fact; absent a verifiable source in this repo it is softened to an attributed
"industry analysts have predicted" prediction.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Fix #2 — savings numbers are labelled as estimates in human output
# ---------------------------------------------------------------------------

class TestStatsEstimateLabeling:
    def test_fmt_estimate_shape(self) -> None:
        from skilllayer.cli import _fmt_estimate

        assert _fmt_estimate(20) == "~20 (estimated, not measured)"

    def _stats_output(self, monkeypatch) -> str:
        import skilllayer.cli as cli

        fake = {
            "total_runs": 5,
            "successful_runs": 4,
            "tool_invocations_total": 9,
            "estimated_llm_calls_saved": 20,
            "estimated_tool_steps_saved": 37,
            "estimated_savings_only": True,
            "workflow_breakdown": {"GitStatusWorkflow": 3},
            "tool_usage_breakdown": {"skilllayer_run": 5},
        }
        monkeypatch.setattr(cli, "summarize_telemetry", lambda: fake)

        class _Args:
            json = False

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.handle_stats(_Args())
        return buf.getvalue()

    def test_llm_calls_saved_labelled_as_estimate(self, monkeypatch) -> None:
        out = self._stats_output(monkeypatch)
        assert "estimated_llm_calls_saved: ~20 (estimated, not measured)" in out

    def test_tool_steps_saved_labelled_as_estimate(self, monkeypatch) -> None:
        out = self._stats_output(monkeypatch)
        assert "estimated_tool_steps_saved: ~37 (estimated, not measured)" in out

    def test_bare_unlabelled_number_not_shown(self, monkeypatch) -> None:
        out = self._stats_output(monkeypatch)
        # The old bare "estimated_llm_calls_saved: 20" (no estimate label) must be gone.
        assert "estimated_llm_calls_saved: 20\n" not in out


# ---------------------------------------------------------------------------
# Fix #3 — README Gartner claim is not an unsourced flat fact
# ---------------------------------------------------------------------------

class TestReadmeGartnerClaim:
    # README_PUBLIC.md is the canonical-repo source of truth and does not
    # ship in the public export (which only carries the derived README.md) —
    # check whichever of the two actually exist in this checkout instead of
    # hardcoding both, so this test doesn't need editing per environment.
    _READMES = [rel for rel in ("README.md", "README_PUBLIC.md") if (_REPO / rel).exists()]

    @pytest.mark.parametrize("rel", _READMES)
    def test_no_flat_gartner_prediction(self, rel: str) -> None:
        text = (_REPO / rel).read_text(encoding="utf-8")
        assert "Gartner predicts" not in text, (
            f"{rel} states the Gartner claim as an unsourced flat fact"
        )

    @pytest.mark.parametrize("rel", _READMES)
    def test_claim_is_attributed_or_sourced(self, rel: str) -> None:
        text = (_REPO / rel).read_text(encoding="utf-8")
        # Either softened attribution, a source, or an explicit statement that
        # the README makes no token-cost/savings claim at all.
        softened = "industry analysts have predicted" in text.lower()
        sourced = "gartner.com" in text.lower()
        no_claim = "does not claim to prove" in text.lower() and "token savings" in text.lower()
        assert softened or sourced or no_claim, (
            f"{rel} must attribute a cost claim, cite it, or make no such claim"
        )
