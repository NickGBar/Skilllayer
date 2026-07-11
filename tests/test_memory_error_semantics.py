"""Tests for the "Memory Error Semantics Closure" milestone.

Covers the shared healthy/warning/broken/error result contract
(finalize_memory_result in memory/skilllayer_memory.py) end-to-end across
every layer a memory workflow result passes through: the core builder, the
direct MCP tool wrapper, the generic `skilllayer_run` MCP tool, and the CLI.

Existing per-workflow behavior (the 20 individual integrity checks, the
write-safety/locking primitives, CLI routing) is covered by
test_validate_memory_workflow.py, test_memory_write_safety.py,
test_memory_workflows.py, and test_memory_cli_dispatch.py respectively. This
file is scoped narrowly to cross-surface success/status/error_code/warnings
truthfulness: does every layer report the SAME thing about the SAME
underlying condition, and never launder a broken/failed result into a
falsely successful one.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import skilllayer.mcp_server as srv
from skilllayer.cli import build_parser
from skilllayer.memory.skilllayer_memory import (
    add_todo,
    mark_todo_done,
    save_context_snapshot,
    track_decision,
    validate_memory_store,
)
from skilllayer.runner.core import (
    build_add_todo_artifacts,
    build_list_todos_artifacts,
)

REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")

_HOLD_LOCK_WORKER = f"""\
import sys, time
sys.path.insert(0, {REPO_SRC!r})
from pathlib import Path
from skilllayer.memory.skilllayer_memory import memory_lock

sk = Path(sys.argv[1])
hold_seconds = float(sys.argv[2])
with memory_lock(sk, timeout=10):
    print("ACQUIRED", flush=True)
    time.sleep(hold_seconds)
