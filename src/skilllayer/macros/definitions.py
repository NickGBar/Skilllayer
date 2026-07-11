from __future__ import annotations

from dataclasses import dataclass

from ..config import MACROS


@dataclass(frozen=True)
class MacroDefinition:
    name: str
    tools: list[str]


def macro_definitions() -> dict[str, MacroDefinition]:
    return {name: MacroDefinition(name, list(tools)) for name, tools in MACROS.items()}
