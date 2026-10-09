"""The change gate judges the commits that would land, whoever made them, and fails closed."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from skilllayer import gate as g
from skilllayer.cli import main

_SRC = "def login():\n    return 'old'\n"
_TEST = "from src.auth import login\n\n\ndef test_login():\n    assert login() == 'old'\n"
_POLICY = "version: 1\nprotected_paths:\n  - migrations/\n"
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
# Built at runtime so this file never carries a key-shaped literal of its own.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
AI_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def _commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    for rel, content in files.items():
        path = repo / rel
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "dev")
    _commit(repo, "init", {
        "src/auth.py": _SRC,
        "tests/test_auth.py": _TEST,
        "pytest.ini": "[pytest]\npythonpath = .\n",
        ".gitignore": "__pycache__/\n",
        ".skilllayer-policy.yml": _POLICY,
        "migrations/001_init.sql": "create table users (id int);\n",
    })
    _git(repo, "switch", "-q", "-c", "feature")
    return repo


def _gate(repo: Path, **kwargs) -> dict:
    return g.run_gate(repo, base_ref="main", test_command=PYTEST, **kwargs)


def _check(receipt: dict, name: str) -> dict:
    return next(c for c in receipt["checks"] if c["name"] == name)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLLAYER_VERIFY_DIR", str(tmp_path / "vdata"))
    monkeypatch.delenv(g.RECEIPT_KEY_ENV, raising=False)
    monkeypatch.delenv(g.RECEIPT_KEY_ID_ENV, raising=False)
    monkeypatch.delenv(g.LANG_ENV, raising=False)


def test_a_clean_agent_change_is_verified_and_marked_ai_assisted(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, f"feat: harmless\n\n{AI_TRAILER}", {"src/auth.py": _SRC + "# harmless\n"})
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_VERIFIED
    assert receipt["blocked"] is False
    assert _check(receipt, "tests")["status"] == g.PASSED
    assert _check(receipt, "tests")["source"] == "observed"
    assert receipt["ai_assisted_commits"] == 1
    assert receipt["commits"][0]["ai_markers"] == ["Co-Authored-By: Claude"]
    assert receipt["commits"][0]["author"] == "dev"
    assert receipt["policy"]["read_at"] == _git(repo, "rev-parse", "main").strip()


def test_a_human_co_author_is_not_marked_ai_assisted(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "fix\n\nCo-authored-by: Ivan Petrov <ivan@example.com>", {"src/auth.py": _SRC + "# x\n"})
    assert _gate(repo)["ai_assisted_commits"] == 0


def test_failing_tests_block_whatever_the_commit_message_claims(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, f"fix: done, all tests pass\n\n{AI_TRAILER}", {"src/auth.py": "def login():\n    return 'new'\n"})
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_BLOCKED
    assert receipt["blocked"] is True
    tests = _check(receipt, "tests")
    assert tests["status"] == g.FAILED
    assert any("test_login" in name for name in tests["failed_tests"])


def test_a_change_cannot_unprotect_itself(tmp_path):
    """The policy comes from the base commit: dropping `migrations/` from it in the same
    change set protects nothing — and editing the policy is itself a protected change."""
    repo = _repo(tmp_path)
    _commit(repo, "migration", {
        "migrations/002_add_index.sql": "create index i on users(id);\n",
        ".skilllayer-policy.yml": "version: 1\nprotected_paths: []\n",
    })
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_BLOCKED
    hits = _check(receipt, "protected_paths")["hits"]
    assert "migrations/002_add_index.sql" in hits
    assert ".skilllayer-policy.yml" in hits


def test_caller_approval_accepts_a_protected_change_and_says_so(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "migration", {"migrations/002_add_index.sql": "create index i on users(id);\n"})
    receipt = _gate(repo, approve_protected=True)
    assert receipt["verdict"] == g.GATE_VERIFIED
    protected = _check(receipt, "protected_paths")
    assert protected["approved_by_caller"] is True
    assert protected["hits"] == ["migrations/002_add_index.sql"]


def test_an_unreadable_base_policy_leaves_protected_paths_unverified(tmp_path):
    """A broken policy at the base must not quietly shrink the protected set to the built-ins."""
    repo = tmp_path / "broken"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "dev")
    _commit(repo, "init", {
        "src/auth.py": _SRC, "tests/test_auth.py": _TEST, "pytest.ini": "[pytest]\npythonpath = .\n",
        ".gitignore": "__pycache__/\n", ".skilllayer-policy.yml": "version: 1\nprotected_paths: migrations/\n",
    })
    _git(repo, "switch", "-q", "-c", "feature")
    _commit(repo, "migration", {"migrations/002.sql": "select 1;\n"})
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_UNVERIFIED
    assert _check(receipt, "protected_paths")["reason"] == "policy_invalid_at_base"
    assert any(note.startswith("policy_ignored:") for note in receipt["limitations"])
    assert _gate(repo, approve_protected=True)["verdict"] == g.GATE_VERIFIED


def test_a_secret_added_then_deleted_still_blocks_and_is_never_echoed(tmp_path):
    repo = _repo(tmp_path)
    leaked = _commit(repo, "add config", {"src/config.py": f'AWS_KEY = "{FAKE_AWS_KEY}"\n'})
    _commit(repo, "oops, remove config", {"src/config.py": None})
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_BLOCKED
    findings = _check(receipt, "secrets")["findings"]
    assert [(f["file"], f["line"], f["pattern"], f["commit"]) for f in findings] == [
        ("src/config.py", 1, "aws_access_key", leaked[:12])
    ]
    assert FAKE_AWS_KEY not in json.dumps(receipt)
    assert FAKE_AWS_KEY not in g.render_gate_report(receipt)


def test_a_key_shaped_test_fixture_is_a_note_not_a_block(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "fixture", {"tests/test_fixture_keys.py": f'FAKE = "{FAKE_AWS_KEY}"\n'})
    receipt = _gate(repo)
    secrets = _check(receipt, "secrets")
    assert secrets["findings"] == []
    assert secrets["notes"][0]["likely_test_fixture"] is True
    assert receipt["verdict"] == g.GATE_VERIFIED
    assert any(n["kind"] == "test_files_touched" for n in receipt["notes"])


def test_an_added_line_that_looks_like_a_file_header_is_still_scanned(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "c-style", {"src/counter.c": f'++ "{FAKE_AWS_KEY}";\n'})
    findings = _check(_gate(repo), "secrets")["findings"]
    assert [f["file"] for f in findings] == ["src/counter.c"]


def test_a_failing_required_check_blocks_and_a_missing_tool_is_unverified(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    failing = _gate(repo, checks=[("lint", f'{sys.executable} -c "import sys; sys.exit(3)"')])
    assert failing["verdict"] == g.GATE_BLOCKED
    assert _check(failing, "check:lint")["exit_code"] == 3
    missing = _gate(repo, checks=[("sast", "definitely-not-an-installed-tool --scan")])
    assert missing["verdict"] == g.GATE_UNVERIFIED
    assert missing["blocked"] is True
    assert _check(missing, "check:sast")["reason"] == "command_not_found"


def test_an_uncommitted_checkout_is_unverified_and_its_tests_are_not_run(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    (repo / "src/auth.py").write_text("def login():\n    return 'local edit'\n")
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_UNVERIFIED
    tests = _check(receipt, "tests")
    assert tests["status"] == g.NOT_VERIFIED
    assert "uncommitted_changes" in tests["reason"]
    assert "outcome" not in tests


def test_untracked_tool_caches_do_not_count_as_a_dirty_checkout(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    (repo / "node_modules/pkg").mkdir(parents=True)
    (repo / "node_modules/pkg/index.js").write_text("module.exports = 1;\n")
    assert _gate(repo)["verdict"] == g.GATE_VERIFIED


def test_warn_mode_reports_without_blocking(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "break", {"src/auth.py": "def login():\n    return 'new'\n"})
    receipt = _gate(repo, mode="warn")
    assert receipt["verdict"] == g.GATE_BLOCKED
    assert receipt["blocked"] is False


def test_nothing_new_on_top_of_the_base_is_no_changes(tmp_path):
    repo = _repo(tmp_path)
    receipt = _gate(repo)
    assert receipt["verdict"] == g.GATE_NO_CHANGES
    assert receipt["blocked"] is False


def test_an_unresolvable_base_is_an_error_not_a_pass(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="does not resolve"):
        g.run_gate(repo, base_ref="origin/not-fetched")


def test_receipt_tampering_is_detected_and_only_the_key_holder_can_reseal(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    sealed = g.seal_receipt(_gate(repo), key=b"ci-key", key_id="ci")
    assert g.check_receipt(sealed, key=b"ci-key") == {"valid": True, "reason": "signature_ok", "signed": True, "key_id": "ci"}

    edited = {**sealed, "verdict": g.GATE_VERIFIED, "reasons": ["edited"]}
    assert g.check_receipt(edited, key=b"ci-key")["reason"] == "digest_mismatch"

    # Someone without the key can recompute the digest, but not the HMAC.
    forged = g.seal_receipt(edited)
    forged["integrity"]["hmac"] = sealed["integrity"]["hmac"]
    assert g.check_receipt(forged, key=b"ci-key")["reason"] == "signature_mismatch"
    assert g.check_receipt(forged)["reason"] == "digest_ok_signature_not_checked"
    assert g.check_receipt(g.seal_receipt(_gate(repo)), key=b"ci-key")["reason"] == "unsigned_receipt"


def test_cli_exit_codes_receipt_and_siem_event(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _commit(repo, f"feat\n\n{AI_TRAILER}", {"src/auth.py": _SRC + "# x\n"})
    receipts, events = tmp_path / "receipts", tmp_path / "events.jsonl"
    common = ["gate", "--repo", str(repo), "--base", "main", "--test-command", shlex.join(PYTEST),
              "--receipt-dir", str(receipts), "--event-log", str(events)]

    monkeypatch.setenv(g.RECEIPT_KEY_ENV, "ci-key")
    assert main(common) == 0
    assert g.RECEIPT_KEY_ENV not in os.environ  # taken out before any repository code ran
    (path,) = receipts.glob("*-VERIFIED.json")
    receipt = json.loads(path.read_text())
    event = json.loads(events.read_text().splitlines()[-1])
    assert event["digest"] == receipt["integrity"]["digest"]
    assert event["ai_assisted_commits"] == 1
    assert "✓ VERIFIED" in capsys.readouterr().out

    monkeypatch.setenv(g.RECEIPT_KEY_ENV, "ci-key")
    assert main(["gate", "--verify-receipt", str(path)]) == 0
    path.write_text(path.read_text().replace('"VERIFIED"', '"BLOCKED"', 1))
    monkeypatch.setenv(g.RECEIPT_KEY_ENV, "ci-key")
    assert main(["gate", "--verify-receipt", str(path)]) == 2

    assert main([*common, "--check", "sast=definitely-not-an-installed-tool"]) == 3
    _commit(repo, "break", {"src/auth.py": "def login():\n    return 'new'\n"})
    assert main(common) == 2
    assert main([*common, "--mode", "warn"]) == 0
    assert main(["gate", "--repo", str(repo), "--base", "origin/not-fetched"]) == 1
    assert main(["gate", "--repo", str(repo)]) == 1
    assert main([*common, "--check", "no equals sign"]) == 1


def test_the_signing_key_is_not_visible_to_the_code_under_judgement(tmp_path, monkeypatch):
    """Tests and checks run the change set's own code; if they could read the key, that code
    could sign a receipt for itself."""
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    probe = f"""{sys.executable} -c "import os, sys; sys.exit(7 if os.environ.get('{g.RECEIPT_KEY_ENV}') else 0)" """
    monkeypatch.setenv(g.RECEIPT_KEY_ENV, "ci-key")
    assert main(["gate", "--repo", str(repo), "--base", "main", "--test-command", shlex.join(PYTEST),
                 "--no-receipt", "--check", f"probe={probe.strip()}"]) == 0


def test_the_report_reads_in_russian(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, f"feat\n\n{AI_TRAILER}", {"src/auth.py": _SRC + "# x\n"})
    text = g.render_gate_report(_gate(repo), lang="ru")
    assert "skilllayer gate — ✓ ПРИНЯТО (VERIFIED)" in text
    assert "коммитов: 1, с пометкой ИИ: 1, файлов: 1" in text
    assert "✓ защищённые пути: не затронуты" in text
    assert "✓ тесты: " in text and "(код 0, " in text
    assert "Не принято" not in text


def test_the_russian_report_translates_reasons_and_still_hides_secrets(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "add config", {"src/config.py": f'AWS_KEY = "{FAKE_AWS_KEY}"\n'})
    receipt = _gate(repo, checks=[("sast", "definitely-not-an-installed-tool")])
    text = g.render_gate_report(receipt, lang="ru")
    assert "✗ ЗАБЛОКИРОВАНО (BLOCKED)" in text
    assert "? проверка sast: не выполнена (команда не найдена)" in text
    assert "✗ секреты: найдено 1 — src/config.py:1 (aws_access_key, коммит" in text
    assert FAKE_AWS_KEY not in text
    assert text.splitlines()[-1].startswith("Не принято: обязательная проверка не прошла")


def test_the_russian_report_says_why_tests_did_not_run(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    (repo / "src/auth.py").write_text("def login():\n    return 'local edit'\n")
    text = g.render_gate_report(_gate(repo), lang="ru")
    assert "? НЕ ПРОВЕРЕНО (UNVERIFIED)" in text
    assert "? тесты: не запускались (в рабочей копии есть незакоммиченные изменения)" in text


def test_receipt_checks_read_in_russian():
    assert g.render_receipt_check({"valid": True, "reason": "signature_ok"}, lang="ru") == (
        "skilllayer gate — квитанция действительна: подпись верна"
    )
    assert g.render_receipt_check({"valid": False, "reason": "digest_mismatch"}, lang="ru") == (
        "skilllayer gate — квитанция НЕДЕЙСТВИТЕЛЬНА: отпечаток не совпадает — квитанцию изменили после выдачи"
    )


def test_the_language_comes_from_the_flag_then_the_environment(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    _commit(repo, "change", {"src/auth.py": _SRC + "# x\n"})
    common = ["gate", "--repo", str(repo), "--base", "main", "--test-command", shlex.join(PYTEST), "--no-receipt"]
    assert main(common) == 0
    assert "✓ VERIFIED" in capsys.readouterr().out
    assert main([*common, "--lang", "ru"]) == 0
    assert "✓ ПРИНЯТО (VERIFIED)" in capsys.readouterr().out
    monkeypatch.setenv(g.LANG_ENV, "ru_RU.UTF-8")
    assert main(common) == 0
    assert "✓ ПРИНЯТО (VERIFIED)" in capsys.readouterr().out
    assert main([*common, "--lang", "en"]) == 0
    assert "✓ VERIFIED" in capsys.readouterr().out
    monkeypatch.setenv(g.LANG_ENV, "de")
    assert main(common) == 0
    assert "✓ VERIFIED" in capsys.readouterr().out
    # Machine-readable output does not change with the language.
    assert main([*common, "--lang", "ru", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "VERIFIED"


def test_russian_limitations_are_translated_and_unknown_codes_kept():
    assert g._limitation_ru("output_tail_lines_withheld:3") == "скрыто строк вывода (могли содержать секреты): 3"
    assert g._limitation_ru("policy_ignored:.skilllayer-policy.yml:SCHEMA") == "политика не прочитана: .skilllayer-policy.yml:SCHEMA"
    assert g._limitation_ru("something_new:1") == "something_new:1"


def test_a_change_that_deletes_the_failing_assertion_is_blocked(tmp_path):
    """Tests go green because the check was removed, not because the code was fixed."""
    repo = _repo(tmp_path)
    _commit(repo, f"fix: tests pass\n\n{AI_TRAILER}", {
        "src/auth.py": "def login():\n    return 'new'\n",
        "tests/test_auth.py": "from src.auth import login\n\n\ndef test_login():\n    login()\n",
    })
    receipt = _gate(repo)
    assert _check(receipt, "tests")["status"] == g.PASSED
    integrity = _check(receipt, "test_integrity")
    assert integrity["status"] == g.FAILED
    assert [f["kind"] for f in integrity["findings"]] == ["assertions_removed"]
    assert receipt["verdict"] == g.GATE_BLOCKED
    assert "fewer assertions" in g.render_gate_report(receipt)
    assert "ассертов стало меньше" in g.render_gate_report(receipt, lang="ru")


def test_a_skipped_test_is_blocked_unless_the_caller_approves(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "skip", {
        "src/auth.py": "def login():\n    return 'new'\n",
        "tests/test_auth.py": "import pytest\nfrom src.auth import login\n\n\n@pytest.mark.skip(reason='later')\ndef test_login():\n    assert login() == 'old'\n",
    })
    assert _gate(repo)["verdict"] == g.GATE_BLOCKED
    approved = _gate(repo, approve_test_changes=True)
    assert _check(approved, "test_integrity")["approved_by_caller"] is True
    assert approved["verdict"] == g.GATE_VERIFIED


def test_adding_a_test_keeps_the_suite_intact(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "more tests", {"tests/test_more.py": "from src.auth import login\n\n\ndef test_again():\n    assert login() == 'old'\n"})
    receipt = _gate(repo)
    integrity = _check(receipt, "test_integrity")
    assert integrity["status"] == g.PASSED and integrity["findings"] == []
    assert integrity["counts"]["test_functions"] == {"added": 1, "removed": 0}
    assert receipt["verdict"] == g.GATE_VERIFIED
