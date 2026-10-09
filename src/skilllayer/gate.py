"""Agent-agnostic change gate: a change set lands only when its required checks were observed passing.

``skilllayer gate`` judges what will actually land — the commits between a base ref and the
checked-out HEAD — whichever agent or person made them. It runs in CI or in a git pre-push
hook, so it needs neither the agent's cooperation nor agent-specific hooks: a commit hidden
behind ``npm run release`` or made by an agent without hook support still passes through it.

The rules of ``skilllayer verify`` apply, plus what a gate needs:

* Fail closed. A check that could not run, a working tree that is not the commit under
  judgement, a diff too large to scan — all are UNVERIFIED, and UNVERIFIED blocks unless the
  caller chose warn mode. An internal error is an error exit, never a pass.
* The policy is read from the base commit: a change set cannot loosen its own guardrails.
* Commands come from the caller (CI configuration or the local hook file), never from files
  inside the repository being judged.
* Every run leaves a receipt with a digest, and an HMAC when a key is configured, so the
  evidence that a change passed its checks can be re-checked later and cannot be edited
  unnoticed by anyone without the key.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner.core import _SECRET_PATTERNS, _is_test_fixture_path
from .verify import (
    _INTEGRITY_NOTE_PATHS,
    _OUTCOME_TO_VERDICT,
    TESTS_FAILING,
    VERIFIED,
    _committed_changes,
    _git,
    _is_internal,
    _matches_rule,
    _now,
    _safe_line,
    _sanitized_tail,
    _tests_line,
    current_head,
    data_dir,
    is_test_path,
    observe_tests,
)
from .verify_hook import load_verify_config
from .weakened_tests import check_test_integrity

RECEIPT_VERSION = 1

# Gate verdicts. Only VERIFIED accepts a change set; NO_CHANGES has nothing to accept.
GATE_VERIFIED = "VERIFIED"
GATE_BLOCKED = "BLOCKED"
GATE_UNVERIFIED = "UNVERIFIED"
GATE_NO_CHANGES = "NO_CHANGES"

# Per-check status.
PASSED = "passed"
FAILED = "failed"
NOT_VERIFIED = "not_verified"

RECEIPT_KEY_ENV = "SKILLLAYER_RECEIPT_KEY"
RECEIPT_KEY_ID_ENV = "SKILLLAYER_RECEIPT_KEY_ID"

# Console language. Receipts, JSON output and the event log keep English keys and codes,
# so a SIEM rule or a script reads them the same whatever language a person reads.
LANG_ENV = "SKILLLAYER_LANG"
LANGUAGES = ("en", "ru")

_MAX_DIFF_CHARS = 5_000_000
_MAX_COMMITS = 500
_BLOCKING_SECRET_SEVERITIES = frozenset({"critical", "high"})

# Untracked build and tool caches cannot change the committed code under judgement, and a
# repository that does not ignore them must not fail every local push because of them.
_CACHE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".venv", "venv", "node_modules",
})

# Commit trailers that say an AI agent wrote or co-wrote the commit. Agents are not obliged
# to add them, so this marks AI-assisted commits; it never proves a commit is human-only.
_AI_TRAILER_RE = re.compile(r"^(co-authored-by|generated-by|assisted-by|ai-assisted|ai-agent)\s*:\s*(.+)$", re.I)
_AI_NAME_RE = re.compile(
    r"claude|anthropic|copilot|cursor|codex|openai|chatgpt|gpt-|gemini|gigacode|gigachat|yandexgpt"
    r"|sourcecraft|devin|aider|cline|kilo|opencode|qwen|deepseek|\[bot\]",
    re.I,
)
_EMAIL_SUFFIX_RE = re.compile(r"\s*<[^<>]*>\s*$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_URL_USERINFO_RE = re.compile(r"(://)[^/@\s]+@")


# --------------------------------------------------------------------------- git facts


def resolve_commit(root: Path, ref: str) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def merge_base(root: Path, base: str, head: str) -> str | None:
    proc = _git(root, "merge-base", base, head)
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _remote_url(root: Path) -> str | None:
    proc = _git(root, "remote", "get-url", "origin")
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    # A remote URL can carry a token ("https://user:token@host/..."); it never reaches a receipt.
    return _safe_line(_URL_USERINFO_RE.sub(r"\1", proc.stdout.strip()), 300)


def _ai_markers(trailers: str) -> list[str]:
    markers: list[str] = []
    for line in trailers.splitlines():
        match = _AI_TRAILER_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "co-authored-by" and not _AI_NAME_RE.search(value):
            continue  # a human co-author
        # The trailer's e-mail address is personal data and adds nothing the commit sha does not.
        safe = _safe_line(_EMAIL_SUFFIX_RE.sub("", line.strip()), 160)
        if safe:
            markers.append(safe)
    return markers


def list_commits(root: Path, base: str, head: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Commits in ``base..head``, newest first, each marked when a trailer says an AI wrote it."""
    fmt = "%H%x1f%an%x1f%s%x1f%(trailers:only,unfold)%x1e"  # author name only: the sha identifies the rest
    proc = _git(root, "log", f"--max-count={_MAX_COMMITS + 1}", f"--format={fmt}", f"{base}..{head}", timeout=60)
    if proc is None or proc.returncode != 0:
        return [], ["commit_list_unavailable"]
    commits: list[dict[str, Any]] = []
    for record in proc.stdout.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) < 4 or not fields[0].strip():
            continue
        sha, author, subject, trailers = fields[0].strip(), fields[1], fields[2], fields[3]
        markers = _ai_markers(trailers)
        commits.append({
            "sha": sha,
            "author": _safe_line(author, 200) or "(withheld)",
            "subject": _safe_line(subject, 200) or "(withheld)",
            "ai_assisted": bool(markers),
            "ai_markers": markers,
        })
    limitations = [f"commit_list_truncated:{_MAX_COMMITS}"] if len(commits) > _MAX_COMMITS else []
    return commits[:_MAX_COMMITS], limitations


