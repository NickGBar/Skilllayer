"""The scripted demo is the product's shop window; it must keep telling the story it claims to."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _skilllayer_command(tmp_path: Path) -> Path:
    wrapper = tmp_path / "skilllayer"
    wrapper.write_text(f'#!/bin/sh\nPYTHONPATH="{ROOT / "src"}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" -m skilllayer "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def test_the_demo_script_shows_a_block_a_recovery_a_warning_and_a_policy_violation(tmp_path):
    done = subprocess.run(
        ["bash", str(ROOT / "examples/verify-demo/run_demo.sh")],
        env={**os.environ, "SKILLLAYER_BIN": str(_skilllayer_command(tmp_path))},
        capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    # Act 1: a false "done" is stopped, with the observed assertion, and the agent recovers.
    assert "blocked: the agent is sent back to work" in out and "assert 5.0 == 0.0" in out
    assert "what you see: skilllayer sent the agent back to work (attempt 1 of 2): tests failing" in out
    # Act 2: tests weakened to pass are allowed but surfaced to the user.
    assert "test files were changed this turn (tests/test_cart.py)" in out
    # Act 3: a protected path is enforced even though the tests pass.
    assert "Protected path modified: migrations/001_init.sql" in out
    assert "blocked: 2" in out and "recovered_after_block: 2" in out


def test_the_demo_template_is_a_valid_repository_policy(tmp_path):
    from skilllayer.policy import evaluate_policy_text

    text = (ROOT / "examples/verify-demo/template/.skilllayer-policy.yml").read_text()
    assert evaluate_policy_text(text, policy_path=".skilllayer-policy.yml")["status"] == "POLICY_VALID"
