"""Synthetic-only local composition of policy probe and Gitleaks."""
from __future__ import annotations

from experiments.secure_context_gitleaks import scan
from experiments.secure_context_local_poc import Decision, analyze


def analyze_with_gitleaks(text: str, binary: str, *, restricted: bool = False) -> tuple[Decision, str | None]:
    # Match the Core scanner's input limit before spawning any detector.
    try:
        if (not isinstance(text, str) or len(text) > 1_048_576 or "\x00" in text
                or len(text.encode("utf-8")) > 1_048_576):
            return Decision.BLOCK, None
    except UnicodeError:
        return Decision.BLOCK, None
    complete, found = scan(text, binary)
    if not complete or found:
        return Decision.BLOCK, None
    decision, output = analyze(text, restricted=restricted)
    if decision not in (Decision.ALLOW, Decision.TRANSFORM):
        return decision, None
    if output is None:
        return Decision.BLOCK, None
    if decision == Decision.ALLOW:
        return (decision, output) if output == text else (Decision.BLOCK, None)
    complete, found = scan(output, binary)
    if not complete or found:
        return Decision.BLOCK, None
    return decision, output


def analyze_guarded(text: str, binary: str, *, source_authorized: bool = False,
                    restricted: bool = False, restricted_terms: tuple[str, ...] = ()) -> tuple[Decision, str | None]:
    """Apply explicit synthetic policy before any possible allow decision."""
    if restricted:
        return Decision.LOCAL_ONLY, None
    if source_authorized is not True:
        return Decision.BLOCK, None
    if not isinstance(text, str):
        return Decision.BLOCK, None
    if not isinstance(restricted_terms, tuple) or any(not isinstance(term, str) or not term.strip() for term in restricted_terms):
        return Decision.BLOCK, None
    folded = text.casefold()
    if any(term.casefold() in folded for term in restricted_terms):
        return Decision.BLOCK, None
    return analyze_with_gitleaks(text, binary)