def _untracked_is_cache(path: str, receipt_dir: Path | None, root: Path) -> bool:
    parts = path.rstrip("/").split("/")
    if any(part in _CACHE_DIRS for part in parts):
        return True
    if receipt_dir is not None:
        try:
            rel = receipt_dir.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return False
        return path.rstrip("/") == rel or path.startswith(rel + "/")
    return False


def tree_problems(root: Path, head_sha: str, *, receipt_dir: Path | None = None) -> list[str]:
    """Why the checkout is not exactly the commit under judgement ([] when it is).

    Tests run against the checkout; if it holds anything the commit does not, their result
    would be about something else, so the gate refuses to call it a verification."""
    problems: list[str] = []
    if current_head(root) != head_sha:
        problems.append("head_not_checked_out")
    status = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=normal")
    if status is None or status.returncode != 0:
        return [*problems, "git_status_unavailable"]
    for entry in (e for e in status.stdout.split("\0") if e):
        code, path = entry[:2], entry[3:]
        if _is_internal(path):
            continue
        if code == "??" and _untracked_is_cache(path, receipt_dir, root):
            continue
        problems.append("uncommitted_changes")
        break
    return problems


# --------------------------------------------------------------------------- checks


def check_protected_paths(paths: list[str], rules: tuple[str, ...], *, approved: bool, policy_invalid: bool = False) -> dict[str, Any]:
    hits = sorted({p for p in paths if any(_matches_rule(p, r) for r in rules)})
    if policy_invalid and not approved:
        # The base policy could not be read, so which paths it protects is unknown: judging
        # against the built-in list alone would quietly pass what the policy meant to stop.
        return {"name": "protected_paths", "status": NOT_VERIFIED, "reason": "policy_invalid_at_base", "hits": hits, "rules": list(rules)}
    if not hits:
        return {"name": "protected_paths", "status": PASSED, "hits": [], "rules": list(rules)}
    if approved:
        # The approval comes from the caller's trusted configuration (for example a CI job
        # that runs only after a person approved the merge request), never from the change.
        return {"name": "protected_paths", "status": PASSED, "hits": hits, "rules": list(rules), "approved_by_caller": True}
    return {"name": "protected_paths", "status": FAILED, "hits": hits, "rules": list(rules)}


def check_added_secrets(root: Path, base: str, head: str) -> dict[str, Any]:
    """Scan every line each commit in ``base..head`` adds — commit by commit, not the net diff:
    a key added in one commit and deleted in the next still reaches the remote in history.
    Findings name the commit, file, line and pattern only — the matched text never reaches
    a receipt, a log or the console."""
    proc = _git(
        root, "-c", "core.quotepath=false", "log", "-p", "-U0", "--no-color", "--no-renames", "--no-ext-diff",
        "--format=%x1fcommit %H", f"{base}..{head}", timeout=120,
    )
    if proc is None or proc.returncode != 0:
        return {"name": "secrets", "status": NOT_VERIFIED, "reason": "history_unavailable", "findings": [], "notes": []}
    if len(proc.stdout) > _MAX_DIFF_CHARS:
        return {"name": "secrets", "status": NOT_VERIFIED, "reason": "history_too_large", "findings": [], "notes": []}
    findings: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    commit = ""
    current: str | None = None
    in_header = False
    line_no = 0
    added = 0
    binary = 0
    for line in proc.stdout.splitlines():
        if line.startswith("\x1fcommit "):
            commit, current, in_header = line.split(" ", 1)[1].strip()[:12], None, False
            continue
        if line.startswith("diff --git "):
            current, in_header = None, True
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            line_no, in_header = int(hunk.group(1)), False
            continue
        if in_header:
            # "+++ " is a file header only here: an added line "++ x" also reads "+++ x".
            if line.startswith("+++ "):
                target = line[4:].strip().strip('"')
                current = target[2:] if target.startswith("b/") else None  # /dev/null: a deletion
            elif line.startswith("Binary files "):
                binary += 1
            continue
        if current is None or not line.startswith("+") or _is_internal(current):
            continue
        added += 1
        content = line[1:]
        for pattern in _SECRET_PATTERNS:
            if not pattern["pattern"].search(content):
                continue
            fixture = _is_test_fixture_path(current)
            item = {
                "commit": commit,
                "file": _safe_line(current, 200) or "(path withheld)",
                "line": line_no,
                "pattern": pattern["name"],
                "severity": pattern["severity"],
                "likely_test_fixture": fixture,
            }
            blocking = pattern["severity"] in _BLOCKING_SECRET_SEVERITIES and not fixture
            (findings if blocking else notes).append(item)
        line_no += 1
    result: dict[str, Any] = {
        "name": "secrets",
        "status": FAILED if findings else PASSED,
        "added_lines_scanned": added,
        "findings": findings[:50],
        "notes": notes[:50],
        "patterns": [p["name"] for p in _SECRET_PATTERNS],
    }
    if binary:
        result["limitations"] = [f"binary_files_not_scanned:{binary}"]
    return result


