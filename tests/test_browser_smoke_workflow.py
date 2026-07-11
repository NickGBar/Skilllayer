"""Focused tests for BrowserSmoke truthfulness (Runtime Safety Closure Stage 1).

Covers: no false-green when a real browser backend is unavailable, no
placeholder screenshots, no hidden artifact writes, and that a real browser
failure is never silently downgraded into the static fallback's "unsupported"
framing. Some tests exercise the REAL Playwright/Chromium backend (installed
in this environment) rather than mocking it, since that is the only way to
prove a real JS console error is genuinely captured, not merely absent by
construction.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from skilllayer.tools.browser import run_browser_smoke


def _playwright_runtime_probe() -> tuple[bool, str]:
    """Distinguish an importable SDK from a launchable browser runtime."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        return False, f"Playwright Chromium cannot start in this environment: {exc}"
    return True, ""


_PLAYWRIGHT_RUNTIME_AVAILABLE, _PLAYWRIGHT_SKIP_REASON = _playwright_runtime_probe()
requires_playwright_runtime = pytest.mark.skipif(
    not _PLAYWRIGHT_RUNTIME_AVAILABLE,
    reason=_PLAYWRIGHT_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Backend unavailable -> honest "unsupported", never success
# ---------------------------------------------------------------------------

def test_playwright_unavailable_returns_unsupported_never_success(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text("<html><body id='app'>ok</body></html>", encoding="utf-8")
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=None):
        result = run_browser_smoke(f"file://{page}", ["#app"], output_dir=tmp_path / "out")
    assert result["success"] is False
    assert result["status"] == "unsupported"
    assert result["error_code"] == "browser_backend_unavailable"
    assert result["browser_executed"] is False
    assert result["backend_used"] in ("static_http", "none")


def test_static_selector_match_does_not_become_browser_success(tmp_path: Path) -> None:
    # The selector genuinely exists in the raw HTML (a real, if limited,
    # static signal) but that must never be reported as a completed browser
    # smoke success.
    page = tmp_path / "index.html"
    page.write_text("<html><body id='app'>ok</body></html>", encoding="utf-8")
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=None):
        result = run_browser_smoke(f"file://{page}", ["#app"], output_dir=tmp_path / "out")
    assert result["missing_elements"] == []
    assert result["elements_verified"] == 1
    assert result["success"] is False
    assert result["status"] == "unsupported"
    assert result["static_inspection_performed"] is True
    assert result["console_errors"] is None  # never claimed as verified


def test_no_placeholder_screenshot_when_backend_unavailable(tmp_path: Path) -> None:
    page = tmp_path / "index.html"
    page.write_text("<html><body>ok</body></html>", encoding="utf-8")
    output_dir = tmp_path / "out"
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=None):
        result = run_browser_smoke(
            f"file://{page}", ["body"], output_dir=output_dir, write_artifacts=True,
        )
    assert result["screenshot_path"] is None
    pngs = list(output_dir.glob("*.png")) if output_dir.exists() else []
    assert pngs == [], f"a placeholder screenshot was written: {pngs}"


def test_mocked_real_browser_success_passes_through_unchanged(tmp_path: Path) -> None:
    fake_success = {
        "success": True,
        "status": "healthy",
        "error_code": None,
        "workflow": "BrowserSmokeWorkflow",
        "url": "http://example.test",
        "browser_executed": True,
        "static_inspection_performed": False,
        "console_errors": 0,
        "network_errors": 0,
        "elements_verified": 1,
        "elements_expected": 1,
        "missing_elements": [],
        "screenshot_path": None,
        "browser_backend": "playwright",
        "backend_used": "playwright",
    }
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=fake_success):
        result = run_browser_smoke("http://example.test", ["body"], output_dir=tmp_path / "out")
    assert result is fake_success
    assert result["success"] is True


def test_mocked_browser_execution_failure_remains_structured_error(tmp_path: Path) -> None:
    failed = {
        "success": False,
        "status": "error",
        "error_code": "browser_execution_failed",
        "browser_executed": False,
        "static_inspection_performed": False,
        "screenshot_path": None,
        "written_paths": [],
        "browser_backend": "playwright",
        "backend_used": "playwright",
    }
    with patch("skilllayer.tools.browser.run_playwright_smoke_if_available", return_value=failed):
        result = run_browser_smoke("http://example.test", ["body"], output_dir=tmp_path / "out")
    assert result is failed
    assert result["success"] is False
    assert result["status"] == "error"
    assert result["screenshot_path"] is None


# ---------------------------------------------------------------------------
# Real Playwright/Chromium backend (installed in this environment)
# ---------------------------------------------------------------------------

@requires_playwright_runtime
def test_real_browser_javascript_crash_is_captured_not_silently_clean(tmp_path: Path) -> None:
    page = tmp_path / "crash.html"
    page.write_text(
        "<html><body><script>throw new Error('boom');</script></body></html>",
        encoding="utf-8",
    )
    result = run_browser_smoke(f"file://{page}", ["body"], output_dir=tmp_path / "out")
    assert result["browser_backend"] == "playwright"
    assert result["browser_executed"] is True
    assert result["console_errors"] >= 1
    assert result["success"] is False


@requires_playwright_runtime
def test_real_browser_clean_page_succeeds_with_no_artifacts_by_default(tmp_path: Path) -> None:
    page = tmp_path / "clean.html"
    page.write_text("<html><body id='app'>hello</body></html>", encoding="utf-8")
    output_dir = tmp_path / "out"
    result = run_browser_smoke(f"file://{page}", ["#app"], output_dir=output_dir)
    assert result["browser_backend"] == "playwright"
    assert result["browser_executed"] is True
    assert result["success"] is True
    assert result["status"] == "healthy"
    assert result["written_paths"] == []
    assert result["screenshot_path"] is None
    assert not output_dir.exists()


@requires_playwright_runtime
def test_real_browser_execution_failure_is_structured_not_static_fallback(tmp_path: Path) -> None:
    # A closed local port fails navigation fast (connection refused) —
    # a genuine mid-execution Playwright failure, not "backend not
    # installed". Must never be silently downgraded to status="unsupported".
    result = run_browser_smoke("http://127.0.0.1:1/", ["body"], output_dir=tmp_path / "out")
    assert result["success"] is False
    assert result["status"] == "error"
    assert result["error_code"] == "browser_execution_failed"
    assert result["browser_backend"] == "playwright"
    assert result["browser_executed"] is False
    assert result["console_errors"] is None
