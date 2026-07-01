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
    assert decision.workflow == "FixBugWorkflow"


def test_router_makes_no_access_decision() -> None:
    # The router selects a workflow regardless of its stability/allowed status.
    # It returns FixBugWorkflow (an INTERNAL workflow) on fallback without raising
    # or filtering — access control is somebody else's job.
    decision = SkillRouter().route("do something unrecognizable 12345")
    assert decision.workflow == "FixBugWorkflow"
    assert not hasattr(decision, "blocked")  # routing carries no access verdict


def test_internal_intent_match_still_matched() -> None:
    # A real rename request is a genuine match (matched=True), distinct from the
    # no-match fallback, even though both happen to select INTERNAL workflows.
    decision = SkillRouter().route("rename parse_money to parse_decimal_money")
    assert decision.matched is True
    assert decision.workflow == "RenameSymbolWorkflow"