_NODE_ID_RE = re.compile(r"^[\w./-]+\.py::[\w\[\]./:-]+$")


def _failed_node_ids(tests: dict[str, Any]) -> list[str] | None:
    """pytest node ids of the failing tests, or None when any label is not one (then the
    whole command is re-run instead of a guess at which tests to target)."""
    ids: list[str] = []
    for label in tests.get("failed_tests") or []:
        node = str(label).split(" — ", 1)[0].strip()
        if not _NODE_ID_RE.match(node):
            return None
        ids.append(node)
    return ids or None


def check_tests(
    root: Path, *, test_command: list[str] | None, timeout_s: int, flaky_reruns: int = 2, accept_flaky: bool = False,
) -> dict[str, Any]:
    """Run the tests; when they fail, re-run the failures up to ``flaky_reruns`` times.

    A failure that passes on a re-run is not "tests failing" — but it is not a pass either: the
    test is flaky, or it depends on what ran before it. Either way the change set was not shown
    to work, so it stays NOT_VERIFIED unless the caller accepts flaky tests, which is recorded."""
    tests = observe_tests(root, test_command=test_command, timeout_s=timeout_s)
    verdict = _OUTCOME_TO_VERDICT.get(str(tests.get("outcome")), "UNVERIFIED_UNKNOWN")
    status = PASSED if verdict == VERIFIED else FAILED if verdict == TESTS_FAILING else NOT_VERIFIED
    result = {"name": "tests", "status": status, "verdict": verdict, **{k: v for k, v in tests.items() if k != "source"}, "source": "observed"}
    command = shlex.split(str(tests.get("command") or ""))
    if status != FAILED or flaky_reruns <= 0 or not command:
        return result
    nodes = _failed_node_ids(tests) if any("pytest" in part for part in command) else None
    rerun_command = [*command, *nodes] if nodes else command
    reruns: list[dict[str, Any]] = []
    for attempt in range(1, flaky_reruns + 1):
        again = observe_tests(root, test_command=rerun_command, timeout_s=timeout_s)
        again_verdict = _OUTCOME_TO_VERDICT.get(str(again.get("outcome")), "UNVERIFIED_UNKNOWN")
        reruns.append({"attempt": attempt, "outcome": again.get("outcome"), "failed_tests": (again.get("failed_tests") or [])[:10]})
        if again_verdict == VERIFIED:
            result["flaky_tests"] = nodes or ["(whole test command)"]
            if accept_flaky:
                result.update(status=PASSED, verdict="FLAKY", flaky_accepted_by_caller=True)
            else:
                result.update(status=NOT_VERIFIED, verdict="FLAKY", reason="flaky_tests")
            break
    result["reruns"] = reruns
    return result


def run_required_check(root: Path, name: str, command: str, *, timeout_s: int) -> dict[str, Any]:
    """Run one caller-supplied check (no shell) and record what actually happened."""
    record: dict[str, Any] = {"name": f"check:{name}", "command": _safe_line(command, 300) or "(withheld)"}
    try:
        argv = shlex.split(command)
    except ValueError:
        return {**record, "status": NOT_VERIFIED, "reason": "unparseable_command"}
    if not argv:
        return {**record, "status": NOT_VERIFIED, "reason": "empty_command"}
    started = time.perf_counter()
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, timeout=timeout_s, check=False)
    except FileNotFoundError:
        return {**record, "status": NOT_VERIFIED, "reason": "command_not_found"}
    except PermissionError:
        return {**record, "status": NOT_VERIFIED, "reason": "permission_denied"}
    except subprocess.TimeoutExpired:
        return {**record, "status": NOT_VERIFIED, "reason": f"timeout_after_{timeout_s}s"}
    except OSError as exc:
        return {**record, "status": NOT_VERIFIED, "reason": f"os_error:{exc.errno}"}
    output = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    tail, limitations = _sanitized_tail(output.decode("utf-8", errors="replace"), max_lines=15)
    return {
        **record,
        "status": PASSED if proc.returncode == 0 else FAILED,
        "exit_code": proc.returncode,
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_tail": tail,
        **({"limitations": limitations} if limitations else {}),
    }


