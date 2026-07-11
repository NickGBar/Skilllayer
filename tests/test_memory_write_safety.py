"""Regression tests for the Memory Storage Safety milestone: cross-process
locking, atomic writes, read-modify-write transaction safety, and corruption
handling for the .skilllayer memory store.

Every test here is designed to FAIL against the pre-milestone implementation:
  - atomic writes: old code used direct write_text(), so a failure between
    open and write left a truncated destination file;
  - concurrency: old code had zero locking, so concurrent read-modify-write
    calls (claim_next_id, add_todo, mark_todo_done, track_decision,
    remember_preferences) could race and lose updates or mint duplicate ids;
  - corruption: old code silently replaced corrupt .state.json with a reset
    counter and persisted that reset, risking a duplicate id on the very
    next write.

Concurrency tests launch real, separate OS processes (via subprocess), not
threads, so they exercise the actual cross-process fcntl.flock guarantee —
threads alone would not detect a missing cross-process lock, since Python's
GIL already serializes pure-Python code within a single process.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.skilllayer.memory.skilllayer_memory import (
    MemoryLockTimeoutError,
    MemoryStateCorruptError,
    TodosStoreCorruptError,
    add_todo,
    atomic_write_json,
    atomic_write_text,
    claim_next_id,
    memory_lock,
    mark_todo_done,
    parse_frontmatter,
    remember_preferences,
    save_context_snapshot,
    track_decision,
    write_index,
)

REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")


_ADD_TODO_WORKER = f"""\
import sys
sys.path.insert(0, {REPO_SRC!r})
from pathlib import Path
from skilllayer.memory.skilllayer_memory import add_todo

sk = Path(sys.argv[1])
n = int(sys.argv[2])
label = sys.argv[3]
for i in range(n):
    r = add_todo(sk, f"{{label}}-{{i}}")
    assert r["error_code"] is None, r
print("OK")
"""

_TRACK_DECISION_WORKER = f"""\
import sys
sys.path.insert(0, {REPO_SRC!r})
from pathlib import Path
from skilllayer.memory.skilllayer_memory import track_decision

sk = Path(sys.argv[1])
label = sys.argv[2]
r = track_decision(sk, f"Decision {{label}}", "ctx", "dec", "reason", "conseq")
assert r["error_code"] is None, r
print(r["decision_id"])
"""

_REMEMBER_PREFS_WORKER = f"""\
import sys
sys.path.insert(0, {REPO_SRC!r})
from pathlib import Path
from skilllayer.memory.skilllayer_memory import remember_preferences

sk = Path(sys.argv[1])
key = sys.argv[2]
remember_preferences(sk, {{"testing": {{key: "value-" + key}}}})
print("OK")
"""

_SAVE_CONTEXT_WORKER = f"""\
import sys
sys.path.insert(0, {REPO_SRC!r})
from pathlib import Path
from skilllayer.memory.skilllayer_memory import save_context_snapshot

