"""Measure bounded synthetic request sizes without printing input content."""
import os
from time import perf_counter

from experiments.secure_context_hybrid_poc import analyze_with_gitleaks
from experiments.secure_context_policy_demo import Decision


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    unit = "def square(value):\n    return value * value\n\n"
    for size in (1_024, 16_384, 65_536, 262_144, 1_048_577):
        value = (unit * (size // len(unit) + 1))[:size]
        started = perf_counter()
        decision, _ = analyze_with_gitleaks(value, binary)
        expected = Decision.BLOCK if size > 1_048_576 else Decision.ALLOW
        print(f"input_bytes={len(value.encode('utf-8'))} decision={decision.value} "
              f"elapsed_ms={(perf_counter()-started)*1000:.1f}", flush=True)
        if decision != expected:
            raise SystemExit(f"Unexpected decision for size {size}")


if __name__ == "__main__":
    main()