def parse_check_spec(spec: str) -> tuple[str, str]:
    """``NAME=COMMAND`` → (name, command). Raises ValueError with a readable message."""
    name, sep, command = spec.partition("=")
    name = name.strip()
    if not sep or not name or not command.strip():
        raise ValueError(f"expected NAME=COMMAND, got {spec!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        raise ValueError(f"check name must be 1-40 letters, digits, '_', '.' or '-': {name!r}")
    return name, command.strip()


# --------------------------------------------------------------------------- the gate


def run_gate(
    root: Path,
    *,
    base_ref: str,
    head_ref: str = "HEAD",
    test_command: list[str] | None = None,
    checks: list[tuple[str, str]] | None = None,
    mode: str = "block",
    max_seconds: int | None = None,
    approve_protected: bool = False,
    approve_test_changes: bool = False,
    flaky_reruns: int = 2,
    accept_flaky: bool = False,
    receipt_dir: Path | None = None,
) -> dict[str, Any]:
    """Judge the change set ``merge-base(base, head)..head``. Writes nothing; raises ValueError
    for a ref that does not resolve (the caller must treat that as a failure, not a pass)."""
    started = time.perf_counter()
    base_sha = resolve_commit(root, base_ref)
    if base_sha is None:
        raise ValueError(f"base ref {base_ref!r} does not resolve to a commit (in CI, fetch the full history)")
    head_sha = resolve_commit(root, head_ref)
    if head_sha is None:
        raise ValueError(f"head ref {head_ref!r} does not resolve to a commit")
    fork_point = merge_base(root, base_sha, head_sha)
    if fork_point is None:
        raise ValueError(f"{base_ref!r} and {head_ref!r} share no history")

    config, policy_notes = load_verify_config(root, base_sha)
    timeout_s = max_seconds or config.test_timeout_seconds
    limitations: list[str] = list(policy_notes)
    commits, commit_limits = list_commits(root, fork_point, head_sha)
    limitations += commit_limits
    paths, deleted, change_limits = _committed_changes(root, fork_point, head_sha)
    limitations += change_limits
    existing = [p for p in paths if p not in set(deleted)]
    changes = {
        "paths": paths[:200],
        "paths_total": len(paths),
        "deleted": deleted[:200],
        "tests_modified": [p for p in existing if is_test_path(p)][:50],
        "tests_deleted": [p for p in deleted if is_test_path(p)][:50],
    }
    notes: list[dict[str, Any]] = []
    if changes["tests_modified"] or changes["tests_deleted"]:
        notes.append({"kind": "test_files_touched", "modified": changes["tests_modified"], "deleted": changes["tests_deleted"]})
    for path in (p for p in paths if p in _INTEGRITY_NOTE_PATHS):
        notes.append({"kind": "agent_configuration_modified", "path": path})

    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "kind": "skilllayer.gate",
        "created_at": _now(),
        "repo": {"name": root.name, "remote": _remote_url(root)},
        "base": {"ref": _safe_line(base_ref, 200), "sha": base_sha},
        "head": {"ref": _safe_line(head_ref, 200), "sha": head_sha},
        "merge_base": fork_point,
        "policy": {"read_at": base_sha, "protected_rules": list(config.effective_protected())},
        "commits": commits,
        "ai_assisted_commits": sum(1 for c in commits if c["ai_assisted"]),
        "changes": changes,
        "mode": mode,
    }

    if not paths and fork_point == head_sha:
        receipt.update({
            "verdict": GATE_NO_CHANGES, "blocked": False, "reasons": [], "checks": [], "notes": notes,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "limitations": sorted(set(limitations)),
        })
        return receipt

    results: list[dict[str, Any]] = [
        check_protected_paths(
            paths, config.effective_protected(), approved=approve_protected,
            policy_invalid=any(note.startswith("policy_ignored:") for note in policy_notes),
        ),
        check_added_secrets(root, fork_point, head_sha),
        check_test_integrity(root, fork_point, head_sha, deleted_paths=deleted, approved=approve_test_changes),
    ]
    problems = tree_problems(root, head_sha, receipt_dir=receipt_dir)
    if problems:
        # Running tests here would test a different tree than the one that lands.
        reason = ",".join(problems)
        results.append({"name": "tests", "status": NOT_VERIFIED, "reason": reason})
        results += [{"name": f"check:{name}", "command": _safe_line(cmd, 300), "status": NOT_VERIFIED, "reason": reason} for name, cmd in (checks or [])]
    else:
        results.append(check_tests(root, test_command=test_command, timeout_s=timeout_s, flaky_reruns=flaky_reruns, accept_flaky=accept_flaky))
        results += [run_required_check(root, name, cmd, timeout_s=timeout_s) for name, cmd in (checks or [])]
    for result in results:
        limitations += result.pop("limitations", []) or []

    reasons = [f"{r['name']}:{r['status']}" for r in results if r["status"] != PASSED]
    if any(r["status"] == FAILED for r in results):
        verdict = GATE_BLOCKED
    elif reasons:
        verdict = GATE_UNVERIFIED
    else:
        verdict = GATE_VERIFIED
    receipt.update({
        "verdict": verdict,
        "blocked": mode == "block" and verdict in {GATE_BLOCKED, GATE_UNVERIFIED},
        "reasons": reasons,
        "checks": results,
        "notes": notes,
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "limitations": sorted(set(limitations)),
    })
    return receipt