sk = Path(sys.argv[1])
label = sys.argv[2]
save_context_snapshot(sk, f"state from {{label}}", [])
print("OK")
"""

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


# ---------------------------------------------------------------------------
# 1. Atomic write success
# ---------------------------------------------------------------------------

class TestAtomicWriteSuccess:
    def test_text_write_to_existing_destination_succeeds(self, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        atomic_write_text(f, "new content")
        assert f.read_text() == "new content"

    def test_json_write_produces_valid_json(self, tmp_path):
        f = tmp_path / "data.json"
        atomic_write_json(f, {"a": 1, "b": [1, 2, 3]})
        assert json.loads(f.read_text()) == {"a": 1, "b": [1, 2, 3]}

    def test_no_temp_files_remain_after_success(self, tmp_path):
        f = tmp_path / "data.json"
        atomic_write_json(f, {"x": 1})
        atomic_write_json(f, {"x": 2})
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "data.json"]
        assert leftovers == []

    def test_write_to_new_destination_creates_parent_dirs(self, tmp_path):
        f = tmp_path / "nested" / "dir" / "data.json"
        atomic_write_json(f, {"created": True})
        assert json.loads(f.read_text()) == {"created": True}

    def test_encoding_and_newlines_preserved(self, tmp_path):
        f = tmp_path / "text.md"
        text = "line one\nline two\nünïcödé\n"
        atomic_write_text(f, text)
        assert f.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------
# 2. Failure before replace: original destination survives, no truncation
# ---------------------------------------------------------------------------

class TestAtomicWriteFailureBeforeReplace:
    def test_os_replace_failure_leaves_destination_untouched(self, tmp_path, monkeypatch):
        f = tmp_path / "important.json"
        atomic_write_json(f, {"version": 1})
        original_bytes = f.read_bytes()

        def _boom(*args, **kwargs):
            raise OSError("simulated failure during replace")

        monkeypatch.setattr("os.replace", _boom)
        with pytest.raises(OSError):
            atomic_write_json(f, {"version": 2, "should": "never appear"})

        assert f.read_bytes() == original_bytes, "destination was modified despite replace failing"

    def test_fsync_failure_leaves_destination_untouched(self, tmp_path, monkeypatch):
        f = tmp_path / "important.txt"
        atomic_write_text(f, "original content")
        original = f.read_text()

        def _boom(*args, **kwargs):
            raise OSError("simulated fsync failure")

        monkeypatch.setattr("os.fsync", _boom)
        with pytest.raises(OSError):
            atomic_write_text(f, "content that should never land")

        assert f.read_text() == original

    def test_failure_cleans_up_temp_file(self, tmp_path, monkeypatch):
        f = tmp_path / "data.json"

        def _boom(*args, **kwargs):
            raise OSError("simulated failure")

        monkeypatch.setattr("os.replace", _boom)
        with pytest.raises(OSError):
            atomic_write_json(f, {"x": 1})

        leftover_tmp = [p for p in tmp_path.iterdir() if p.name.startswith(".data.json.")]
        assert leftover_tmp == [], f"temp file(s) left behind: {leftover_tmp}"

    def test_no_destination_ever_created_on_first_write_failure(self, tmp_path, monkeypatch):
        # Destination doesn't exist yet at all — failure must not leave a
        # truncated/partial NEW file in its place.
        f = tmp_path / "brand_new.json"

        def _boom(*args, **kwargs):
            raise OSError("simulated failure")

        monkeypatch.setattr("os.replace", _boom)
        with pytest.raises(OSError):
            atomic_write_json(f, {"x": 1})

        assert not f.exists()


# ---------------------------------------------------------------------------
# 3. Concurrent todo creation — real separate processes
# ---------------------------------------------------------------------------

class TestConcurrentTodoCreation:
    def test_no_lost_updates_unique_ids_valid_json(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        n_procs, n_per_proc = 5, 3
        worker_path = tmp_path / "_add_todo_worker.py"
        worker_path.write_text(_ADD_TODO_WORKER, encoding="utf-8")

        handles = []
        for i in range(n_procs):
            p = subprocess.Popen(
                [sys.executable, str(worker_path), str(sk), str(n_per_proc), f"proc{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            handles.append(p)

        outputs = [p.communicate(timeout=30) for p in handles]
        for i, (out, err) in enumerate(outputs):
            assert handles[i].returncode == 0, f"proc{i} failed: {err}"
            assert out.strip() == "OK"

        todos_path = sk / "todos.json"
        data = json.loads(todos_path.read_text())  # must be valid JSON, not torn
        todos = data["todos"]

        assert len(todos) == n_procs * n_per_proc, "lost update: not all todos survived"
        ids = [t["id"] for t in todos]
        assert len(ids) == len(set(ids)), f"duplicate todo ids: {ids}"

        expected_texts = {f"proc{i}-{j}" for i in range(n_procs) for j in range(n_per_proc)}
        actual_texts = {t["text"] for t in todos}
        assert actual_texts == expected_texts, "some todos are missing or corrupted"

    def test_state_counter_matches_todo_count_after_concurrency(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        n_procs, n_per_proc = 4, 2
        worker_path = tmp_path / "_add_todo_worker.py"
        worker_path.write_text(_ADD_TODO_WORKER, encoding="utf-8")

        handles = [
            subprocess.Popen(
                [sys.executable, str(worker_path), str(sk), str(n_per_proc), f"p{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(n_procs)
        ]
        for p in handles:
            out, err = p.communicate(timeout=30)
            assert p.returncode == 0, err

        state = json.loads((sk / ".state.json").read_text())
        todos = json.loads((sk / "todos.json").read_text())["todos"]
        max_id = max(int(t["id"]) for t in todos)
        assert state["next_todo_id"] > max_id, "counter is behind existing ids after concurrency"


# ---------------------------------------------------------------------------
# 4. Concurrent decision creation — real separate processes
# ---------------------------------------------------------------------------

class TestConcurrentDecisionCreation:
    def test_no_duplicate_ids_all_readable(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        n_procs = 6
        worker_path = tmp_path / "_track_decision_worker.py"
        worker_path.write_text(_TRACK_DECISION_WORKER, encoding="utf-8")

        handles = [
            subprocess.Popen(
                [sys.executable, str(worker_path), str(sk), f"proc{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(n_procs)
        ]
        claimed_ids = []
        for p in handles:
            out, err = p.communicate(timeout=30)
            assert p.returncode == 0, err
            claimed_ids.append(out.strip())

        assert len(claimed_ids) == len(set(claimed_ids)), f"duplicate decision ids claimed: {claimed_ids}"

        decisions_dir = sk / "decisions"
        files = sorted(decisions_dir.glob("*.md"))
        assert len(files) == n_procs, "lost update: not all decisions survived"

        filenames_ids = set()
        for f in files:
            meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
            assert meta.get("id"), f"{f.name} has unparseable/missing frontmatter"
            assert "## Context" in body and "## Decision" in body
            filenames_ids.add(meta["id"])
        assert filenames_ids == set(claimed_ids)


# ---------------------------------------------------------------------------
# 5. Concurrent preference/context writes
# ---------------------------------------------------------------------------

class TestConcurrentPreferenceAndContextWrites:
    def test_preferences_remain_parseable_no_partial_writes(self, tmp_path):
        # Documented semantics: last-writer-wins per key (remember_preferences'
        # own contract). Under the lock, writes are serialized — no interleaved
        # merge, no torn file — but WHICH writer is last is not guaranteed by
        # this test (only that the result is a fully valid, non-corrupted file
        # reflecting some consistent, fully-applied set of writes).
        sk = tmp_path / ".skilllayer"
        n_procs = 5
        worker_path = tmp_path / "_remember_prefs_worker.py"
        worker_path.write_text(_REMEMBER_PREFS_WORKER, encoding="utf-8")

        handles = [
            subprocess.Popen(
                [sys.executable, str(worker_path), str(sk), f"key{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(n_procs)
        ]
        for p in handles:
            out, err = p.communicate(timeout=30)
            assert p.returncode == 0, err

        prefs_file = sk / "preferences.md"
        raw = prefs_file.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw)
        assert meta.get("schema") is not None, "preferences.md frontmatter is unparseable — torn write"

        # Distinct keys never collide, so append semantics guarantee ALL
        # survive (no lost updates for non-overlapping keys).
        for i in range(n_procs):
            assert f"key{i}: value-key{i}" in body, f"key{i} missing — lost update"

    def test_context_snapshot_remains_parseable_no_partial_writes(self, tmp_path):
        # Documented semantics: latest-wins for context/latest.md. Under
        # concurrency, exactly one process's state must end up as the final
        # latest.md content — never a torn mix of two writes' bytes.
        sk = tmp_path / ".skilllayer"
        n_procs = 5
        worker_path = tmp_path / "_save_context_worker.py"
        worker_path.write_text(_SAVE_CONTEXT_WORKER, encoding="utf-8")

        handles = [
            subprocess.Popen(
                [sys.executable, str(worker_path), str(sk), f"proc{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(n_procs)
        ]
        for p in handles:
            out, err = p.communicate(timeout=30)
            assert p.returncode == 0, err

        latest = sk / "context" / "latest.md"
        meta, body = parse_frontmatter(latest.read_text(encoding="utf-8"))
        assert meta.get("schema") is not None, "latest.md frontmatter is unparseable — torn write"
        assert "## State" in body and "## Open Questions" in body

        # Latest-wins: content must be exactly one process's full state string,
        # never a fragment mixing two.
        matching = [i for i in range(n_procs) if f"state from proc{i}" in body]
        assert len(matching) == 1, f"expected exactly one winning writer, found {matching}"

        # History must hold the other n_procs-1 rotated snapshots, all parseable.
        history_dir = sk / "context" / "history"
        history_files = list(history_dir.glob("*.md"))
        assert len(history_files) == n_procs - 1
        for hf in history_files:
            h_meta, h_body = parse_frontmatter(hf.read_text(encoding="utf-8"))
            assert h_meta.get("schema") is not None, f"{hf.name} is unparseable — torn write"


# ---------------------------------------------------------------------------
# 6. Lock timeout
# ---------------------------------------------------------------------------

class TestLockTimeout:
    def test_timeout_returns_clear_error_no_partial_state(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        worker_path = tmp_path / "_hold_lock_worker.py"
        worker_path.write_text(_HOLD_LOCK_WORKER, encoding="utf-8")

        holder = subprocess.Popen(
            [sys.executable, str(worker_path), str(sk), "2.0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            # Wait for the holder to actually acquire before racing it.
            line = holder.stdout.readline()
            assert line.strip() == "ACQUIRED"

            before_files = sorted(p.name for p in sk.iterdir())

            with pytest.raises(MemoryLockTimeoutError):
                with memory_lock(sk, timeout=0.3):
                    pytest.fail("should never acquire while holder is active")

            after_files = sorted(p.name for p in sk.iterdir())
            assert before_files == after_files, "timeout path left behind partial state"
        finally:
            holder.communicate(timeout=10)

    def test_timeout_error_message_is_clear_and_not_stale_lock_framing(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        worker_path = tmp_path / "_hold_lock_worker.py"
        worker_path.write_text(_HOLD_LOCK_WORKER, encoding="utf-8")

        holder = subprocess.Popen(
            [sys.executable, str(worker_path), str(sk), "1.5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "ACQUIRED"
            with pytest.raises(MemoryLockTimeoutError) as exc_info:
                with memory_lock(sk, timeout=0.2):
                    pass
            msg = str(exc_info.value)
            assert "0.2" in msg
            assert "stale" not in msg.lower() or "not a stale lock" in msg.lower()
            assert exc_info.value.skilllayer_dir == sk
        finally:
            holder.communicate(timeout=10)

    def test_lock_available_immediately_after_holder_exits_no_stale_lock(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        worker_path = tmp_path / "_hold_lock_worker.py"
        worker_path.write_text(_HOLD_LOCK_WORKER, encoding="utf-8")

        holder = subprocess.Popen(
            [sys.executable, str(worker_path), str(sk), "0.3"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert holder.stdout.readline().strip() == "ACQUIRED"
        holder.communicate(timeout=10)  # wait for natural release
        assert holder.returncode == 0

        # Immediately acquirable — no leftover stale-lock condition.
        t0 = time.monotonic()
        with memory_lock(sk, timeout=5):
            pass
        assert time.monotonic() - t0 < 1.0

    def test_reentrant_call_does_not_deadlock_or_timeout(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        with memory_lock(sk, timeout=1):
            with memory_lock(sk, timeout=1):
                with memory_lock(sk, timeout=1):
                    pass  # must not deadlock against itself


# ---------------------------------------------------------------------------
# 6b. Same-process thread safety on the no-fcntl (e.g. Windows) fallback path
#
# Post-review regression: memory_lock()'s fallback previously used ONLY
# threading.local() depth bookkeeping when fcntl is unavailable. Each thread
# has its own independent depth counter, so two threads both saw depth==0 and
# both proceeded concurrently — no actual mutual exclusion. Fixed with a
# shared process-local threading.RLock (keyed by resolved skilllayer_dir),
# acquired before the (POSIX-only) flock and released after it.
# ---------------------------------------------------------------------------

class TestSameProcessThreadSafetyOnFallbackPath:
    def test_two_threads_no_lost_updates_when_fcntl_unavailable(self, tmp_path, monkeypatch):
        import threading
        import src.skilllayer.memory.skilllayer_memory as mem

        monkeypatch.setattr(mem, "fcntl", None)  # simulate the Windows fallback path
        sk = tmp_path / ".skilllayer"

        n_per_thread = 20
        barrier = threading.Barrier(2)

        def worker(label):
            barrier.wait()  # maximize actual overlap between the two threads
            for i in range(n_per_thread):
                r = mem.add_todo(sk, f"{label}-{i}")
                assert r["error_code"] is None, r

        t1 = threading.Thread(target=worker, args=("threadA",))
        t2 = threading.Thread(target=worker, args=("threadB",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive()

        data = json.loads((sk / "todos.json").read_text())
        todos = data["todos"]
        assert len(todos) == 2 * n_per_thread, (
            f"lost update on the fallback path: expected {2 * n_per_thread}, got {len(todos)}"
        )
        ids = [t["id"] for t in todos]
        assert len(ids) == len(set(ids)), f"duplicate ids on the fallback path: {ids}"

    def test_reentrancy_still_works_when_fcntl_unavailable(self, tmp_path, monkeypatch):
        import src.skilllayer.memory.skilllayer_memory as mem

        monkeypatch.setattr(mem, "fcntl", None)
        sk = tmp_path / ".skilllayer"
        with mem.memory_lock(sk, timeout=1):
            with mem.memory_lock(sk, timeout=1):
                pass  # must not deadlock against itself on the fallback path either

    def test_process_lock_is_keyed_per_directory(self, tmp_path):
        # Two different .skilllayer stores must not contend with each other.
        sk_a = tmp_path / "repo_a" / ".skilllayer"
        sk_b = tmp_path / "repo_b" / ".skilllayer"
        from src.skilllayer.memory.skilllayer_memory import _process_lock_for
        assert _process_lock_for(sk_a) is not _process_lock_for(sk_b)
        assert _process_lock_for(sk_a) is _process_lock_for(sk_a)


# ---------------------------------------------------------------------------
# 7. Corrupt JSON/state
# ---------------------------------------------------------------------------

class TestCorruptStateHandling:
    def test_corrupt_state_json_not_silently_reset(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        (sk / ".state.json").write_text("{ not valid json")
        with pytest.raises(MemoryStateCorruptError):
            claim_next_id(sk, "next_decision_id")

    def test_corrupt_state_json_original_bytes_preserved(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        corrupt_bytes = "{ not valid json, definitely broken !!!"
        (sk / ".state.json").write_text(corrupt_bytes)
        with pytest.raises(MemoryStateCorruptError):
            claim_next_id(sk, "next_decision_id")
        assert (sk / ".state.json").read_text() == corrupt_bytes

    def test_track_decision_reports_clear_failure_not_silent_reset(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        (sk / ".state.json").write_text("not json")
        result = track_decision(sk, "Title", "ctx", "dec", "reason", "conseq")
        assert result["error_code"] == "memory_state_corrupt"
        assert result["decision_id"] is None
        assert not (sk / "decisions").exists() or not list((sk / "decisions").glob("*.md"))

    def test_add_todo_reports_clear_failure_not_silent_reset(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        (sk / ".state.json").write_text("not json")
        result = add_todo(sk, "a todo")
        assert result["error_code"] == "memory_state_corrupt"
        assert result["todo_id"] is None
        assert not (sk / "todos.json").exists()

    def test_never_mints_duplicate_id_after_corruption_and_repair(self, tmp_path):
        # The actual danger the old silent-reset behavior created: claim two
        # real ids, corrupt the counter, confirm the corrupt state blocks
        # further claims rather than restarting at 1 and colliding.
        sk = tmp_path / ".skilllayer"
        sk.mkdir(parents=True)
        id1 = claim_next_id(sk, "next_decision_id")
        id2 = claim_next_id(sk, "next_decision_id")
        assert id1 == "0001" and id2 == "0002"

        (sk / ".state.json").write_text("corrupted!!!")
        with pytest.raises(MemoryStateCorruptError):
            claim_next_id(sk, "next_decision_id")
        # Confirm it did NOT silently reset to 1 (which would collide with id1).
        assert (sk / ".state.json").read_text() == "corrupted!!!"

    def test_corrupt_todos_json_still_preserved_pre_existing_fix(self, tmp_path):
        # Regression guard for the sibling fix from the prior session:
        # todos.json corruption must behave identically to .state.json
        # corruption — preserved, not reset.
        sk = tmp_path / ".skilllayer"
        add_todo(sk, "real todo")
        (sk / "todos.json").write_text("{corrupt")
        from src.skilllayer.memory.skilllayer_memory import _read_todos
        with pytest.raises(TodosStoreCorruptError):
            _read_todos(sk)
        assert (sk / "todos.json").read_text() == "{corrupt"


# ---------------------------------------------------------------------------
# 8. Existing memory workflow compatibility (also run as a dedicated pass in
# the full validation — this class re-confirms it from within this file too)
# ---------------------------------------------------------------------------

class TestExistingCompatibility:
    def test_full_realistic_session_end_to_end(self, tmp_path):
        sk = tmp_path / ".skilllayer"
        save_context_snapshot(sk, "working on X", ["done yet?"])
        d1 = track_decision(sk, "Use Y", "c", "d", "r", "q")
        track_decision(sk, "Use Z", "c", "d", "r", "q", supersedes=d1["decision_id"])
        remember_preferences(sk, {"testing": {"runner": "pytest"}})
        t1 = add_todo(sk, "todo one")
        add_todo(sk, "todo two")
        mark_todo_done(sk, t1["todo_id"])
        write_index(sk)

        from skilllayer.memory.skilllayer_memory import validate_memory_store
        report = validate_memory_store(sk)
        assert report["summary"]["broken_count"] == 0
        assert report["summary"]["warnings_count"] == 0
        assert report["success"] is True
        assert report["status"] == "healthy"


# ---------------------------------------------------------------------------
# Post-review: MEMORY_LOCK_CROSS_PROCESS_SUPPORTED surfaced via doctor
# ---------------------------------------------------------------------------

class TestDoctorSurfacesLockSupport:
    def test_doctor_reports_cross_process_lock_support(self):
        from src.skilllayer.mcp_server import skilllayer_doctor
        from src.skilllayer.memory.skilllayer_memory import MEMORY_LOCK_CROSS_PROCESS_SUPPORTED

        result = skilllayer_doctor()
        check = result["optional_checks"]["memory_lock_cross_process_supported"]
        assert check["value"] == MEMORY_LOCK_CROSS_PROCESS_SUPPORTED
        assert check["required"] is False
        if MEMORY_LOCK_CROSS_PROCESS_SUPPORTED:
            assert check["ok"] is True
            assert check["warning"] is None
        else:
            assert check["ok"] is False
            assert "not" in check["warning"].lower() or "no fcntl" in check["warning"].lower()

    def test_doctor_lock_check_never_blocks_required_success(self):
        # Optional check — must never make doctor report overall failure.
        from src.skilllayer.mcp_server import skilllayer_doctor
        result = skilllayer_doctor()
        assert "memory_lock_cross_process_supported" not in result["required_checks"]


# ---------------------------------------------------------------------------
# Post-review: known gap, deliberately NOT auto-fixed (no .gitignore editing
# was added, per instruction). This test documents current behavior so any
# future change to it is a deliberate, visible decision, not silent drift.
# ---------------------------------------------------------------------------

class TestLockFileGitignoreCharacterization:
    def test_memory_lock_creates_no_gitignore_entry(self, tmp_path):
        # Characterization test for a known, disclosed, deliberately
        # unaddressed gap: memory_lock() must not touch .gitignore at all
        # (no auto-editing was added). If a future change adds a
        # gitignore-ensuring call for MEMORY_LOCK_FILENAME, this test should
        # be updated deliberately, not silently pass either way.
        from src.skilllayer.memory.skilllayer_memory import memory_lock
        sk = tmp_path / ".skilllayer"
        with memory_lock(sk):
            pass
        gitignore = tmp_path / ".gitignore"
        assert not gitignore.exists(), "memory_lock() must not create/modify .gitignore"

    def test_memory_lock_file_not_covered_by_repo_gitignore_patterns(self):
        # Documents the actual gap found during post-review: this repo's own
        # .gitignore has specific entries for .state.json and the baseline/
        # snapshot caches, but none for .memory.lock.
        gitignore_text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
        assert ".skilllayer/.state.json" in gitignore_text  # sibling file IS covered
        assert ".memory.lock" not in gitignore_text  # confirmed gap, not silently fixed here
