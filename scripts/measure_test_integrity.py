"""How often would test_integrity flag ordinary, human-reviewed changes?

    .venv/bin/python scripts/measure_test_integrity.py /path/to/clone [...] [--last 300]

Walks the last N first-parent changes of each clone (one change per merged PR, for merge and
squash workflows alike), runs the detector on each against its first parent, and prints the
flag rate and the flagged subjects for review. Mature projects rarely weaken their own tests on
purpose, so the rate is an upper bound on false positives — read the flagged list to split it.
"""
from __future__ import annotations

import argparse
import collections
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from skilllayer.verify import is_test_path  # noqa: E402
from skilllayer.weakened_tests import analyze_diff  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True, text=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repos", nargs="+", type=Path)
    parser.add_argument("--last", type=int, default=300)
    args = parser.parse_args()
    total = touching = flagged = 0
    for repo in args.repos:
        shas = _git(repo, "rev-list", "--first-parent", f"--max-count={args.last}", "HEAD").split()
        repo_flagged, kinds = 0, collections.Counter()
        for sha in shas:
            parent = _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^1").strip()
            if not parent:
                continue
            status = _git(repo, "diff", "--name-status", "--no-renames", parent, sha).splitlines()
            names = [line.split("\t", 1)[-1] for line in status]
            touching += any(is_test_path(name) for name in names)
            deleted = [line.split("\t", 1)[1] for line in status if line.startswith("D\t")]
            diff = _git(repo, "diff", "-U0", "--no-color", "--no-renames", "--no-ext-diff", parent, sha)
            result = analyze_diff(diff, deleted_paths=deleted)
            if result["findings"]:
                repo_flagged += 1
                found = sorted({f["kind"] for f in result["findings"]})
                kinds.update(found)
                print(f"  {repo.name} {sha[:10]} {','.join(found)} | {_git(repo, 'log', '-1', '--format=%s', sha).strip()[:90]}")
        total += len(shas)
        flagged += repo_flagged
        print(f"{repo.name}: {repo_flagged}/{len(shas)} flagged {dict(kinds)}")
    print(f"TOTAL: {flagged}/{total} changes flagged ({flagged / max(total, 1):.1%}); "
          f"{flagged / max(touching, 1):.1%} of the {touching} that touch tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