# --------------------------------------------------------------------------- receipts


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal_receipt(receipt: dict[str, Any], *, key: bytes | None = None, key_id: str | None = None) -> dict[str, Any]:
    """Add ``integrity``: a SHA-256 digest of the canonical receipt, plus an HMAC when a key
    is given. The digest alone fingerprints the receipt (store it elsewhere, e.g. in the SIEM
    event, to detect later edits); the HMAC makes it tamper-evident for anyone without the key."""
    body = {k: v for k, v in receipt.items() if k != "integrity"}
    canonical = _canonical(body)
    integrity: dict[str, Any] = {"digest": "sha256:" + hashlib.sha256(canonical).hexdigest()}
    if key:
        integrity["hmac"] = {
            "alg": "HMAC-SHA256",
            "key_id": key_id or "default",
            "value": hmac.new(key, canonical, hashlib.sha256).hexdigest(),
        }
    return {**body, "integrity": integrity}


def check_receipt(receipt: dict[str, Any], *, key: bytes | None = None) -> dict[str, Any]:
    """Re-derive a receipt's digest (and HMAC when a key is given) and compare."""
    integrity = receipt.get("integrity")
    if not isinstance(integrity, dict) or not isinstance(integrity.get("digest"), str):
        return {"valid": False, "reason": "no_integrity_block"}
    body = {k: v for k, v in receipt.items() if k != "integrity"}
    canonical = _canonical(body)
    digest_ok = hmac.compare_digest(integrity["digest"], "sha256:" + hashlib.sha256(canonical).hexdigest())
    if not digest_ok:
        return {"valid": False, "reason": "digest_mismatch"}
    signature = integrity.get("hmac")
    if key is None:
        return {"valid": True, "reason": "digest_ok_signature_not_checked" if signature else "digest_ok_unsigned", "signed": bool(signature)}
    if not isinstance(signature, dict) or not isinstance(signature.get("value"), str):
        return {"valid": False, "reason": "unsigned_receipt"}
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature["value"], expected):
        return {"valid": False, "reason": "signature_mismatch"}
    return {"valid": True, "reason": "signature_ok", "signed": True, "key_id": signature.get("key_id")}


def take_receipt_key_from_env() -> tuple[bytes | None, str | None]:
    """Read the signing key and remove it from the environment.

    The gate runs the change set's own code (its tests, its build scripts). Left in the
    environment, the key would be readable by exactly the code it is meant to vouch for,
    which could then sign a receipt for itself. Call this before ``run_gate``."""
    raw = os.environ.pop(RECEIPT_KEY_ENV, None)
    key_id = os.environ.pop(RECEIPT_KEY_ID_ENV, None)
    return (raw.encode("utf-8"), key_id or "default") if raw else (None, None)


def default_receipt_dir(root: Path) -> Path:
    return data_dir(root) / "gate-receipts"


