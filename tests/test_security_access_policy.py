"""Security component tests: WorkflowAccessPolicy in isolation.

These exercise *only* the access-control boundary. No router is involved — the
policy is handed workflow names directly. This confirms the security concern is
self-contained and does not depend on routing/intent logic.
"""

from __future__ import annotations

import pytest

from skilllayer.security import (
    InternalWorkflowBlockedError,
    WorkflowAccessPolicy,
    blocked_workflow_reason,
    workflow_execution_blocked,
)

INTERNAL_WORKFLOW = "AddHelperWorkflow"      # stability: internal
STABLE_WORKFLOW = "FindFunctionWorkflow"     # stability: stable
EXPERIMENTAL_WORKFLOW = "UnregisteredFutureWorkflow"  # not in metadata → defaults to experimental


def test_blocks_internal_workflow() -> None:
    policy = WorkflowAccessPolicy()
    assert policy.is_blocked(INTERNAL_WORKFLOW) is True


def test_allows_stable_and_experimental_workflows() -> None:
    policy = WorkflowAccessPolicy()
    assert policy.is_blocked(STABLE_WORKFLOW) is False
    assert policy.is_blocked(EXPERIMENTAL_WORKFLOW) is False


def test_allow_internal_override() -> None:
    policy = WorkflowAccessPolicy()
    assert policy.is_blocked(INTERNAL_WORKFLOW, allow_internal=True) is False


def test_enforce_raises_with_explicit_reason() -> None:
    policy = WorkflowAccessPolicy()
    with pytest.raises(InternalWorkflowBlockedError) as excinfo:
        policy.enforce(INTERNAL_WORKFLOW)
    error = excinfo.value
    assert error.workflow == INTERNAL_WORKFLOW
    assert INTERNAL_WORKFLOW in str(error)
    assert "internal" in str(error).lower()


def test_enforce_does_not_raise_for_allowed_workflow() -> None:
    # Should simply return None, no exception.
    assert WorkflowAccessPolicy().enforce(STABLE_WORKFLOW) is None


def test_policy_is_routing_agnostic() -> None:
    # The policy decides purely on the workflow name/tier. A made-up name that no
    # router would ever produce is treated by its (default) tier, proving the
    # policy has no dependency on routing.
    policy = WorkflowAccessPolicy()
    assert policy.is_blocked("CompletelyMadeUpWorkflow") is False  # unknown -> experimental default


def test_module_level_helpers_match_default_policy() -> None:
    assert workflow_execution_blocked(INTERNAL_WORKFLOW) is True
    assert workflow_execution_blocked(STABLE_WORKFLOW) is False
    assert INTERNAL_WORKFLOW in blocked_workflow_reason(INTERNAL_WORKFLOW)


def test_custom_blocked_stabilities() -> None:
    # Policy is configurable and self-contained: block experimental too.
    strict = WorkflowAccessPolicy(blocked_stabilities={"internal", "experimental"})
    assert strict.is_blocked(EXPERIMENTAL_WORKFLOW) is True
    assert strict.is_blocked(STABLE_WORKFLOW) is False
