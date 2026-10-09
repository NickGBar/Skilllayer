"""Claude Code hook handlers for ``skilllayer verify``.

Two hooks cooperate:

* ``UserPromptSubmit`` snapshots the repository (HEAD + working-tree hash) when a
  turn begins, and resets the per-turn block budget.
* ``Stop`` runs when the agent tries to finish. If the work changed during the turn
  it is verified independently; failing tests or a protected-path edit block the
  stop and hand the observed facts back to the agent (a JSON block decision, or exit 2).

Design constraints, each of them a way this layer could silently stop protecting:

* The repository policy is read from the **committed** version at the turn baseline,
  never the working tree — an agent cannot loosen its own guardrails by editing the
  policy file mid-turn (and touching that file is itself a violation).
* Commits made during the turn count: changes are measured from the baseline HEAD,
  not only from the dirty tree.
* The block budget is finite. After ``max_consecutive_blocks`` the hook lets the agent
  stop but says so loudly and records the work as unverified — a verification layer
  that can trap a session forever will simply be uninstalled.
* Internal failures are visible (a message the user sees inline), never a silent pass.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .policy import POLICY_FILES, evaluate_policy_text, load_policy
from .verify import (
    UNVERIFIED_VERDICTS,
    VerifyConfig,
    current_head,
    data_dir,
    find_git_root,
    record_report,
    render_agent_message,
    render_user_summary,
    show_committed_file,
    take_snapshot,
    verify_repo,
)

_SESSION_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class HookOutcome:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


# --------------------------------------------------------------------------- config & state


def load_verify_config(root: Path, ref: str | None) -> tuple[VerifyConfig, list[str]]:
    """Enforcement settings from the policy committed at ``ref``. Any problem with the
    policy falls back to strict defaults and is reported, never silently relaxed."""
    if not ref:
        return VerifyConfig(), []
    for name in POLICY_FILES:
        text = show_committed_file(root, ref, name)
        if text is None:
            continue
        result = evaluate_policy_text(text, policy_path=name)
        if result["status"] != "POLICY_VALID":
            codes = ",".join(e["code"] for e in result["errors"]) or result["status"]
            return VerifyConfig(), [f"policy_ignored:{name}:{codes}"]
        return config_from_policy(result["normalized_policy"]), []
    return VerifyConfig(), []


def config_from_policy(policy: dict[str, Any]) -> VerifyConfig:
    verify = policy["verify"]
    return VerifyConfig(
        mode=verify["mode"],
        protected_paths=tuple(policy["protected_paths"]),
        max_consecutive_blocks=verify["max_consecutive_blocks"],
        block_on_unverified=verify["block_on_unverified"],
        test_timeout_seconds=verify["test_timeout_seconds"],
        block_on_weakened_tests=verify.get("block_on_weakened_tests", True),
        agent_scopes=tuple((name, tuple(rules)) for name, rules in policy.get("agent_scopes", {}).items()),
    )


def load_worktree_config(root: Path) -> tuple[VerifyConfig, list[str]]:
    """For a human running ``skilllayer verify`` by hand: the policy as it is on disk now."""
    result = load_policy(root)
    if result["status"] == "POLICY_NOT_PRESENT":
        return VerifyConfig(), []
    if result["status"] != "POLICY_VALID":
        codes = ",".join(e["code"] for e in result["errors"]) or result["status"]
        return VerifyConfig(), [f"policy_ignored:{result['policy_path']}:{codes}"]
    return config_from_policy(result["normalized_policy"]), []


def _session_id(payload: dict[str, Any]) -> str:
    raw = str(payload.get("session_id") or "nosession")
    return _SESSION_RE.sub("_", raw)[:64] or "nosession"


def _state_path(root: Path, session_id: str) -> Path:
    return data_dir(root) / "state" / f"{session_id}.json"


def load_state(root: Path, session_id: str) -> dict[str, Any]:
    try:
        return json.loads(_state_path(root, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(root: Path, session_id: str, state: dict[str, Any]) -> bool:
    path = _state_path(root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- handlers


def handle_prompt_submit(payload: dict[str, Any]) -> HookOutcome:
    """Record the turn baseline. Prints nothing: UserPromptSubmit stdout would be
    injected into the model's context."""
    try:
        root = find_git_root(Path(payload.get("cwd") or os.getcwd()))
        if root is None:
            return HookOutcome(0)
        session = _session_id(payload)
        state = load_state(root, session)
        snap = take_snapshot(root)
        state["baseline"] = {"head": snap.head, "tree_hash": snap.tree_hash}
        state["consecutive_blocks"] = 0
        state["last_block_tree_hash"] = None
        save_state(root, session, state)
        return HookOutcome(0)
    except Exception as exc:  # noqa: BLE001 - a hook must report, not crash the session
        return HookOutcome(1, stderr=f"skilllayer verify (prompt hook): internal error ({type(exc).__name__}: {exc}); turn baseline not recorded.")


def _warning(text: str) -> str:
    return json.dumps({"systemMessage": text})


def _observations(report: dict[str, Any]) -> list[str]:
    """Facts worth telling the user even though nothing is blocked."""
    notes: list[str] = []
    for finding in report["findings"]:
        if finding["kind"] == "test_files_touched":
            touched = finding["modified"] + finding["deleted"]
            notes.append("test files were changed this turn (" + ", ".join(touched[:5]) + (", ..." if len(touched) > 5 else "") + ")")
        elif finding["kind"] == "agent_configuration_modified":
            notes.append(f"agent configuration was changed ({finding['path']})")
        elif finding["kind"] == "tests_weakened" and not finding.get("blocking"):
            notes.append("tests were weakened this turn: " + "; ".join(finding["described"][:3]))
    return notes


