"""Regression checks for the isolated Secure Context detector experiment."""
from __future__ import annotations

import importlib.util
import re
import sys
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments" / "secure_context_day1" / "scan_matrix.py"
SPEC = importlib.util.spec_from_file_location("secure_context_scan_matrix", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
scan_matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan_matrix)


def test_fixture_matrix_is_deterministic_and_has_both_controls() -> None:
    first = scan_matrix._fixtures()
    second = scan_matrix._fixtures()

    assert first == second
    positives, negatives = first
    assert len(positives) == 9
    assert len(negatives) == 2
    assert {"pii_only.py", "clean.py"} == set(negatives)


def test_core_scan_reports_matrix_counts_without_printing_fixture_values(
    monkeypatch, capsys
) -> None:
    # Keep this regression test independent of an optional Gitleaks install.
    monkeypatch.setenv("PATH", "")
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    assert scan_matrix.main() == 0
    output = capsys.readouterr().out
    positives, negatives = scan_matrix._fixtures()

    assert "positive_files_detected=9/9" in output
    assert "negative_files_flagged=0/2" in output
    assert "scan_complete=True" in output
    assert "gitleaks=unavailable" in output

    # Generated credential-like strings must not appear in the experiment's
    # console report. Check long token-shaped segments, not labels or paths.
    fixture_text = "\n".join([*positives.values(), *negatives.values()])
    token_segments = re.findall(r"[A-Za-z0-9_+/=-]{32,}", fixture_text)
    assert token_segments
    assert all(token not in output for token in token_segments)