print("RELEASED", flush=True)
"""


def _sk(tmp_path: Path) -> Path:
    d = tmp_path / ".skilllayer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_cli(repo: Path, task: str) -> tuple[int, dict]:
    """Invoke handle_run exactly as the real CLI entry point does, capturing
    its JSON stdout, so exit code and payload can both be asserted."""
    import io
    from contextlib import redirect_stdout

    args = build_parser().parse_args(["run", "--repo", str(repo), "--task", task, "--json"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        exit_code = args.handler(args)
    return exit_code, json.loads(buf.getvalue())


# ---------------------------------------------------------------------------
# 1. Healthy validation
# ---------------------------------------------------------------------------

class TestHealthyValidation:
    def test_clean_store_is_healthy_contract(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", ["q"])
        track_decision(sk, "Use X", "c", "d", "r", "q")
        add_todo(sk, "do thing")
        r = validate_memory_store(sk)
        assert r["success"] is True
        assert r["status"] == "healthy"
        assert r["error_code"] is None
        assert r["warnings"] == []
        assert r["summary"]["broken_count"] == 0
        assert r["summary"]["warnings_count"] == 0


# ---------------------------------------------------------------------------
# 2. Broken validation
# ---------------------------------------------------------------------------

class TestBrokenValidation:
    def test_dangling_supersession_is_broken_contract(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="9999")  # dangling
        r = validate_memory_store(sk)
        assert r["success"] is False
        assert r["status"] == "broken"
        assert r["error_code"] == "memory_integrity_broken"
        assert r["summary"]["broken_count"] >= 1

    def test_inconsistent_counter_is_broken_contract(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q")
        state_path = sk / ".state.json"
        state = json.loads(state_path.read_text())
        state["next_decision_id"] = 1  # behind the highest existing id
        state_path.write_text(json.dumps(state))
        r = validate_memory_store(sk)
        assert r["success"] is False
        assert r["status"] == "broken"
        assert r["error_code"] == "memory_integrity_broken"
        assert r["summary"]["broken_count"] >= 1


# ---------------------------------------------------------------------------
# 3. Corrupt .state.json — same underlying condition, every surface
#    (AddTodoWorkflow: claim_next_id reads .state.json before writing)
# ---------------------------------------------------------------------------

class TestCorruptStateJsonCrossSurface:
    def _corrupt(self, tmp_path) -> Path:
        sk = _sk(tmp_path)
        (sk / ".state.json").write_text("{ not valid json, definitely broken")
        return sk

    def test_builder_reports_broken(self, tmp_path):
        sk = self._corrupt(tmp_path)
        before = (sk / ".state.json").read_bytes()
        result = build_add_todo_artifacts(tmp_path, "via builder")
        assert result["success"] is False
        assert result["error_code"] == "memory_state_corrupt"
        assert (sk / ".state.json").read_bytes() == before

    def test_direct_mcp_reports_broken(self, tmp_path):
        sk = self._corrupt(tmp_path)
        before = (sk / ".state.json").read_bytes()
        result = srv.skilllayer_add_todo(str(tmp_path), "via direct mcp")
        assert result["success"] is False
        assert result["error_code"] == "memory_state_corrupt"
        assert (sk / ".state.json").read_bytes() == before

    def test_generic_mcp_reports_broken(self, tmp_path):
        sk = self._corrupt(tmp_path)
        before = (sk / ".state.json").read_bytes()
        result = srv.skilllayer_run(repo_path=str(tmp_path), task="add a todo: via generic mcp")
        assert result["success"] is False
        assert result["error_code"] == "memory_state_corrupt"
        assert (sk / ".state.json").read_bytes() == before

    def test_cli_reports_broken_with_nonzero_exit(self, tmp_path):
        sk = self._corrupt(tmp_path)
        before = (sk / ".state.json").read_bytes()
        exit_code, payload = _run_cli(tmp_path, "add a todo: via cli")
        assert exit_code != 0
        assert payload["success"] is False
        assert payload["error_code"] == "memory_state_corrupt"
        assert (sk / ".state.json").read_bytes() == before


# ---------------------------------------------------------------------------
# 4. Corrupt todos.json — same underlying condition, every surface
#    (ListTodosWorkflow: read-only, so this also proves corrupt input is
#    never silently reported as an empty-but-successful list.)
# ---------------------------------------------------------------------------

class TestCorruptTodosJsonCrossSurface:
    def _corrupt(self, tmp_path) -> Path:
        sk = _sk(tmp_path)
        add_todo(sk, "a real todo")
        (sk / "todos.json").write_text('{"todos": "not-a-list"}')
        return sk

    def test_builder_reports_broken_not_empty(self, tmp_path):
        sk = self._corrupt(tmp_path)
        before = (sk / "todos.json").read_bytes()
        result = build_list_todos_artifacts(tmp_path)
        assert result["success"] is False
        assert result["error_code"] == "todos_store_corrupt"
        assert result["total_count"] == 0
        assert (sk / "todos.json").read_bytes() == before

    def test_direct_mcp_reports_broken_not_empty(self, tmp_path):
        sk = self._corrupt(tmp_path)
        result = srv.skilllayer_list_todos(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "todos_store_corrupt"
        assert result["total_count"] == 0

    def test_generic_mcp_reports_broken_not_empty(self, tmp_path):
        sk = self._corrupt(tmp_path)
        result = srv.skilllayer_run(repo_path=str(tmp_path), task="list todos")
        assert result["success"] is False
        assert result["error_code"] == "todos_store_corrupt"

    def test_cli_reports_broken_with_nonzero_exit(self, tmp_path):
        sk = self._corrupt(tmp_path)
        exit_code, payload = _run_cli(tmp_path, "list todos")
        assert exit_code != 0
        assert payload["success"] is False
        assert payload["error_code"] == "todos_store_corrupt"


# ---------------------------------------------------------------------------
# 5. Lock timeout cross-surface
#
# One real external process holds the whole-store lock; each surface's own
# in-process call must independently time out (structured result, never a
# raw traceback), and the store must be byte-identical before/after. Each
# surface gets its own store + holder to avoid the surfaces contending with
# EACH OTHER's process-local RLock (keyed per resolved .skilllayer path) —
# that would just test in-process lock ordering, not the real cross-process
# timeout this milestone is about.
# ---------------------------------------------------------------------------

class TestLockTimeoutCrossSurface:
    def _hold_lock(self, tmp_path, name: str, hold_seconds: float = 11.0):
        sk = tmp_path / name / ".skilllayer"
        sk.mkdir(parents=True)
        add_todo(sk, "seed")  # give .state.json/todos.json real content to protect
        worker_path = tmp_path / f"_hold_{name}.py"
        worker_path.write_text(_HOLD_LOCK_WORKER, encoding="utf-8")
        holder = subprocess.Popen(
            [sys.executable, str(worker_path), str(sk), str(hold_seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert holder.stdout.readline().strip() == "ACQUIRED"
        return sk, holder

    def test_all_surfaces_time_out_with_no_partial_write(self, tmp_path):
        results: dict[str, object] = {}

        sk_builder, holder_builder = self._hold_lock(tmp_path, "builder")
        sk_mcp, holder_mcp = self._hold_lock(tmp_path, "direct_mcp")
        sk_generic, holder_generic = self._hold_lock(tmp_path, "generic_mcp")
        sk_cli, holder_cli = self._hold_lock(tmp_path, "cli")

        before = {
            "builder": (sk_builder / ".state.json").read_bytes(),
            "direct_mcp": (sk_mcp / ".state.json").read_bytes(),
            "generic_mcp": (sk_generic / ".state.json").read_bytes(),
            "cli": (sk_cli / ".state.json").read_bytes(),
        }

        def _run_builder():
            results["builder"] = build_add_todo_artifacts(sk_builder.parent, "x")

        def _run_direct_mcp():
            results["direct_mcp"] = srv.skilllayer_add_todo(str(sk_mcp.parent), "x")

        def _run_generic_mcp():
            results["generic_mcp"] = srv.skilllayer_run(repo_path=str(sk_generic.parent), task="add a todo: x")

        threads = [threading.Thread(target=fn) for fn in (_run_builder, _run_direct_mcp, _run_generic_mcp)]
        for t in threads:
            t.start()

        # CLI check runs on the main thread so it can use plain stdout capture.
        cli_exit_code, cli_payload = _run_cli(sk_cli.parent, "add a todo: x")
        results["cli_exit_code"] = cli_exit_code
        results["cli_payload"] = cli_payload

        for t in threads:
            t.join(timeout=15)
            assert not t.is_alive(), "surface call did not return within the expected window"

        for holder in (holder_builder, holder_mcp, holder_generic, holder_cli):
            holder.communicate(timeout=15)

        # AddTodoWorkflow's top-level "status" is the touched todo's own
        # open/done state, not the health word (see include_status_field in
        # finalize_memory_result) — it stays None here since no todo was
        # created.
        assert results["builder"]["success"] is False
        assert results["builder"]["error_code"] == "memory_lock_timeout"
        assert results["builder"]["status"] is None
        assert results["builder"]["todo_id"] is None

        assert results["direct_mcp"]["success"] is False
        assert results["direct_mcp"]["error_code"] == "memory_lock_timeout"

        assert results["generic_mcp"]["success"] is False
        assert results["generic_mcp"]["error_code"] == "memory_lock_timeout"

        assert results["cli_exit_code"] != 0
        assert results["cli_payload"]["success"] is False
        assert results["cli_payload"]["error_code"] == "memory_lock_timeout"

        assert (sk_builder / ".state.json").read_bytes() == before["builder"]
        assert (sk_mcp / ".state.json").read_bytes() == before["direct_mcp"]
        assert (sk_generic / ".state.json").read_bytes() == before["generic_mcp"]
        assert (sk_cli / ".state.json").read_bytes() == before["cli"]


# ---------------------------------------------------------------------------
# 6. Empty valid results must not be treated as broken
# ---------------------------------------------------------------------------

class TestEmptyValidResultsNotBroken:
    def test_no_decisions_found_is_healthy_not_broken(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])  # store exists, no decisions yet
        result = srv.skilllayer_search_decisions(str(tmp_path), "nonexistent query")
        assert result["success"] is True
        assert result.get("status") != "broken"
        assert result["error_code"] is None
        assert result["total_matches"] == 0

    def test_no_todos_is_healthy_not_broken(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])  # store exists, no todos yet
        result = srv.skilllayer_list_todos(str(tmp_path))
        assert result["success"] is True
        assert result.get("status") != "broken"
        assert result["error_code"] is None
        assert result["total_count"] == 0

    def test_no_prior_snapshot_is_not_broken(self, tmp_path):
        # Nothing to diff yet is a real inability to perform the requested
        # comparison (error, not silently empty-success) — but it must never
        # be confused with a data-integrity failure.
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "only one snapshot exists", [])
        result = srv.skilllayer_compare_context_snapshots(str(tmp_path))
        assert result["status"] != "broken"
        assert result["error_code"] != "memory_integrity_broken"
        assert result["error_code"] == "insufficient_snapshots"


# ---------------------------------------------------------------------------
# 7. Warning semantics
# ---------------------------------------------------------------------------

class TestWarningSemantics:
    def test_no_cross_process_lock_support_is_doctor_warning_not_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "MEMORY_LOCK_CROSS_PROCESS_SUPPORTED", False)
        result = srv.skilllayer_doctor(repo_path=str(tmp_path))
        # Required checks (and therefore overall doctor success) are
        # unaffected by this platform-level optional-check condition.
        assert result["success"] is True
        assert result["ok"] is True
        check = result["optional_checks"]["memory_lock_cross_process_supported"]
        assert check["ok"] is False
        assert any("fcntl" in w for w in result["warnings"])

    def test_unrelated_healthy_op_still_reports_success_despite_platform_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(srv, "MEMORY_LOCK_CROSS_PROCESS_SUPPORTED", False)
        srv.skilllayer_doctor(repo_path=str(tmp_path))  # the warning condition above
        result = srv.skilllayer_add_todo(str(tmp_path), "unaffected todo")
        assert result["success"] is True
        assert result["error_code"] is None

    def test_doctor_memory_store_status_check_is_optional_not_folded_into_overall_success(self, tmp_path):
        sk = _sk(tmp_path)
        track_decision(sk, "A", "c", "d", "r", "q")
        track_decision(sk, "B", "c", "d", "r", "q", supersedes="9999")  # broken store
        result = srv.skilllayer_doctor(repo_path=str(tmp_path))
        check = result["optional_checks"]["memory_store_status"]
        assert check["value"] == "broken"
        assert check["ok"] is False
        assert any("broken" in w.lower() for w in result["warnings"])
        # Doctor's overall required-checks success is untouched: memory
        # health is represented as an explicit optional signal, not silently
        # folded into (or breaking) the required-checks verdict.
        assert result["success"] is True
        assert result["ok"] is True

    def test_doctor_memory_store_status_healthy_store(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "state", [])
        track_decision(sk, "A", "c", "d", "r", "q")
        result = srv.skilllayer_doctor(repo_path=str(tmp_path))
        check = result["optional_checks"]["memory_store_status"]
        assert check["ok"] is True
        assert check["warning"] is None

    def test_doctor_without_repo_path_marks_memory_check_not_applicable(self):
        result = srv.skilllayer_doctor(repo_path=None)
        check = result["optional_checks"]["memory_store_status"]
        assert check["value"] == "not_applicable"
        assert check["ok"] is True


# ---------------------------------------------------------------------------
# 8. Wrapper parity — direct MCP and generic MCP agree on the same artifact
# ---------------------------------------------------------------------------

class TestWrapperParity:
    def _assert_parity(self, direct: dict, generic: dict) -> None:
        assert direct["success"] == generic["success"]
        assert direct.get("error_code") == generic.get("error_code")
        assert direct.get("status") == generic.get("status")

    def test_rehydrate_warning_parity(self, tmp_path):
        direct = srv.skilllayer_rehydrate_context(str(tmp_path))
        generic = srv.skilllayer_run(repo_path=str(tmp_path), task="rehydrate context")
        self._assert_parity(direct, generic)
        assert direct["status"] == "warning"

    def test_add_todo_healthy_parity(self, tmp_path):
        direct = srv.skilllayer_add_todo(str(tmp_path), "direct todo")
        generic = srv.skilllayer_run(repo_path=str(tmp_path), task="add a todo: generic todo")
        self._assert_parity(direct, generic)
        assert direct["success"] is True

    def test_list_todos_healthy_parity(self, tmp_path):
        sk = _sk(tmp_path)
        add_todo(sk, "seed")
        direct = srv.skilllayer_list_todos(str(tmp_path))
        generic = srv.skilllayer_run(repo_path=str(tmp_path), task="list todos")
        self._assert_parity(direct, generic)

    def test_compare_context_snapshots_error_parity(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, "only one snapshot", [])
        direct = srv.skilllayer_compare_context_snapshots(str(tmp_path))
        generic = srv.skilllayer_run(repo_path=str(tmp_path), task="compare context snapshots")
        self._assert_parity(direct, generic)
        assert direct["success"] is False

    def test_mark_todo_done_healthy_parity(self, tmp_path):
        repo_a = tmp_path / "direct"
        repo_b = tmp_path / "generic"
        added_a = add_todo(_sk(repo_a), "to be marked done")
        added_b = add_todo(_sk(repo_b), "to be marked done")
        direct = srv.skilllayer_mark_todo_done(str(repo_a), added_a["todo_id"])
        generic = srv.skilllayer_run(repo_path=str(repo_b), task=f"mark todo {added_b['todo_id']} done")
        self._assert_parity(direct, generic)
        assert direct["success"] is True


# ---------------------------------------------------------------------------
# 9. No hardcoded success — one focused test per direct memory MCP wrapper
#
# Monkeypatches each wrapper's underlying builder to return a crafted
# failure artifact and asserts the wrapper's own "success" reflects it,
# rather than being hardcoded True (the exact bug class found and fixed in
# skilllayer_validate_memory / skilllayer_save_context /
# skilllayer_remember_preferences during this milestone's audit) or silently
# re-derived in a way that loses the failure.
# ---------------------------------------------------------------------------

def _fake_failure_artifact(workflow: str) -> dict:
    return {
        "workflow": workflow,
        "error_code": "fake_forced_error",
        "error": "forced for test",
        "status": "error",
        "success": False,
        "warnings": [],
    }


class TestNoHardcodedSuccessPerWrapper:
    def test_save_context(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_save_context_artifacts",
            lambda *a, **k: _fake_failure_artifact("SaveContextSnapshotWorkflow"),
        )
        result = srv.skilllayer_save_context(str(tmp_path), "state", [])
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_track_decision(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_track_decision_artifacts",
            lambda *a, **k: _fake_failure_artifact("TrackDecisionLogWorkflow"),
        )
        result = srv.skilllayer_track_decision(str(tmp_path), "T", "c", "d", "r", "q")
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_remember_preferences(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_remember_preferences_artifacts",
            lambda *a, **k: _fake_failure_artifact("RememberUserPreferencesWorkflow"),
        )
        result = srv.skilllayer_remember_preferences(str(tmp_path), {"testing": {"runner": "pytest"}})
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_rehydrate_context(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_rehydrate_context_artifacts",
            lambda *a, **k: _fake_failure_artifact("RehydrateContextWorkflow"),
        )
        result = srv.skilllayer_rehydrate_context(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_compare_context_snapshots(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_compare_context_snapshots_artifacts",
            lambda *a, **k: _fake_failure_artifact("CompareContextSnapshotsWorkflow"),
        )
        result = srv.skilllayer_compare_context_snapshots(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_search_decisions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_search_decisions_artifacts",
            lambda *a, **k: _fake_failure_artifact("SearchDecisionLogWorkflow"),
        )
        result = srv.skilllayer_search_decisions(str(tmp_path), "query")
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_add_todo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_add_todo_artifacts",
            lambda *a, **k: _fake_failure_artifact("AddTodoWorkflow"),
        )
        result = srv.skilllayer_add_todo(str(tmp_path), "text")
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_mark_todo_done(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_mark_todo_done_artifacts",
            lambda *a, **k: _fake_failure_artifact("MarkTodoDoneWorkflow"),
        )
        result = srv.skilllayer_mark_todo_done(str(tmp_path), "0001")
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_list_todos(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_list_todos_artifacts",
            lambda *a, **k: _fake_failure_artifact("ListTodosWorkflow"),
        )
        result = srv.skilllayer_list_todos(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"

    def test_validate_memory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            srv, "build_validate_memory_artifacts",
            lambda *a, **k: _fake_failure_artifact("ValidateMemoryWorkflow"),
        )
        result = srv.skilllayer_validate_memory(str(tmp_path))
        assert result["success"] is False
        assert result["error_code"] == "fake_forced_error"
