"""Internal Verified Task Execution foundations.

Foundation B adds factual repository baselines and scope comparison.  None of
these APIs are public workflows, routed, or exposed over MCP.
"""

from .baseline import capture_repository_baseline, fingerprint_relevant_files, read_repository_state
from .scope import validate_scope, validate_scope_contract

__all__ = [
    "capture_repository_baseline",
    "fingerprint_relevant_files",
    "read_repository_state",
    "validate_scope",
    "validate_scope_contract",
]
