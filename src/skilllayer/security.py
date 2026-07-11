"""Access-control boundary for workflow execution.

This module owns exactly one concern: *is a given workflow allowed to run from a
public entry point?* The decision is based purely on the workflow's declared
stability tier. It performs no routing or intent logic and does not know or care
how a workflow was selected — that is the router's responsibility.

Keeping this separate from routing means the security boundary can be reasoned
about and tested in isolation: given a workflow name and an override flag, it
returns allow/deny, full stop.
"""

from __future__ import annotations

import os

from .config.defaults import WORKFLOW_METADATA, workflow_stability

# Stability tiers that may not be executed through public entry points
# (CLI / MCP / direct Python API) without an explicit maintainer override.
BLOCKED_WORKFLOW_STABILITIES = {"internal"}

# Environment opt-in for maintainers running internal workflows deliberately.
ALLOW_INTERNAL_ENV_VAR = "SKILLLAYER_ALLOW_INTERNAL"


class InternalWorkflowBlockedError(RuntimeError):
    """Raised when the access policy denies execution of a workflow.

    Carries the offending ``workflow`` name and a human-readable ``reason`` so
    callers can surface a clear, explicit error.
    """

    def __init__(self, workflow: str | None, reason: str) -> None:
        self.workflow = workflow
        self.reason = reason
        super().__init__(reason)


class WorkflowAccessPolicy:
    """Decides whether a workflow may run from a public entry point.

    Single responsibility: access control by stability tier. No routing, no
    intent, no fallback handling.
    """

    def __init__(self, blocked_stabilities: set[str] | None = None) -> None:
        self.blocked_stabilities = set(
            BLOCKED_WORKFLOW_STABILITIES if blocked_stabilities is None else blocked_stabilities
        )

    def is_blocked(self, workflow: str | None, *, allow_internal: bool = False) -> bool:
        """True when ``workflow`` must not execute through a public entry point."""
        if allow_internal:
            return False
        return workflow_stability(workflow) in self.blocked_stabilities

    def denial_reason(self, workflow: str | None) -> str:
        """Human-readable explanation for refusing to run a blocked workflow."""
        stability = workflow_stability(workflow)
        summary = str(WORKFLOW_METADATA.get(workflow or "", {}).get("summary", "")).strip()
        base = (
            f"{workflow} is marked '{stability}' and is blocked from execution through "
            f"public entry points (MCP/CLI) until it is promoted to a supported tier."
        )
        if summary:
            base = f"{base} {summary}"
        return base

    def enforce(self, workflow: str | None, *, allow_internal: bool = False) -> None:
        """Raise :class:`InternalWorkflowBlockedError` if ``workflow`` is denied."""
        if self.is_blocked(workflow, allow_internal=allow_internal):
            raise InternalWorkflowBlockedError(workflow, self.denial_reason(workflow))


# Shared default policy plus thin module-level helpers, so entry points can use
# either the object (composition) or the functions (convenience) without
# duplicating the decision logic.
DEFAULT_ACCESS_POLICY = WorkflowAccessPolicy()


def workflow_execution_blocked(workflow: str | None, *, allow_internal: bool = False) -> bool:
    return DEFAULT_ACCESS_POLICY.is_blocked(workflow, allow_internal=allow_internal)


def blocked_workflow_reason(workflow: str | None) -> str:
    return DEFAULT_ACCESS_POLICY.denial_reason(workflow)


def internal_execution_allowed_via_env() -> bool:
    """Maintainer opt-in (env var) to run INTERNAL workflows deliberately."""
    return os.environ.get(ALLOW_INTERNAL_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}
