"""The case log counts what the gate stopped and how often a person agreed, without touching receipts."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from skilllayer import gate as g
from skilllayer import gate_cases as gc
from skilllayer.cli import main

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


def _receipt(head: str, verdict: str, checks: list[tuple[str, str]], *, mode: str = "warn",
             ai: int = 1, ms: float = 30000.0, created: str = "2026-11-30T10:00:00Z", key: bytes | None = None) -> dict:
    blocked = mode == "block" and verdict in gc.STOPPING
    return g.seal_receipt({
        "kind": "skilllayer.gate", "receipt_version": 1, "created_at": created,
        "head": {"sha": head * 40}, "verdict": verdict, "blocked": blocked, "mode": mode,
        "reasons": [f"{n}:{s}" for n, s in checks if s != g.PASSED],
        "checks": [{"name": n, "status": s} for n, s in checks],
        "ai_assisted_commits": ai, "duration_ms": ms,
    }, key=key)


def _write(directory: Path, receipt: dict, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(receipt), encoding="utf-8")
    return path


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    monkeypatch.delenv(g.RECEIPT_KEY_ENV, raising=False)
    d = tmp_path / "gate-receipts"
    _write(d, _receipt("a", g.GATE_BLOCKED, [("tests", g.PASSED), ("test_integrity", g.FAILED)]), "1.json")
    _write(d, _receipt("b", g.GATE_UNVERIFIED, [("tests", g.NOT_VERIFIED), ("secrets", g.PASSED)],
                       mode="block", created="2026-12-01T10:00:00Z"), "2.json")
    _write(d, _receipt("c", g.GATE_VERIFIED, [("tests", g.PASSED)], ai=0, created="2026-12-02T10:00:00Z"), "3.json")
    _write(d, _receipt("d", g.GATE_NO_CHANGES, [], ai=0, ms=0, created="2026-12-03T10:00:00Z"), "4.json")
    return d


def _cases(d: Path) -> list[dict]:
    receipts, _ = gc.load_receipts(d)
    return [c for r in receipts for c in gc.cases_of(r)]


def test_one_case_per_check_that_did_not_pass(pilot):
    cases = _cases(pilot)
    assert [(c["check"], c["status"], c["verdict"]) for c in cases] == [
        ("test_integrity", g.FAILED, g.GATE_BLOCKED), ("tests", g.NOT_VERIFIED, g.GATE_UNVERIFIED)]
    assert [c["id"] for c in cases] == [c["id"] for c in _cases(pilot)]  # stable across listings
    assert len({c["id"] for c in cases}) == 2


def test_two_failed_checks_in_one_run_are_two_cases(tmp_path):
    d = tmp_path / "r"
    _write(d, _receipt("a", g.GATE_BLOCKED, [("tests", g.FAILED), ("secrets", g.FAILED)]), "1.json")
    first, second = _cases(d)
    assert first["id"] != second["id"]
    gc.add_label(d, first, verdict="false_positive")
    assert second["id"] not in gc.read_labels(d)[0]


def test_invalid_and_duplicate_receipts(pilot):
    tampered = _receipt("e", g.GATE_BLOCKED, [("secrets", g.FAILED)])
    tampered["verdict"] = g.GATE_VERIFIED  # edited after sealing
    _write(pilot, tampered, "5.json")
    _write(pilot, {"kind": "something.else"}, "6.json")
    (pilot / "7.json").write_text("not json", encoding="utf-8")
    _write(pilot, json.loads((pilot / "1.json").read_text()), "1-copy.json")
    receipts, rejected = gc.load_receipts(pilot)
    assert sorted(rejected) == ["5.json", "6.json", "7.json"]
    assert len(receipts) == 4
    assert gc.summarize(receipts, {}, rejected=len(rejected))["rejected_receipts"] == 3


def test_signed_receipts_checked_against_the_key(tmp_path):
    d = tmp_path / "r"
    _write(d, _receipt("a", g.GATE_BLOCKED, [("tests", g.FAILED)], key=b"k1"), "1.json")
    assert len(gc.load_receipts(d, key=b"k1")[0]) == 1
    assert gc.load_receipts(d, key=b"k2") == ([], ["1.json"])


def test_labels_append_latest_wins_and_receipts_stay_untouched(pilot):
    before = {p.name: p.read_bytes() for p in pilot.glob("*.json")}
    case = _cases(pilot)[0]
    gc.add_label(pilot, case, verdict="false_positive")
    gc.add_label(pilot, case, verdict="correct", fixed=True, minutes=12, note="removed a real assertion")
    labels, bad = gc.read_labels(pilot)
    assert (labels[case["id"]]["verdict"], labels[case["id"]]["minutes"], bad) == ("correct", 12, 0)
    assert len((pilot / gc.LABELS_FILE).read_text().splitlines()) == 2
    assert {p.name: p.read_bytes() for p in pilot.glob("*.json")} == before


@pytest.mark.parametrize("kwargs", [
    {"verdict": "maybe"}, {"verdict": "correct", "minutes": -1}, {"verdict": "correct", "minutes": 100000},
    {"verdict": "correct", "minutes": 1.5}, {"verdict": "correct", "note": "two\nlines"},
    {"verdict": "correct", "note": "x" * (gc.MAX_NOTE_CHARS + 1)},
])
def test_bad_labels_are_refused_and_nothing_is_written(pilot, kwargs):
    with pytest.raises(ValueError):
        gc.add_label(pilot, _cases(pilot)[0], **kwargs)
    assert not (pilot / gc.LABELS_FILE).exists()


def test_unreadable_label_lines_are_counted_not_trusted(pilot):
    case = _cases(pilot)[0]
    (pilot / gc.LABELS_FILE).write_text(
        "garbage\n" + json.dumps({"case_id": case["id"], "verdict": "nonsense"}) + "\n"
        + json.dumps({"case_id": "c-../../etc", "verdict": "correct"}) + "\n", encoding="utf-8")
    labels, bad = gc.read_labels(pilot)
    assert (labels, bad) == ({}, 3)


def test_summary_gives_the_pilot_numbers(pilot):
    first, second = _cases(pilot)
    gc.add_label(pilot, first, verdict="correct", fixed=True, minutes=10)
    gc.add_label(pilot, second, verdict="false_positive", minutes=5)
    receipts, _ = gc.load_receipts(pilot)
    summary = gc.summarize(receipts, gc.read_labels(pilot)[0])
    assert summary == {
        "runs": 4, "runs_with_changes": 3, "ai_assisted_runs": 2,
        "would_stop": 2, "blocked": 1,
        "reasons": {"test_integrity:failed": 1, "tests:not_verified": 1},
        "cases": 2, "labels": {"correct": 1, "false_positive": 1, "unsure": 0, "unlabeled": 0},
        "false_positive_rate": 0.5, "fixed": 1, "review_minutes": 15, "gate_minutes": 1.5,
        "rejected_receipts": 0, "unreadable_label_lines": 0,
    }
    assert gc.summarize(receipts, {})["false_positive_rate"] is None
    assert "Доля ложных среди оценённых: 50%" in gc.render_summary(summary, lang="ru")


def test_labels_for_unsure_do_not_count_as_judged(pilot):
    first, second = _cases(pilot)
    gc.add_label(pilot, first, verdict="unsure")
    gc.add_label(pilot, second, verdict="false_positive")
    receipts, _ = gc.load_receipts(pilot)
    assert gc.summarize(receipts, gc.read_labels(pilot)[0])["false_positive_rate"] == 1.0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_cli_end_to_end_from_a_real_gate_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SKILLLAYER_VERIFY_DIR", str(tmp_path / "vdata"))
    monkeypatch.delenv(g.RECEIPT_KEY_ENV, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "dev")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "switch", "-q", "-c", "agent")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    _git(repo, "commit", "-qam", "agent: simplify add\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    receipts = repo / "gate-receipts"  # inside the checkout, as in the CI templates
    gate = ["gate", "--repo", str(repo), "--base", "main", "--mode", "warn",
            "--test-command", " ".join(PYTEST), "--receipt-dir", str(receipts)]
    assert main(gate) == 0
    capsys.readouterr()

    common = ["--receipt-dir", str(receipts), "--json"]
    assert main(["cases", "list", *common]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(c["check"], c["status"], c["label"]) for c in listed] == [("tests", g.FAILED, None)]
    case = listed[0]["id"]

    assert main(["cases", "label", case, "--verdict", "correct", "--fixed", "yes", "--minutes", "7", *common]) == 0
    assert json.loads(capsys.readouterr().out)["fixed"] is True
    assert main(["cases", "label", "c-0000000000", "--verdict", "correct", *common]) == 1
    capsys.readouterr()

    assert main(["cases", "summary", *common]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["would_stop"], summary["blocked"], summary["labels"]["correct"], summary["review_minutes"]) == (1, 0, 1, 7)

    # The labels file sits in the receipt directory inside the checkout; the next gate run must
    # not read it as an uncommitted change to the tree under judgement.
    assert main(gate) == 0
    newest = sorted(receipts.glob("*.json"))[-1]  # names start with a UTC timestamp
    tests = next(c for c in json.loads(newest.read_text())["checks"] if c["name"] == "tests")
    assert tests["status"] == g.FAILED
    capsys.readouterr()
    assert main(["cases", "summary", "--receipt-dir", str(receipts), "--lang", "ru"]) == 0
    assert "Остановил бы: 2" in capsys.readouterr().out


def test_cli_without_receipts_or_repository(tmp_path, capsys):
    assert main(["cases", "summary", "--receipt-dir", str(tmp_path / "missing")]) == 1
    assert main(["cases", "list", "--repo", str(tmp_path)]) == 1
    assert "not inside a git repository" in capsys.readouterr().err
