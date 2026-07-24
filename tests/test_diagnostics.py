from __future__ import annotations

import json
from pathlib import Path

from skilllayer.cli import main
from skilllayer.sanitization import sanitize


def test_sanitization_redacts_nested_synthetic_sensitive_values() -> None:
    payload = {
        "token": "sk-proj-secret-value",
        "nested": ["Bearer synthetic-secret", "postgres://alice:password@example.invalid/db"],
        "remote": "git@github.com:private/repo.git",
        "env": "API_KEY=synthetic-value",
        "memory": "do not include this private memory text",
    }
    result = sanitize(payload)
    encoded = json.dumps(result)
    for secret in ("sk-proj-secret-value", "synthetic-secret", "alice:password", "git@github.com", "synthetic-value"):
        assert secret not in encoded
    assert result["memory"] == payload["memory"]


def test_diagnostics_json_is_local_and_sanitized(tmp_path: Path, capsys) -> None:
    assert main(["diagnostics", "--repo", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["uploaded"] is False
    assert report["professional_skills"] == {
        "safe_code_change": True,
        "release_readiness": True,
        "resume_project_work": True,
        "verified_task_execution": True,
    }
    assert "Review this report before sharing" in report["privacy_warning"]


def test_diagnostics_output_is_atomic_and_requires_force(tmp_path: Path, capsys) -> None:
    destination = tmp_path / "diagnostics.json"
    assert main(["diagnostics", "--output", str(destination)]) == 0
    assert destination.exists()
    original = destination.read_text(encoding="utf-8")
    assert main(["diagnostics", "--output", str(destination)]) == 2
    assert destination.read_text(encoding="utf-8") == original
    assert main(["diagnostics", "--output", str(destination), "--force"]) == 0
    assert "uploaded" in destination.read_text(encoding="utf-8")
    assert "Diagnostics written locally" in capsys.readouterr().out