def write_receipt(receipt: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{stamp}-{receipt['head']['sha'][:12]}-{receipt['verdict']}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return path


def append_event(receipt: dict[str, Any], log_path: Path) -> None:
    """One compact JSON line per run, for a SIEM forwarder: what was judged and how, plus the
    receipt digest so the full receipt can be matched against it later."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": receipt["created_at"], "kind": receipt["kind"], "repo": receipt["repo"]["name"],
        "base": receipt["base"]["sha"], "head": receipt["head"]["sha"], "verdict": receipt["verdict"],
        "blocked": receipt["blocked"], "mode": receipt["mode"], "reasons": receipt["reasons"],
        "commits": len(receipt["commits"]), "ai_assisted_commits": receipt["ai_assisted_commits"],
        "digest": (receipt.get("integrity") or {}).get("digest"),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- rendering

_MARKS = {PASSED: "✓", FAILED: "✗", NOT_VERIFIED: "?"}


def _check_line(result: dict[str, Any]) -> str:
    name, status = result["name"], result["status"]
    mark = _MARKS.get(status, "?")
    if name == "tests":
        detail = _tests_line(result) if result.get("outcome") else f"not run ({result.get('reason')})"
        lines = [f"  {mark} tests: {detail}"]
        if result.get("flaky_tests"):
            how = "accepted by the caller" if result.get("flaky_accepted_by_caller") else "flaky or order-dependent, not a pass"
            lines.append(f"      failed, then passed on re-run — {how}: {', '.join(result['flaky_tests'][:5])}")
            return "\n".join(lines)
        lines += [f"      {failed}" for failed in (result.get("failed_tests") or [])[:10]]
        return "\n".join(lines)
    if name == "protected_paths":
        if status == NOT_VERIFIED:
            return f"  ? protected paths: not checked ({result.get('reason')})"
        if result.get("approved_by_caller"):
            return f"  {mark} protected paths: changed with caller approval — {', '.join(result['hits'][:5])}"
        return f"  {mark} protected paths: " + (", ".join(result["hits"][:5]) + " (policy read from the base commit)" if result["hits"] else "none changed")
    if name == "secrets":
        if status == NOT_VERIFIED:
            return f"  ? secrets: not scanned ({result.get('reason')})"
        found = result.get("findings") or []
        if not found:
            return f"  ✓ secrets: none in {result.get('added_lines_scanned', 0)} added lines"
        shown = ", ".join(f"{f['file']}:{f['line']} ({f['pattern']}, commit {f['commit']})" for f in found[:5])
        return f"  ✗ secrets: {len(found)} added — {shown}"
    if name == "test_integrity":
        return _integrity_lines(result, mark, _INTEGRITY_EN, "test integrity")
    if status == NOT_VERIFIED:
        return f"  ? {name}: not verified ({result.get('reason')})"
    return f"  {mark} {name}: exit {result.get('exit_code')} ({(result.get('duration_ms') or 0) / 1000:.1f}s) — {result.get('command')}"


_INTEGRITY_EN = {
    "skip_added": "skip/xfail added",
    "tautological_assertion": "assertion that cannot fail",
    "tests_deselected": "tests deselected in configuration",
    "threshold_lowered": "threshold lowered",
    "tests_removed": "fewer tests",
    "assertions_removed": "fewer assertions",
    "test_file_deleted": "test file deleted",
    "_ok": "suite not weakened",
    "_approved": "weakened with caller approval",
    "_failed": "signs the suite was weakened: {n}",
    "_not_checked": "not checked",
    "_counts": "test functions +{fa}/−{fr}, assertions +{aa}/−{ar}",
    "_by": " by {n}",
}


def _integrity_lines(result: dict[str, Any], mark: str, text: dict[str, str], label: str, reason: Any = None) -> str:
    """One check line, then one indented line per finding (shared by both languages)."""
    if result["status"] == NOT_VERIFIED:
        return f"  ? {label}: {text['_not_checked']} ({reason if reason is not None else result.get('reason')})"
    found = result.get("findings") or []
    counts = result.get("counts") or {}
    funcs, asserts = counts.get("test_functions", {}), counts.get("assertions", {})
    tally = text["_counts"].format(fa=funcs.get("added", 0), fr=funcs.get("removed", 0), aa=asserts.get("added", 0), ar=asserts.get("removed", 0))
    if not found:
        return f"  {mark} {label}: {text['_ok']} ({tally})"
    head = text["_approved"] if result.get("approved_by_caller") else text["_failed"].format(n=len(found))
    lines = [f"  {mark} {label}: {head} ({tally})"]
    for item in found[:8]:
        where = item["file"] + (f":{item['line']}" if item.get("line") else "")
        detail = f" ({item['detail']})" if item.get("detail") else ""
        by = text["_by"].format(n=item["count"]) if item.get("count") else ""
        lines.append(f"      {text.get(item['kind'], item['kind'])}{by} — {where}{detail}")
    return "\n".join(lines)


def resolve_lang(explicit: str | None = None) -> str:
    """``--lang``, then ``$SKILLLAYER_LANG`` (``ru``, ``ru_RU.UTF-8`` …), then English."""
    value = (explicit or os.environ.get(LANG_ENV) or "en").strip().lower()[:2]
    return value if value in LANGUAGES else "en"


def render_gate_report(receipt: dict[str, Any], *, receipt_path: Path | None = None, lang: str = "en") -> str:
    if lang == "ru":
        return _render_gate_report_ru(receipt, receipt_path=receipt_path)
    verdict = receipt["verdict"]
    mark = {GATE_VERIFIED: "✓", GATE_BLOCKED: "✗", GATE_NO_CHANGES: "·"}.get(verdict, "?")
    commits = receipt["commits"]
    lines = [
        f"skilllayer gate — {mark} {verdict}",
        f"  change set: {receipt['merge_base'][:10]}..{receipt['head']['sha'][:10]} "
        f"({len(commits)} commit{'s' if len(commits) != 1 else ''}, {receipt['ai_assisted_commits']} marked AI-assisted, "
        f"{receipt['changes']['paths_total']} file{'s' if receipt['changes']['paths_total'] != 1 else ''})",
    ]
    lines += [_check_line(r) for r in receipt.get("checks") or []]
    for note in receipt.get("notes") or []:
        if note["kind"] == "test_files_touched":
            lines.append(f"  note: test files changed — {', '.join((note['modified'] + note['deleted'])[:5])}")
        elif note["kind"] == "agent_configuration_modified":
            lines.append(f"  note: agent configuration changed — {note['path']}")
    if receipt.get("limitations"):
        lines.append(f"  limitations: {receipt['limitations']}")
    if receipt_path is not None:
        lines.append(f"  receipt: {receipt_path}  ({(receipt.get('integrity') or {}).get('digest', 'unsealed')[:23]}…)")
    if verdict == GATE_BLOCKED:
        lines.append("Not accepted: a required check failed on the commits that would land. Fix it and push again.")
    elif verdict == GATE_UNVERIFIED:
        lines.append("Not accepted: a required check was not observed to pass, and an unverified change is not a pass.")
    if receipt["mode"] == "warn" and verdict in {GATE_BLOCKED, GATE_UNVERIFIED}:
        lines.append("(warn mode: reported only, nothing was blocked)")
    return "\n".join(lines)


def render_receipt_check(result: dict[str, Any], *, lang: str = "en") -> str:
    if lang == "ru":
        state = "действительна" if result["valid"] else "НЕДЕЙСТВИТЕЛЬНА"
        return f"skilllayer gate — квитанция {state}: {_RECEIPT_REASONS_RU.get(result['reason'], result['reason'])}"
    return f"skilllayer gate — receipt {'valid' if result['valid'] else 'INVALID'}: {result['reason']}"


# --------------------------------------------------------------------------- rendering, Russian
#
# Counts read "коммитов: 3" rather than "3 коммита": the label-then-number form is correct
# Russian for every number, with no plural agreement to get wrong.

_VERDICT_RU = {
    GATE_VERIFIED: "ПРИНЯТО",
    GATE_BLOCKED: "ЗАБЛОКИРОВАНО",
    GATE_UNVERIFIED: "НЕ ПРОВЕРЕНО",
    GATE_NO_CHANGES: "НЕТ ИЗМЕНЕНИЙ",
}

_REASONS_RU = {
    "head_not_checked_out": "проверяемый коммит не выгружен",
    "uncommitted_changes": "в рабочей копии есть незакоммиченные изменения",
    "git_status_unavailable": "не удалось выполнить git status",
    "policy_invalid_at_base": "политику в базовом коммите не удалось прочитать",
    "history_unavailable": "история коммитов недоступна",
    "history_too_large": "история слишком большая для проверки",
    "command_not_found": "команда не найдена",
    "permission_denied": "нет прав на запуск",
    "unparseable_command": "команду не удалось разобрать",
    "empty_command": "пустая команда",
    "flaky_tests": "тесты нестабильны",
}

_INTEGRITY_RU = {
    "skip_added": "добавлен skip или xfail",
    "tautological_assertion": "ассерт, который не может упасть",
    "tests_deselected": "тесты отключены в конфигурации",
    "threshold_lowered": "понижен порог",
    "tests_removed": "тестов стало меньше",
    "assertions_removed": "ассертов стало меньше",
    "test_file_deleted": "удалён файл тестов",
    "_ok": "тесты не ослаблены",
    "_approved": "ослаблены с разрешения",
    "_failed": "признаков ослабления: {n}",
    "_not_checked": "не проверена",
    "_counts": "тестов +{fa}/−{fr}, ассертов +{aa}/−{ar}",
    "_by": " на {n}",
}

_RECEIPT_REASONS_RU = {
    "signature_ok": "подпись верна",
    "digest_ok_unsigned": "отпечаток совпадает, квитанция не подписана",
    "digest_ok_signature_not_checked": "отпечаток совпадает, подпись не проверялась: ключ не задан",
    "digest_mismatch": "отпечаток не совпадает — квитанцию изменили после выдачи",
    "signature_mismatch": "подпись не совпадает — квитанцию изменил тот, у кого нет ключа",
    "unsigned_receipt": "квитанция не подписана, хотя ключ задан",
    "no_integrity_block": "в квитанции нет блока целостности",
}


def _reason_ru(reason: Any) -> str:
    parts: list[str] = []
    for code in str(reason or "").split(","):
        if code.startswith("timeout_after_"):
            parts.append(f"превышено время: {code.removeprefix('timeout_after_').rstrip('s')} с")
        elif code.startswith("os_error:"):
            parts.append(f"ошибка ОС {code.split(':', 1)[1]}")
        elif code:
            parts.append(_REASONS_RU.get(code, code))
    return "; ".join(parts) or "причина не указана"


# Limitation codes are "name" or "name:detail"; unknown ones are shown as they are.
_LIMITATIONS_RU = {
    "output_tail_lines_withheld": "скрыто строк вывода (могли содержать секреты)",
    "binary_files_not_scanned": "бинарных файлов не проверено на секреты",
    "commit_list_truncated": "список коммитов обрезан до",
    "test_python_fallback": "тесты запущены интерпретатором",
    "policy_ignored": "политика не прочитана",
    "commit_list_unavailable": "список коммитов недоступен",
    "committed_changes_unavailable": "список изменённых файлов недоступен",
}


def _limitation_ru(code: str) -> str:
    name, sep, detail = code.partition(":")
    label = _LIMITATIONS_RU.get(name)
    if label is None:
        return code
    return f"{label}: {detail}" if sep else label


def _seconds_ru(ms: Any) -> str:
    return f"{(ms or 0) / 1000:.1f}".replace(".", ",") + " с"


def _tests_detail_ru(result: dict[str, Any]) -> str:
    command = re.sub(r"^/\S*/(python[0-9.]*)\b", r"\1", result.get("command") or "(команда тестов не найдена)")
    bits = [f"код {result['exit_code']}" if result.get("exit_code") is not None else None,
            _seconds_ru(result.get("duration_ms")) if result.get("duration_ms") else None]
    detail = ", ".join(b for b in bits if b)
    return f"{command} → {result.get('summary') or result.get('outcome')}" + (f" ({detail})" if detail else "")


def _check_line_ru(result: dict[str, Any]) -> str:
    name, status = result["name"], result["status"]
    mark = _MARKS.get(status, "?")
    if name == "tests":
        detail = _tests_detail_ru(result) if result.get("outcome") else f"не запускались ({_reason_ru(result.get('reason'))})"
        lines = [f"  {mark} тесты: {detail}"]
        if result.get("flaky_tests"):
            how = "нестабильность принята" if result.get("flaky_accepted_by_caller") else "нестабильны или зависят от порядка — это не «прошло»"
            lines.append(f"      упали, затем прошли при перезапуске — {how}: {', '.join(result['flaky_tests'][:5])}")
            return "\n".join(lines)
        lines += [f"      {failed}" for failed in (result.get("failed_tests") or [])[:10]]
        return "\n".join(lines)
    if name == "protected_paths":
        if status == NOT_VERIFIED:
            return f"  ? защищённые пути: не проверены ({_reason_ru(result.get('reason'))})"
        if result.get("approved_by_caller"):
            return f"  {mark} защищённые пути: изменены с разрешения — {', '.join(result['hits'][:5])}"
        if result["hits"]:
            return f"  {mark} защищённые пути: {', '.join(result['hits'][:5])} (политика взята из базового коммита)"
        return f"  {mark} защищённые пути: не затронуты"
    if name == "secrets":
        if status == NOT_VERIFIED:
            return f"  ? секреты: не проверены ({_reason_ru(result.get('reason'))})"
        found = result.get("findings") or []
        if not found:
            return f"  ✓ секреты: не найдены (проверено добавленных строк: {result.get('added_lines_scanned', 0)})"
        shown = ", ".join(f"{f['file']}:{f['line']} ({f['pattern']}, коммит {f['commit']})" for f in found[:5])
        return f"  ✗ секреты: найдено {len(found)} — {shown}"
    if name == "test_integrity":
        return _integrity_lines(result, mark, _INTEGRITY_RU, "целостность тестов", reason=_reason_ru(result.get("reason")))
    label = "проверка " + name.removeprefix("check:")
    if status == NOT_VERIFIED:
        return f"  ? {label}: не выполнена ({_reason_ru(result.get('reason'))})"
    return f"  {mark} {label}: код {result.get('exit_code')} ({_seconds_ru(result.get('duration_ms'))}) — {result.get('command')}"


def _render_gate_report_ru(receipt: dict[str, Any], *, receipt_path: Path | None) -> str:
    verdict = receipt["verdict"]
    mark = {GATE_VERIFIED: "✓", GATE_BLOCKED: "✗", GATE_NO_CHANGES: "·"}.get(verdict, "?")
    lines = [
        f"skilllayer gate — {mark} {_VERDICT_RU.get(verdict, verdict)} ({verdict})",
        f"  изменения: {receipt['merge_base'][:10]}..{receipt['head']['sha'][:10]} — коммитов: {len(receipt['commits'])}, "
        f"с пометкой ИИ: {receipt['ai_assisted_commits']}, файлов: {receipt['changes']['paths_total']}",
    ]
    lines += [_check_line_ru(r) for r in receipt.get("checks") or []]
    for note in receipt.get("notes") or []:
        if note["kind"] == "test_files_touched":
            lines.append(f"  примечание: изменены файлы тестов — {', '.join((note['modified'] + note['deleted'])[:5])}")
        elif note["kind"] == "agent_configuration_modified":
            lines.append(f"  примечание: изменена конфигурация агента — {note['path']}")
    if receipt.get("limitations"):
        lines.append("  ограничения: " + "; ".join(_limitation_ru(str(code)) for code in receipt["limitations"]))
    if receipt_path is not None:
        lines.append(f"  квитанция: {receipt_path}  ({(receipt.get('integrity') or {}).get('digest', 'не запечатана')[:23]}…)")
    if verdict == GATE_BLOCKED:
        lines.append("Не принято: обязательная проверка не прошла на коммитах, которые попали бы в ветку. Исправьте и отправьте снова.")
    elif verdict == GATE_UNVERIFIED:
        lines.append("Не принято: обязательная проверка не подтвердилась, а непроверенное изменение не считается прошедшим.")
    if receipt["mode"] == "warn" and verdict in {GATE_BLOCKED, GATE_UNVERIFIED}:
        lines.append("(режим warn: только отчёт, ничего не заблокировано)")
    return "\n".join(lines)
