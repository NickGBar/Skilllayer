"""Small paired timing probe for synthetic inputs; not a production benchmark."""
import os
from statistics import median
from time import perf_counter

from experiments.secure_context_hybrid_poc import analyze_with_gitleaks
from experiments.secure_context_local_poc import analyze


def main():
    binary = os.environ.get("SECURE_CONTEXT_GITLEAKS", "")
    if not os.path.isabs(binary):
        raise SystemExit("SECURE_CONTEXT_GITLEAKS must name an absolute binary path")
    cases = {"clean": "Explain a Python list", "email": "Email a.person@example.com",
             "secret": 'api_key = "' + "abcXYZ0123456789" * 3 + '"'}
    # Warm both paths once; alternate ordering to avoid systematically assigning
    # cache and load effects to the same path.
    analyze(cases["clean"])
    analyze_with_gitleaks(cases["clean"], binary)
    for label, value in cases.items():
        samples = {"local": [], "hybrid": []}
        for i in range(6):
            order = ("local", "hybrid") if i % 2 == 0 else ("hybrid", "local")
            decisions = {}
            for kind in order:
                started = perf_counter()
                result = analyze(value) if kind == "local" else analyze_with_gitleaks(value, binary)
                samples[kind].append((perf_counter() - started) * 1000)
                decisions[kind] = result[0]
            if decisions["local"] != decisions["hybrid"]:
                raise RuntimeError(f"synthetic decisions differ for {label}")
        print(f"{label}: local_median_ms={median(samples['local']):.1f} "
              f"hybrid_median_ms={median(samples['hybrid']):.1f} pairs=6")


if __name__ == "__main__":
    main()
