"""Synthetic-only demonstration of policy decisions; no network or production integration."""
from enum import Enum


class Decision(str, Enum):
    ALLOW = "ALLOW"
    TRANSFORM = "TRANSFORM"
    LOCAL_ONLY = "LOCAL_ONLY"
    BLOCK = "BLOCK"


def decide(*, detector_complete: bool, secret: bool, pii: bool, restricted: bool, transformable: bool) -> Decision:
    if not detector_complete or secret:
        return Decision.BLOCK
    if restricted:
        return Decision.LOCAL_ONLY
    if pii:
        return Decision.TRANSFORM if transformable else Decision.BLOCK
    return Decision.ALLOW


if __name__ == "__main__":
    scenarios = {
        "clean": (True, False, False, False, False),
        "pii": (True, False, True, False, True),
        "secret": (True, True, False, False, False),
        "restricted": (True, False, False, True, False),
        "incomplete_scan": (False, False, False, False, False),
        "untransformable_pii": (True, False, True, False, False),
    }
    for name, (complete, secret, pii, restricted, transformable) in scenarios.items():
        result = decide(detector_complete=complete, secret=secret, pii=pii, restricted=restricted, transformable=transformable)
        print(f"{name}: {result.value}")
