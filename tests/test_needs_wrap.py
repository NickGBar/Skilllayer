"""Tests for the NeedsWrap check (compute_needs_wrap + INDEX/rehydrate wiring).

NeedsWrap is automatic logic inside regenerate_index() — not a standalone
workflow. It detects real file changes since the last saved context snapshot by
reading two files that already exist (context/latest.md and
file_watch_snapshot.json) and NEVER triggers a new file scan.

Every one of the five states is tested independently:
    unknown, never_saved, stale_watch, needs_wrap, clean
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.skilllayer.memory.skilllayer_memory import (
    NEEDS_WRAP_SAMPLE_CAP,
    _parse_iso,
    compute_needs_wrap,
    regenerate_index,
    rehydrate_context,
    save_context_snapshot,
)

# A far-future / far-past instant makes "after/before the save (which is now)"
# unambiguous without depending on wall-clock timing.
FUTURE = "2100-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"


def _sk(tmp_path: Path) -> Path:
    d = tmp_path / ".skilllayer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_watch(sk: Path, recorded_at: str, files: dict) -> None:
    (sk / "file_watch_snapshot.json").write_text(
        json.dumps({"schema": 1, "recorded_at": recorded_at, "scope": ".", "files": files}),
        encoding="utf-8",
    )


def _file(mtime: str, size: int = 10) -> dict:
    return {"hash": "sha256:deadbeef", "size": size, "mtime": mtime}


# ---------------------------------------------------------------------------
# The five states, each independently
# ---------------------------------------------------------------------------

class TestFiveStates:
    def test_unknown_when_no_watch_snapshot(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        # No file_watch_snapshot.json exists -> cannot tell, must NOT say clean.
        result = compute_needs_wrap(sk)
        assert result["status"] == "unknown"
        assert result["changed_count"] == 0

    def test_never_saved_when_watch_but_no_context(self, tmp_path):
        sk = _sk(tmp_path)
        _write_watch(sk, recorded_at=FUTURE, files={"a.py": _file(PAST)})
        result = compute_needs_wrap(sk)
        assert result["status"] == "never_saved"
        assert result["changed_count"] == 1

    def test_stale_watch_when_watch_predates_save(self, tmp_path):
        sk = _sk(tmp_path)
        # Watch recorded in the past, THEN a snapshot saved now.
        _write_watch(sk, recorded_at=PAST, files={"a.py": _file(PAST)})
        save_context_snapshot(sk, state="s", open_questions=[])
        result = compute_needs_wrap(sk)
        assert result["status"] == "stale_watch"

    def test_needs_wrap_when_file_modified_after_save(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])  # created = now
        _write_watch(
            sk, recorded_at=FUTURE,
            files={"work.py": _file(FUTURE), "old.py": _file(PAST)},
        )
        result = compute_needs_wrap(sk)
        assert result["status"] == "needs_wrap"
        assert result["changed_count"] == 1  # only work.py is newer than the save
        assert result["changed_sample"] == ["work.py"]

    def test_clean_when_nothing_changed_since_save(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        _write_watch(sk, recorded_at=FUTURE, files={"old.py": _file(PAST)})
        result = compute_needs_wrap(sk)
        assert result["status"] == "clean"
        assert result["changed_count"] == 0


# ---------------------------------------------------------------------------
# Tolerant datetime parsing across the two real formats
# ---------------------------------------------------------------------------

class TestTolerantParsing:
    def test_parses_trailing_z(self):
        dt = _parse_iso("2026-07-07T14:22:05Z")
        assert dt is not None and dt.tzinfo is not None
        assert dt == datetime(2026, 7, 7, 14, 22, 5, tzinfo=timezone.utc)

    def test_parses_offset_with_microseconds(self):
        dt = _parse_iso("2026-07-07T14:22:05.123456+00:00")
        assert dt is not None and dt.tzinfo is not None

    def test_z_and_offset_are_comparable(self):
        # The whole point: latest.md uses 'Z', the snapshot uses '+00:00'.
        z = _parse_iso("2026-07-07T14:22:05Z")
        off = _parse_iso("2026-07-07T14:22:06.000000+00:00")
        assert off > z

    def test_bad_values_return_none(self):
        for bad in (None, "", "not-a-date", 12345, "2026-13-99"):
            assert _parse_iso(bad) is None

    def test_cross_format_comparison_drives_needs_wrap(self, tmp_path):
        # latest.md 'created' is written by save_context_snapshot in Z-format;
        # the snapshot mtime here is +00:00 format. NeedsWrap must still compare
        # them correctly and flag the newer file.
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        created = _read_created(sk)
        assert created.endswith("Z")  # confirm the Z-format side is real
        _write_watch(sk, recorded_at=FUTURE, files={"new.py": _file(FUTURE)})
        assert compute_needs_wrap(sk)["status"] == "needs_wrap"


def _read_created(sk: Path) -> str:
    import re
    text = (sk / "context" / "latest.md").read_text(encoding="utf-8")
    return re.search(r"^created:\s*(\S+)", text, re.MULTILINE).group(1)


# ---------------------------------------------------------------------------
# INDEX.md rendering
# ---------------------------------------------------------------------------

class TestIndexRendering:
    def test_needs_wrap_heading_and_body(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        _write_watch(sk, recorded_at=FUTURE, files={"work.py": _file(FUTURE)})
        idx = regenerate_index(sk)
        assert "## Unsaved Work · NeedsWrap — 1 files changed since last snapshot" in idx
        assert "last snapshot:" in idx and "last file-watch:" in idx
        assert "recent: work.py" in idx
        assert "→ Save a context snapshot to consolidate this work." in idx

    def test_clean_variant(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        _write_watch(sk, recorded_at=FUTURE, files={"old.py": _file(PAST)})
        idx = regenerate_index(sk)
        assert "## Unsaved Work · NeedsWrap — clean" in idx

    def test_unknown_variant(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        idx = regenerate_index(sk)
        assert "## Unsaved Work · NeedsWrap — unknown" in idx
        assert "Run WatchFileChanges" in idx

    def test_sample_capped_and_count_full(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        files = {f"f{i}.py": _file(FUTURE) for i in range(7)}
        _write_watch(sk, recorded_at=FUTURE, files=files)
        result = compute_needs_wrap(sk)
        assert result["changed_count"] == 7
        assert len(result["changed_sample"]) == NEEDS_WRAP_SAMPLE_CAP == 5

    def test_no_section_on_empty_store(self, tmp_path):
        sk = _sk(tmp_path)
        idx = regenerate_index(sk)  # nothing saved, no watch data
        assert "NeedsWrap" not in idx


# ---------------------------------------------------------------------------
# Live recompute in rehydrate differs from the stale baked-in INDEX.md
# ---------------------------------------------------------------------------

class TestLiveRecompute:
    def test_rehydrate_recomputes_live_not_stale_index(self, tmp_path):
        sk = _sk(tmp_path)
        # 1) watch present with OLD files, THEN save -> baked INDEX says clean.
        _write_watch(sk, recorded_at=FUTURE, files={"old.py": _file(PAST)})
        save_context_snapshot(sk, state="s", open_questions=[])  # regenerates INDEX
        assert "## Unsaved Work · NeedsWrap — clean" in (sk / "INDEX.md").read_text()

        # 2) a file is now modified after the save, recorded in a NEW watch,
        #    but INDEX.md is NOT regenerated -> it is stale.
        _write_watch(sk, recorded_at=FUTURE, files={"work.py": _file(FUTURE)})

        # 3) rehydrate must report the LIVE state, not the stale baked value.
        out = rehydrate_context(sk)
        assert out["needs_wrap"]["status"] == "needs_wrap"
        assert out["needs_wrap"]["changed_count"] == 1
        # Proof the baked INDEX is genuinely stale (still says clean):
        assert "## Unsaved Work · NeedsWrap — clean" in out["index"]

    def test_rehydrate_exposes_structured_fields(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        _write_watch(sk, recorded_at=FUTURE, files={"work.py": _file(FUTURE)})
        nw = rehydrate_context(sk)["needs_wrap"]
        assert set(nw) >= {"status", "changed_count", "last_snapshot", "last_file_watch"}
        assert nw["last_snapshot"] is not None
        assert nw["last_file_watch"] == FUTURE


# ---------------------------------------------------------------------------
# Zero new scans: the check only READS existing snapshot data
# ---------------------------------------------------------------------------

class TestNoNewScan:
    def test_watch_snapshot_is_never_rewritten(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        _write_watch(sk, recorded_at=FUTURE, files={"work.py": _file(FUTURE)})
        snap = sk / "file_watch_snapshot.json"
        before = snap.read_bytes()
        compute_needs_wrap(sk)
        regenerate_index(sk)
        rehydrate_context(sk)
        # A real scan would overwrite the snapshot with fresh hashes/recorded_at.
        assert snap.read_bytes() == before

    def test_no_watch_builder_imported_in_memory_module(self):
        # compute_needs_wrap lives in the memory module and must not pull in the
        # scanner. Guard against a future edit wiring in a real scan.
        import src.skilllayer.memory.skilllayer_memory as mem
        source = Path(mem.__file__).read_text(encoding="utf-8")
        assert "build_watch_file_changes_artifacts" not in source
        assert "rglob" not in source  # no filesystem walk in this module

    def test_no_snapshot_created_when_absent(self, tmp_path):
        sk = _sk(tmp_path)
        save_context_snapshot(sk, state="s", open_questions=[])
        compute_needs_wrap(sk)
        regenerate_index(sk)
        # The check must not fabricate a watch snapshot when none exists.
        assert not (sk / "file_watch_snapshot.json").exists()
