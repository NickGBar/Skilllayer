"""Independent verification that an agent's "done" is true — observed, never claimed.

Shared by the ``skilllayer verify`` CLI and the Claude Code Stop hook
(``skilllayer.verify_hook``). The rule that governs every verdict here is the one
the professional skills already follow: a check that did not actually run never
becomes a clean result. Tests are *executed by this module* and their observed
outcome is what counts; nothing the agent says about them is an input.

Facts come from three independent observations: the test command's real exit
status (via the existing ``TestRunner``), live git state (working tree plus any
commits made since a baseline), and the repository policy's protected paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .policy import POLICY_FILES
from .runner.core import build_run_tests_artifacts
from .tasks.baseline import _parse_porcelain_z, fingerprint_relevant_files
from .tasks.persistence import FieldPolicy, sanitize_persisted_value
from .verifier.test_runner import TestRunner

RECEIPT_VERSION = 1

VERIFIED = "VERIFIED"
TESTS_FAILING = "TESTS_FAILING"
POLICY_VIOLATION = "POLICY_VIOLATION"
UNVERIFIED_NO_TESTS = "UNVERIFIED_NO_TESTS"
UNVERIFIED_ENVIRONMENT = "UNVERIFIED_ENVIRONMENT"
UNVERIFIED_TIMEOUT = "UNVERIFIED_TIMEOUT"
UNVERIFIED_UNKNOWN = "UNVERIFIED_UNKNOWN"

BLOCKING_VERDICTS = frozenset({TESTS_FAILING, POLICY_VIOLATION})
UNVERIFIED_VERDICTS = frozenset(
    {UNVERIFIED_NO_TESTS, UNVERIFIED_ENVIRONMENT, UNVERIFIED_TIMEOUT, UNVERIFIED_UNKNOWN}
)

# Observed test outcome (runner/core.classify_run_tests_outcome) -> verdict.
# COMMAND_ERROR means the test command exited non-zero with no parseable failure
# list: a definite observed failure, not an unknown, so it blocks like a failing run.
_OUTCOME_TO_VERDICT = {
    "PASSED": VERIFIED,
    "FAILED_TESTS": TESTS_FAILING,
    "COMMAND_ERROR": TESTS_FAILING,
    "TIMEOUT": UNVERIFIED_TIMEOUT,
    "ENVIRONMENT_MISMATCH": UNVERIFIED_ENVIRONMENT,
    "NO_TESTS_DISCOVERED": UNVERIFIED_NO_TESTS,
    "NO_TEST_COMMAND": UNVERIFIED_NO_TESTS,
}

# The repository policy defines enforcement, so an agent that edits it is changing
# its own guardrails. Always treated as protected, in addition to configured paths.
_INTEGRITY_NOTE_PATHS = (".claude/settings.json", ".claude/settings.local.json")

_TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[A-Za-z0-9]+$|\.(test|spec)\.[A-Za-z0-9]+$"
)
_TAIL_CHARS = 1200
_MAX_LISTED_PATHS = 200


@dataclass(frozen=True)
class VerifyConfig:
    mode: str = "block"  # "block" enforces; "warn" only reports
    protected_paths: tuple[str, ...] = ()
    max_consecutive_blocks: int = 2
    block_on_unverified: bool = False
    test_timeout_seconds: int = 300

    def effective_protected(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*POLICY_FILES, *self.protected_paths)))


# --------------------------------------------------------------------------- state dirs


def data_dir(root: Path) -> Path:
    """Per-repository, per-user directory for state, receipts and the event log.

    Deliberately outside the repository: verification never dirties the working
    tree it is judging, and works whether or not ``.skilllayer/`` is gitignored.
    """
    base = Path(os.environ["SKILLLAYER_VERIFY_DIR"]) if os.environ.get("SKILLLAYER_VERIFY_DIR") else Path.home() / ".skilllayer" / "verify"
    resolved = root.resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved.name).strip("-") or "repo"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return base / f"{slug}-{digest}"


# --------------------------------------------------------------------------- git helpers


def _git(root: Path, *args: str, timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_git_root(start: Path) -> Path | None:
    proc = _git(start, "rev-parse", "--show-toplevel")
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()).resolve()


def current_head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def show_committed_file(root: Path, ref: str, path: str) -> str | None:
    """Content of ``path`` as committed at ``ref`` (None when absent)."""
    proc = _git(root, "show", f"{ref}:{path}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout


def _is_internal(path: str) -> bool:
    return path == ".skilllayer" or path.startswith(".skilllayer/")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _matches_rule(path: str, rule: str) -> bool:
    rule = rule.strip()
    if not rule:
        return False
    if rule.endswith("/"):
        return path.startswith(rule)
    return path == rule or path.startswith(rule + "/")


def _stat_token(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{st.st_mtime_ns}"


# --------------------------------------------------------------------------- snapshot


@dataclass
class Snapshot:
    head: str | None
    dirty_paths: list[str]
    deleted_paths: list[str]
    limitations: list[str]
    tree_hash: str


def take_snapshot(root: Path) -> Snapshot:
    """Head + uncommitted state, reduced to a stable hash.

    Untracked files are listed individually (``--untracked-files=all``) so edits
    inside a new directory still change the hash. Files the fingerprint budget
    skips fall back to size+mtime, so *any* content change moves the hash.
    """
    head = current_head(root)
    status = _git(root, "status", "--porcelain=v1", "-z", "--renames", "--untracked-files=all")
    if status is None or status.returncode != 0:
        token = hashlib.sha256(f"{head}|status-unavailable|{time.time_ns()}".encode()).hexdigest()
        return Snapshot(head, [], [], ["git_status_unavailable"], token)
    changed, _staged, _untracked, _added, deleted, _renamed, _copied = _parse_porcelain_z(status.stdout)
    paths = [p for p in changed if not _is_internal(p)]
    deleted = [p for p in deleted if not _is_internal(p)]
    fingerprints = fingerprint_relevant_files(root, paths)
    by_path = {item["path"]: item["sha256"] for item in fingerprints["fingerprints"]}
    parts = [head or "no-head"]
    for path in sorted(paths):
        parts.append(f"{path}\0{by_path.get(path) or _stat_token(root / path)}")
    tree_hash = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return Snapshot(head, sorted(paths), sorted(deleted), list(fingerprints["limitations"]), tree_hash)


def _committed_changes(root: Path, baseline_head: str | None, head: str | None) -> tuple[list[str], list[str], list[str]]:
    """Paths changed by commits made since ``baseline_head``: (paths, deleted, limitations)."""
    if not baseline_head or not head or baseline_head == head:
        return [], [], []
    proc = _git(root, "diff", "--name-status", "-z", "--no-renames", baseline_head, head)
    if proc is None or proc.returncode != 0:
        return [], [], ["committed_changes_unavailable"]
    tokens = [t for t in proc.stdout.split("\0") if t]
    paths: list[str] = []
    deleted: list[str] = []
    for status, path in zip(tokens[0::2], tokens[1::2]):
        if _is_internal(path):
            continue
        paths.append(path)
        if status.startswith("D"):
            deleted.append(path)
    return sorted(set(paths)), sorted(set(deleted)), []


def collect_changes(root: Path, snapshot: Snapshot, baseline_head: str | None) -> dict[str, Any]:
    committed, committed_deleted, limitations = _committed_changes(root, baseline_head, snapshot.head)
    paths = sorted(set(snapshot.dirty_paths) | set(committed))
    deleted = sorted(set(snapshot.deleted_paths) | set(committed_deleted))
    existing = [p for p in paths if p not in set(deleted)]
    return {
        "baseline_head": baseline_head,
        "head": snapshot.head,
        "paths": paths,
        "deleted": deleted,
        "tests_modified": [p for p in existing if is_test_path(p)],
        "tests_deleted": [p for p in deleted if is_test_path(p)],
        "limitations": sorted(set(limitations) | set(snapshot.limitations)),
    }


# --------------------------------------------------------------------------- observation

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _gate(text: str, limit: int = 300) -> tuple[str | None, str | None]:
    """One line through the persistence gate (it rejects control characters and
    redacts secrets): (value, None), or (None, why it was refused)."""
    gate = sanitize_persisted_value(text[:limit], FieldPolicy.REDACTABLE_TEXT, max_length=limit, field_name="test_output_tail")
    if gate.accepted and gate.sanitized_value is not None:
        return gate.sanitized_value, None
    return None, str(gate.rejection_reason or "refused")


def _safe_line(text: str, limit: int = 300) -> str | None:
    return _gate(text, limit)[0]


def _sanitized_tail(text: str, max_lines: int = 25) -> tuple[str | None, list[str]]:
    """Last lines of test output, each individually sanitized; refused lines are dropped
    and counted rather than silently hidden."""
    lines = _ANSI_RE.sub("", text).splitlines()[-max_lines:]
    kept: list[str] = []
    dropped = 0
    for raw in lines:
        line = raw.rstrip()
        if not line:
            kept.append("")
            continue
        safe = _safe_line(line)
        if safe is None:
            dropped += 1
        else:
            kept.append(safe)
    tail = "\n".join(kept).strip()[-_TAIL_CHARS:] or None
    return tail, ([f"output_tail_lines_withheld:{dropped}"] if dropped else [])


# A pytest node name: the test function, then an optional parametrization id.
_NODE_NAME_RE = re.compile(r"(?P<func>[A-Za-z_][A-Za-z0-9_]*)(?P<params>\[.*\])?")


def _is_word_identifier(name: str) -> bool:
    """A snake_case name built from short, mostly word-like parts, e.g.
    ``test_save10_never_takes_off_more_than_50_dollars``. Keys and tokens are one long
    run of characters, not a sentence of short words."""
    parts = [part for part in name.split("_") if part]
    return len(parts) >= 3 and all(len(part) <= 16 for part in parts) and 2 * sum(part.isalpha() for part in parts) >= len(parts)


def _safe_test_name(name: str) -> str:
    """A failing test's name for the agent, never dropped. The gate's entropy heuristic
    refuses any 32+ character token mixing letters and digits — which a descriptive test
    name often is — so a word-like function name is kept unless it matches a known secret
    shape. A parametrization id can carry real data and still goes through the full gate."""
    match = _NODE_NAME_RE.fullmatch(name)
    if not match:
        return _safe_line(name, 200) or "(test name withheld)"
    func, params = match.group("func"), match.group("params") or ""
    safe_func, reason = _gate(func, 200)
    if safe_func is None:
        heuristic_only = bool(reason) and reason.endswith("low_confidence_sensitive_token")
        safe_func = func if heuristic_only and _is_word_identifier(func) else "(test name withheld)"
    if params:
        params = _safe_line(params, 120) or "[…]"
    return safe_func + params


def _failed_test_labels(items: list[Any]) -> list[str]:
    """``file::test — first line of the assertion`` per failing test, deduplicated.
    The runner reports a pytest entry and a traceback entry for one failure; prefer
    the named entries. Each part is sanitized on its own, so a refused assertion text
    or parameter never costs the agent the name of the test that failed."""
    entries = [i for i in items if isinstance(i, dict)]
    chosen = [i for i in entries if i.get("test_name")] or entries
    labels: list[str] = []
    for item in chosen:
        where, name = str(item.get("file") or ""), str(item.get("test_name") or "")
        where = (_safe_line(where, 200) or "(path withheld)") if where else ""
        name = _safe_test_name(name) if name else ""
        base = f"{where}::{name}" if where and name else (where or name or str(item.get("kind") or "failure"))
        snippet = str(item.get("snippet") or "").strip().splitlines()[:1]
        detail = _safe_line(snippet[0], 160) if snippet else None
        label = f"{base} — {detail}" if detail else base
        if label not in labels:
            labels.append(label)
    labels.extend(_safe_line(str(i), 200) or "(failure withheld)" for i in items if not isinstance(i, dict))
    return labels[:20]


@contextmanager
def _wide_terminal(columns: int = 200):
    """pytest cuts its one-line failure summaries to the terminal width (80 columns when there
    is no terminal), which drops the assertion exactly where it matters."""
    previous = os.environ.get("COLUMNS")
    if previous is None:
        os.environ["COLUMNS"] = str(columns)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("COLUMNS", None)


_TEST_MODULES = frozenset({"pytest", "unittest"})


def _python_can_import(python: str, module: str, cwd: Path) -> bool:
    try:
        probe = subprocess.run([python, "-c", f"import {module}"], cwd=cwd, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _fallback_test_python(root: Path, runner: TestRunner, command: list[str]) -> tuple[list[str], str | None]:
    """Find an interpreter that can actually run the detected test command.

    A pipx-style install runs this code from an isolated interpreter that has none of
    the project's test tooling. When no project-local environment was found and that
    interpreter cannot import the test framework, use the interpreter the user's own
    shell resolves (an activated virtualenv, then python3/python on PATH) rather than
    report an environment failure a different interpreter would not have. Only the
    command's interpreter is replaced, and only by one the user already runs.
    """
    if command[:1] != [sys.executable] or command[1:2] != ["-m"] or len(command) < 3 or command[2] not in _TEST_MODULES:
        return command, None
    if runner.select_python_interpreter(root).selection_source != "current_interpreter":
        return command, None
    module = command[2]
    if _python_can_import(sys.executable, module, root):
        return command, None
    own_bin = Path(sys.executable).parent
    virtual_env = os.environ.get("VIRTUAL_ENV")
    candidates = [str(Path(virtual_env) / "bin" / "python")] if virtual_env else []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        # Absolute entries only: "." or a relative entry would resolve inside the repository
        # under judgement and run whatever it planted there.
        if os.path.isabs(directory) and Path(directory) != own_bin:
            candidates += [str(Path(directory) / name) for name in ("python3", "python")]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen or not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        seen.add(candidate)
        if _python_can_import(candidate, module, root):
            return [candidate, *command[1:]], f"test_python_fallback:{candidate}"
    return command, None


def observe_tests(root: Path, *, test_command: list[str] | None = None, timeout_s: int = 300) -> dict[str, Any]:
    """Run the test command and report what actually happened.

    ``test_command`` comes from the caller's trusted configuration; when absent
    the runner's own detection is used. It is never read from a file inside the
    repository being judged.
    """
    runner = TestRunner(timeout=timeout_s)
    command = list(test_command) if test_command else runner.detect(root)
    notes: list[str] = []
    if command is not None and not test_command:
        command, note = _fallback_test_python(root, runner, command)
        if note:
            notes.append(note)
    if command is None:
        raw: dict[str, Any] = {
            "available": False, "returncode": None, "passed": 0, "failed": 0,
            "stdout": "", "stderr": "no pytest/unittest tests detected",
        }
    else:
        with _wide_terminal():
            raw = runner.run_command(root, command)
    art = build_run_tests_artifacts(raw)
    tail, limitations = _sanitized_tail(f"{art.get('stderr_snippet') or ''}\n{art.get('stdout_snippet') or ''}")
    failed = _failed_test_labels(art.get("failed_tests") or [])
    environment = art.get("environment_error_message") if art.get("environment_error") else None
    summary = art.get("summary")
    if art.get("outcome") == "ENVIRONMENT_MISMATCH" and environment:
        summary = environment  # the runner's own summary here reads "No test command detected."
    return {
        "source": "observed",
        "command": art.get("test_command") or None,
        "outcome": art.get("outcome"),
        "tests_run": bool(art.get("tests_run")),
        "tests_passed": art.get("tests_passed"),
        "exit_code": art.get("exit_code"),
        "duration_ms": art.get("duration_ms"),
        "pass_count": art.get("pass_count"),
        "failure_count": art.get("failure_count"),
        "failed_tests": failed[:20],
        "summary": summary,
        "environment_error": environment,
        "output_tail": tail,
        "limitations": [*limitations, *notes],
    }


# --------------------------------------------------------------------------- verdict


def should_block(verdict: str, config: VerifyConfig) -> bool:
    if config.mode != "block":
        return False
    if verdict in BLOCKING_VERDICTS:
        return True
    return verdict in UNVERIFIED_VERDICTS and config.block_on_unverified


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def verify_repo(
    root: Path,
    *,
    config: VerifyConfig | None = None,
    test_command: list[str] | None = None,
    baseline_head: str | None = None,
    session_id: str = "cli",
) -> dict[str, Any]:
    """Observe tests + repository facts and reach a verdict. Writes nothing."""
    config = config or VerifyConfig()
    started = time.perf_counter()
    before = take_snapshot(root)
    changes = collect_changes(root, before, baseline_head)
    tests = observe_tests(root, test_command=test_command, timeout_s=config.test_timeout_seconds)
    after = take_snapshot(root)  # test runs can leave artifacts; the hook stores this hash

    protected_rules = config.effective_protected()
    protected_hits = sorted({p for p in changes["paths"] if any(_matches_rule(p, r) for r in protected_rules)})
    integrity_notes = sorted(p for p in changes["paths"] if p in _INTEGRITY_NOTE_PATHS)

    findings: list[dict[str, Any]] = []
    verdict = _OUTCOME_TO_VERDICT.get(str(tests.get("outcome")), UNVERIFIED_UNKNOWN)
    if verdict == VERIFIED:
        pass
    elif verdict == TESTS_FAILING:
        findings.append({"kind": "tests_failing", "outcome": tests.get("outcome")})
    else:
        findings.append({"kind": "tests_unverified", "verdict": verdict, "outcome": tests.get("outcome")})
    for path in protected_hits:
        findings.append({"kind": "protected_path_modified", "path": path})
    if protected_hits:
        verdict = POLICY_VIOLATION
    if changes["tests_modified"] or changes["tests_deleted"]:
        findings.append({
            "kind": "test_files_touched",
            "modified": changes["tests_modified"][:20], "deleted": changes["tests_deleted"][:20],
        })
    for path in integrity_notes:
        findings.append({"kind": "agent_configuration_modified", "path": path})

    blocked = should_block(verdict, config)
    return {
        "receipt_version": RECEIPT_VERSION,
        "kind": "skilllayer.verify",
        "created_at": _now(),
        "repo": str(root),
        "session_id": session_id,
        "verdict": verdict,
        "blocked": blocked,
        "mode": config.mode,
        "findings": findings,
        "tests": tests,
        "changes": {**changes, "paths": changes["paths"][:_MAX_LISTED_PATHS], "protected_hits": protected_hits},
        "tree_hash_before": before.tree_hash,
        "tree_hash_after": after.tree_hash,
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "limitations": sorted(set(changes["limitations"]) | set(tests.get("limitations") or [])),
    }


# --------------------------------------------------------------------------- rendering


def _tests_line(tests: dict[str, Any]) -> str:
    cmd = re.sub(r"^/\S*/(python[0-9.]*)\b", r"\1", tests.get("command") or "(no test command detected)")
    bits = [f"exit {tests.get('exit_code')}" if tests.get("exit_code") is not None else None,
            f"{tests.get('duration_ms') / 1000:.1f}s" if tests.get("duration_ms") else None]
    counts = tests.get("summary")
    detail = ", ".join(b for b in bits if b)
    return f"{cmd} -> {counts or tests.get('outcome')}" + (f" ({detail})" if detail else "")


def render_agent_message(report: dict[str, Any], *, attempt: int, max_attempts: int) -> str:
    """Factual, imperative feedback returned to the agent when a Stop is blocked."""
    lines = ["skilllayer: this work cannot be accepted as complete — independent verification found problems."]
    tests = report["tests"]
    for finding in report["findings"]:
        kind = finding["kind"]
        if kind == "tests_failing":
            lines.append(f"- Tests failing (observed by running them): {_tests_line(tests)}")
            for name in tests.get("failed_tests") or []:
                lines.append(f"    {name}")
            if tests.get("output_tail") and not tests.get("failed_tests"):
                lines.append("    " + tests["output_tail"][-500:].replace("\n", "\n    "))
        elif kind == "tests_unverified":
            lines.append(f"- Tests could not be verified ({finding['verdict']}): {_tests_line(tests)}")
        elif kind == "protected_path_modified":
            # "Revert first": stopping to ask while the file is still modified is itself a
            # stop, and would be blocked again.
            lines.append(f"- Protected path modified: {finding['path']} — revert it. If the change is really needed, say so in your final message: only the user can approve it.")
        elif kind == "test_files_touched":
            touched = finding["modified"] + finding["deleted"]
            lines.append(f"- Test files were changed this turn: {', '.join(touched)} — do not weaken tests to make them pass.")
        elif kind == "agent_configuration_modified":
            lines.append(f"- Agent configuration modified: {finding['path']}")
    lines.append("Fix the issues above, then finish. Do not report success without re-running the tests.")
    lines.append(f"(Blocked attempt {attempt} of {max_attempts}; after that the hook lets you stop but records the work as UNVERIFIED.)")
    return "\n".join(lines)


def render_user_summary(report: dict[str, Any], *, attempt: int, max_attempts: int) -> str:
    """One line for the person watching: *why* the agent was sent back to work. The full
    facts go to the agent (`render_agent_message`); the harness only shows a generic
    "Stop hook error" for a block, so without this the human sees no reason at all."""
    parts: list[str] = []
    protected: list[str] = []
    for finding in report["findings"]:
        if finding["kind"] == "tests_failing":
            names = report["tests"].get("failed_tests") or []
            parts.append("tests failing" + (f" ({names[0][:140]})" if names else ""))
        elif finding["kind"] == "tests_unverified":
            parts.append(f"tests not verified ({finding['verdict']})")
        elif finding["kind"] == "protected_path_modified":
            protected.append(finding["path"])
    if protected:
        parts.append("protected path modified: " + ", ".join(protected[:3]) + (", ..." if len(protected) > 3 else ""))
    return f"skilllayer sent the agent back to work (attempt {attempt} of {max_attempts}): " + ("; ".join(parts) or "verification found problems") + "."


def render_human_report(report: dict[str, Any]) -> str:
    verdict = report["verdict"]
    marks = {VERIFIED: "✓"}
    lines = [f"skilllayer verify — {marks.get(verdict, '✗' if verdict in BLOCKING_VERDICTS else '?')} {verdict}"]
    tests = report["tests"]
    lines.append(f"  tests ({tests.get('source')}): {_tests_line(tests)}")
    changes = report["changes"]
    lines.append(f"  changed paths: {len(changes['paths'])}" + (f"  protected hits: {changes['protected_hits']}" if changes["protected_hits"] else ""))
    for finding in report["findings"]:
        if finding["kind"] == "test_files_touched":
            lines.append(f"  note: test files touched — modified={finding['modified']} deleted={finding['deleted']}")
        elif finding["kind"] == "agent_configuration_modified":
            lines.append(f"  note: agent configuration modified — {finding['path']}")
    if report["limitations"]:
        lines.append(f"  limitations: {report['limitations']}")
    if verdict in UNVERIFIED_VERDICTS:
        lines.append("  This is NOT a pass: the tests were not actually observed to succeed.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- receipts & stats


def record_report(root: Path, report: dict[str, Any]) -> list[str]:
    """Persist the receipt and append one line to the event log. Returns written paths."""
    base = data_dir(root)
    receipts = base / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    receipt_path = receipts / f"{stamp}-{report['verdict']}.json"
    receipt_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    log_path = base / "log.jsonl"
    event = {
        "ts": report["created_at"], "session_id": report["session_id"], "verdict": report["verdict"],
        "blocked": report["blocked"], "mode": report["mode"],
        "loop_guard": bool(report.get("loop_guard")),
        "kinds": sorted({f["kind"] for f in report["findings"]}),
        "duration_ms": report["duration_ms"],
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    return [str(receipt_path), str(log_path)]


def read_events(root: Path) -> list[dict[str, Any]]:
    log_path = data_dir(root) / "log.jsonl"
    if not log_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def compute_stats(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts an operator can quote: how often the layer blocked, and how often the agent
    then actually fixed it. ``recovered_after_block`` counts block sequences that ended in
    a VERIFIED result later in the same session."""
    per_session: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        per_session.setdefault(str(event.get("session_id")), []).append(event)
    recovered = 0
    for session_events in per_session.values():
        open_block = False
        for event in sorted(session_events, key=lambda e: str(e.get("ts"))):
            if event.get("blocked"):
                open_block = True
            elif event.get("verdict") == VERIFIED and open_block:
                recovered += 1
                open_block = False

    def count(pred) -> int:
        return sum(1 for e in events if pred(e))

    return {
        "verifications": len(events),
        "verified": count(lambda e: e.get("verdict") == VERIFIED),
        "blocked": count(lambda e: e.get("blocked")),
        "blocked_tests_failing": count(lambda e: e.get("blocked") and e.get("verdict") == TESTS_FAILING),
        "blocked_policy_violation": count(lambda e: e.get("blocked") and e.get("verdict") == POLICY_VIOLATION),
        "recovered_after_block": recovered,
        "loop_guard_allowed": count(lambda e: e.get("loop_guard")),
        "unverified": count(lambda e: e.get("verdict") in UNVERIFIED_VERDICTS),
        "unverified_no_tests": count(lambda e: e.get("verdict") == UNVERIFIED_NO_TESTS),
        "unverified_timeout": count(lambda e: e.get("verdict") == UNVERIFIED_TIMEOUT),
        "unverified_environment": count(lambda e: e.get("verdict") == UNVERIFIED_ENVIRONMENT),
        "test_files_touched_events": count(lambda e: "test_files_touched" in (e.get("kinds") or [])),
        "sessions": len(per_session),
        "first_event": min((str(e.get("ts")) for e in events), default=None),
        "last_event": max((str(e.get("ts")) for e in events), default=None),
    }