def handle_stop(
    payload: dict[str, Any],
    *,
    test_command: list[str] | None = None,
    record: bool = True,
    config_override: VerifyConfig | None = None,
    max_seconds: int | None = None,
) -> HookOutcome:
    try:
        return _handle_stop(payload, test_command=test_command, record=record, config_override=config_override, max_seconds=max_seconds)
    except Exception as exc:  # noqa: BLE001
        # Exit 0 with a systemMessage: the harness shows that inline ("Stop says: ..."), while an
        # exit-1 hook only yields a generic "Stop hook error occurred" with the detail hidden.
        return HookOutcome(0, stdout=_warning(
            f"skilllayer verify: internal error ({type(exc).__name__}: {exc}); this Stop was NOT verified."
        ))


def _block(report: dict[str, Any], *, attempt: int, max_attempts: int) -> HookOutcome:
    """Send the agent back to work. Two protocols carry the same message; both reach the model
    as "Stop hook feedback", but only the JSON one can also show the *human* the reason (the
    harness surfaces an exit-2 block as a generic "Stop hook error"). Checked against Claude
    Code 2.1.275 by scripts/e2e_claude_code_hook.py; SKILLLAYER_BLOCK_STYLE=exit2 selects the
    older exit-code protocol for a harness that does not honour the JSON decision."""
    message = render_agent_message(report, attempt=attempt, max_attempts=max_attempts)
    if os.environ.get("SKILLLAYER_BLOCK_STYLE") == "exit2":
        return HookOutcome(2, stderr=message)
    summary = render_user_summary(report, attempt=attempt, max_attempts=max_attempts)
    return HookOutcome(0, stdout=json.dumps({"decision": "block", "reason": message, "systemMessage": summary}))


def _handle_stop(payload: dict[str, Any], *, test_command: list[str] | None, record: bool, config_override: VerifyConfig | None, max_seconds: int | None) -> HookOutcome:
    root = find_git_root(Path(payload.get("cwd") or os.getcwd()))
    if root is None:
        return HookOutcome(0)  # not a git repository: nothing to verify against
    session = _session_id(payload)
    state = load_state(root, session)
    baseline = state.get("baseline") or {}
    baseline_head = baseline.get("head")

    notes: list[str] = []
    if config_override is not None:
        config = config_override
    else:
        config, notes = load_verify_config(root, baseline_head or current_head(root))
    if max_seconds is not None and config.test_timeout_seconds > max_seconds:
        # The harness kills a hook that outlives its own timeout and then allows the stop
        # silently; ending the test run first turns that into a visible UNVERIFIED_TIMEOUT.
        config = dataclasses.replace(config, test_timeout_seconds=max_seconds)
        notes = [*notes, "test_timeout_clamped_to_hook_budget"]

    snap = take_snapshot(root)
    if snap.tree_hash in {state.get("last_verified_tree_hash"), baseline.get("tree_hash")}:
        return HookOutcome(0)  # nothing new since the turn began or since the last verification

    report = verify_repo(root, config=config, test_command=test_command, baseline_head=baseline_head, session_id=session)
    report["limitations"] = sorted(set(report["limitations"]) | set(notes))
    blocks_so_far = int(state.get("consecutive_blocks") or 0)
    stop_hook_active = bool(payload.get("stop_hook_active"))

    if report["blocked"]:
        attempt = blocks_so_far + 1
        exhausted = attempt > config.max_consecutive_blocks
        state["consecutive_blocks"] = attempt
        state["last_block_tree_hash"] = report["tree_hash_after"]
        persisted = save_state(root, session, state)
        # Guard against loops even if our own bookkeeping failed: a second stop in a row
        # (stop_hook_active) with no way to count attempts must not block again.
        if exhausted or (stop_hook_active and not persisted):
            report["blocked"] = False
            report["loop_guard"] = True
            state.update(consecutive_blocks=0, last_verified_tree_hash=report["tree_hash_after"])
            save_state(root, session, state)
            if record:
                record_report(root, report)
            return HookOutcome(0, stdout=_warning(
                f"skilllayer: allowing this stop after {max(blocks_so_far, 1)} blocked attempt(s) — "
                f"the work is UNVERIFIED ({report['verdict']}). Review it before relying on it."
            ))
        if record:
            record_report(root, report)
        return _block(report, attempt=attempt, max_attempts=config.max_consecutive_blocks)

    state.update(consecutive_blocks=0, last_verified_tree_hash=report["tree_hash_after"])
    save_state(root, session, state)
    if record:
        record_report(root, report)
    observations = _observations(report)
    if report["verdict"] in UNVERIFIED_VERDICTS:
        message = (
            f"skilllayer: this work is {report['verdict']} — the tests were not observed to pass "
            f"({report['tests'].get('summary') or report['tests'].get('outcome')})."
        )
    elif config.mode == "warn" and report["findings"] and report["verdict"] != "VERIFIED":
        message = f"skilllayer (warn mode): verification found {report['verdict']} — not blocking."
    elif observations:
        return HookOutcome(0, stdout=_warning(f"skilllayer: tests passed, but {'; '.join(observations)} — review before trusting the result."))
    else:
        return HookOutcome(0)
    if observations:
        message += f" Also: {'; '.join(observations)}."
    return HookOutcome(0, stdout=_warning(message))
