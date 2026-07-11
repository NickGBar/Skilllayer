from __future__ import annotations

from dataclasses import dataclass

from ..config import WORKFLOWS


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    macro_sequence: list[str]


def workflow_definitions() -> dict[str, WorkflowDefinition]:
    return {name: WorkflowDefinition(name, list(macros)) for name, macros in WORKFLOWS.items()}
