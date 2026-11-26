"""Synthetic-only local composition of policy probe and Gitleaks."""
from __future__ import annotations

from experiments.secure_context_gitleaks import scan
from experiments.secure_context_local_poc import Decision, analyze


def analyze_with_gitleaks(text: str, binary: str, *, restricted: bool = False) -> tuple[Decision, str | None]:
    complete, found = scan(text, binary)
    if not complete or found:
        return Decision.BLOCK, None
    decision, output = analyze(text, restricted=restricted)
    if decision not in (Decision.ALLOW, Decision.TRANSFORM):
        return decision, None
    if output is None:
        return Decision.BLOCK, None
    complete, found = scan(output, binary)
    if not complete or found:
        return Decision.BLOCK, None
    return decision, output


def analyze_guarded(text: str, binary: str, *, source_authorized: bool = False,
                    restricted: bool = False) -> tuple[Decision, str | None]:
    """Require explicit source authorization before any possible allow decision."""
    if restricted:
        return Decision.LOCAL_ONLY, None
    if source_authorized is not True:
        return Decision.BLOCK, None
    return analyze_with_gitleaks(text, binary)
