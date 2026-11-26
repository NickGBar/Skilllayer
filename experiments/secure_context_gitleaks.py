"""Synthetic-only Gitleaks stdin probe. Findings and raw input are never logged."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def scan(text: str, binary: str, *, timeout: float = 15.0) -> tuple[bool, bool]:
    """Return (scan_complete, findings_present); all failures are incomplete."""
    try:
        with tempfile.TemporaryDirectory(prefix="gitleaks-probe-") as directory:
            report = Path(directory, "report.json")
            # Do not pass shell credentials or inherited scanner configuration.
            env = {"PATH": os.defpath, "LANG": "C.UTF-8"}
            process = subprocess.run(
                [binary, "stdin", "--no-banner", "--ignore-gitleaks-allow", "--redact=100", "--report-format=json",
                 "--report-path", str(report)],
                input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=directory, env=env, timeout=timeout, check=False,
            )
            if process.returncode not in (0, 1) or not report.is_file():
                return False, False
            data = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                return False, False
            if (process.returncode == 0) != (len(data) == 0):
                return False, False
            return True, bool(data)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, False
