"""Internal Verified Task Execution foundations.

Foundation C adds internal append-only checkpoints and read-only resume
assessment. None of these APIs are public workflows, routed, or exposed over
MCP.
"""

from .baseline import capture_repository_baseline, fingerprint_relevant_files, read_repository_state
from .scope import validate_scope, validate_scope_contract
from .checkpoint import acquire_task_ownership, build_checkpoint, write_checkpoint_history
from .resume import assess_resume, load_checkpoint_chain

__all__ = [
    "capture_repository_baseline",
    "fingerprint_relevant_files",
    "read_repository_state",
    "validate_scope",
    "validate_scope_contract",
    "acquire_task_ownership",
    "assess_resume",
    "build_checkpoint",
    "load_checkpoint_chain",
    "write_checkpoint_history",
]
