"""Routing component tests: SkillRouter in isolation.

These exercise *only* workflow selection and the no-match fallback. No access
policy is involved. This confirms the routing/intent concern is self-contained
and, in particular, that "no suitable workflow matched" is an explicit routing
outcome (matched=False) rather than an implicit choice that some other component
has to interpret.
"""

from __future__ import annotations

from skilllayer.router import SkillRouter
from skilllayer.router.cascade import FALLBACK_CONFIDENCE, FALLBACK_TASK_TYPE


def test_matched_task_sets_matched_true() -> None:
    decision = SkillRouter().route("Find function normalize_value")
    assert decision.matched is True
    assert decision.task_type == "find_function"
    assert decision.workflow == "FindFunctionWorkflow"


def test_unmatched_task_falls_back_explicitly() -> None:
    decision = SkillRouter().route("please reticulate the splines")
    # Routing owns the no-match outcome and marks it explicitly.
    assert decision.matched is False
    assert decision.task_type == FALLBACK_TASK_TYPE
    assert decision.confidence == FALLBACK_CONFIDENCE
    # The no-match fallback is a read-only clarification, never a write workflow.
    assert decision.workflow == "ClarifyIntentWorkflow"


def test_fallback_is_not_write_capable() -> None:
    # Regression guard: vague prose must never fall back to FixBugWorkflow (or any
    # code-editing workflow), which is how "make the CLI faster" once reached a
    # write-capable path stopped only by the internal-tier gate.
    decision = SkillRouter().route("make the CLI faster")
    assert decision.workflow != "FixBugWorkflow"
    assert decision.workflow == "ClarifyIntentWorkflow"
    assert decision.matched is False


def test_router_makes_no_access_decision() -> None:
    # The router selects a workflow regardless of any access/stability concern and
    # carries no access verdict — access control is somebody else's job.
    decision = SkillRouter().route("do something unrecognizable 12345")
    assert decision.workflow == "ClarifyIntentWorkflow"
    assert not hasattr(decision, "blocked")  # routing carries no access verdict


def test_internal_intent_match_still_matched() -> None:
    # A real rename request is a genuine match (matched=True), distinct from the
    # no-match fallback, even though both happen to select INTERNAL workflows.
    decision = SkillRouter().route("rename parse_money to parse_decimal_money")
    assert decision.matched is True
    assert decision.workflow == "RenameSymbolWorkflow"
