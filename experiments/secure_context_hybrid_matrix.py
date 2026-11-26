"""Run the fixed synthetic matrix through the mandatory Gitleaks composition."""
import os
from functools import partial

from experiments.secure_context_hybrid_poc import analyze_with_gitleaks
from experiments.secure_context_matrix import main


if __name__ == "__main__":
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS")
    if not binary or not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    raise SystemExit(main(partial(analyze_with_gitleaks, binary=binary)))
