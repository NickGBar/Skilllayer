from __future__ import annotations

import html.parser
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ERROR_BROWSER_BACKEND_UNAVAILABLE = "browser_backend_unavailable"
ERROR_BROWSER_EXECUTION_FAILED = "browser_execution_failed"

_STATIC_FALLBACK_LIMITATIONS = [
    "No real browser executed; JavaScript on the page was never run.",
    "Console errors could not be checked — that requires a real browser.",
    "Rendering/layout was not verified — only raw HTML/HTTP text was inspected.",
    "No screenshot was captured.",
]


@dataclass
class BrowserSmokeSession:
    """Static HTML/HTTP fallback used only when no real browser backend is
    available. Never represents itself as a completed browser smoke test —
    see run_static_fallback()."""

    output_dir: Path = Path("runs/browser_smoke")
    write_artifacts: bool = False
    wait_seconds: float = 1.0
    trace: list[dict[str, Any]] = field(default_factory=list)
    html: str = ""
    url: str = ""
    network_errors: list[str] = field(default_factory=list)

    def browser_open_page(self, url: str) -> dict[str, Any]:
        self.url = url
        try:
            if url.startswith("file://"):
                path = Path(url[7:])
                self.html = path.read_text(encoding="utf-8", errors="ignore")
            elif url.startswith(("http://", "https://")):
                with urllib.request.urlopen(url, timeout=8) as response:
                    self.html = response.read().decode("utf-8", errors="ignore")
            else:
                path = Path(url)
                self.html = path.read_text(encoding="utf-8", errors="ignore")
            result = {"tool": "browser_open_page", "url": url, "success": True, "chars": len(self.html)}
        except Exception as exc:
            self.network_errors.append(str(exc))
            result = {"tool": "browser_open_page", "url": url, "success": False, "error": str(exc)}
        self.trace.append(result)
        return result

    def browser_element_exists(self, selector: str) -> dict[str, Any]:
        exists = selector_exists(self.html, selector)
        result = {"tool": "browser_element_exists", "selector": selector, "success": exists}
        self.trace.append(result)
        return result

    def generate_unsupported_report(self, selectors: list[str]) -> dict[str, Any]:
        """Honest, explicitly non-browser result. success is always False here:
        static HTML/HTTP inspection is a limited advisory, never a completed
        browser smoke test — see the Stage 1 runtime-safety requirements."""
        verified = [item for item in self.trace if item.get("tool") == "browser_element_exists" and item.get("success")]
        missing = [item.get("selector") for item in self.trace if item.get("tool") == "browser_element_exists" and not item.get("success")]
        static_inspection_performed = bool(self.html) or bool(self.network_errors)
        report: dict[str, Any] = {
            "success": False,
            "status": "unsupported",
            "error_code": ERROR_BROWSER_BACKEND_UNAVAILABLE,
            "error": (
                "No real browser backend is available (Playwright is not installed). "
                "Install with `pip install playwright` and `playwright install chromium` "
                "to enable real browser smoke checks."
            ),
            "workflow": "BrowserSmokeWorkflow",
            "url": self.url,
            "browser_executed": False,
            "static_inspection_performed": static_inspection_performed,
            "limitations": list(_STATIC_FALLBACK_LIMITATIONS),
            "browser_backend": "static_http" if static_inspection_performed else "none",
            "backend_used": "static_http" if static_inspection_performed else "none",
            # Console errors have no real signal without a real browser — never
            # reported as a verified 0. network_errors reflects only the raw
            # HTTP/file fetch, a genuine (if limited) signal, so it stays a
            # real count when the fetch was attempted.
            "console_errors": None,
            "network_errors": len(self.network_errors) if static_inspection_performed else None,
            "elements_verified": len(verified),
            "elements_expected": len(selectors),
            "missing_elements": missing,
            "screenshot_path": None,
            "trace": list(self.trace),
            "artifact_writes_enabled": self.write_artifacts,
            "written_paths": [],
        }
        if self.write_artifacts:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            report_path = self.output_dir / f"browser_smoke_report_{time.strftime('%Y%m%d_%H%M%S')}.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            report["report_path"] = str(report_path)
            report["written_paths"].append(str(report_path))
            self.trace.append({"tool": "generate_smoke_report", "success": True, "report_path": str(report_path)})
        else:
            report["report_path"] = None
        return report


def run_browser_smoke(
    url: str,
    selectors: list[str],
    output_dir: str | Path = "runs/browser_smoke",
    wait_seconds: float = 1.0,
    *,
    write_artifacts: bool = False,
) -> dict[str, Any]:
    playwright_result = run_playwright_smoke_if_available(
        url,
        selectors,
        Path(output_dir),
        wait_seconds,
        write_artifacts=write_artifacts,
    )
    if playwright_result is not None:
        # Covers both a real completed browser run AND a real browser backend
        # that was attempted but failed mid-execution — both are structured,
        # honest results, never silently downgraded to the static fallback.
        return playwright_result

    # Playwright is not installed at all. Optionally still perform a clearly
    # non-browser, limited static HTML/HTTP inspection, but never represent
    # it as a completed browser smoke test.
    session = BrowserSmokeSession(
        output_dir=Path(output_dir),
        wait_seconds=wait_seconds,
        write_artifacts=write_artifacts,
    )
    session.browser_open_page(url)
    for selector in selectors:
        session.browser_element_exists(selector)
    return session.generate_unsupported_report(selectors)


def run_playwright_smoke_if_available(
    url: str,
    selectors: list[str],
    output_dir: Path,
    wait_seconds: float,
    *,
    write_artifacts: bool = False,
) -> dict[str, Any] | None:
    """Returns None only when Playwright itself is not installed (the caller
    then does the honest static fallback). Any failure AFTER Playwright is
    available (browser launch, navigation, etc.) returns a structured error
    dict here — it must never be silently swallowed into the static
    fallback, which would misrepresent a real browser failure as merely
    "backend not installed"."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    if write_artifacts:
        output_dir.mkdir(parents=True, exist_ok=True)
    trace: list[dict[str, Any]] = []
    console_errors: list[str] = []
    network_errors: list[str] = []
    screenshot_path = output_dir / f"browser_smoke_{time.strftime('%Y%m%d_%H%M%S')}.png"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            # An uncaught JS exception (a real "JavaScript crash") never
            # reaches page.on("console", ...) — only explicit console.error()
            # calls do. Without this, a page that throws uncaught would
            # report 0 console errors, exactly the false-green this closes.
            page.on("pageerror", lambda exc: console_errors.append(str(exc)))
            page.on("requestfailed", lambda request: network_errors.append(request.url))
            page.goto(url, wait_until="load", timeout=10000)
            page.wait_for_timeout(int(max(wait_seconds, 0) * 1000))
            trace.append({"tool": "browser_open_page", "url": url, "success": True})
            trace.append({"tool": "browser_wait_ready", "success": True, "wait_seconds": wait_seconds})
            trace.append({"tool": "browser_check_console_errors", "success": not console_errors, "console_errors": len(console_errors)})
            trace.append({"tool": "browser_check_network_errors", "success": not network_errors, "network_errors": len(network_errors)})
            missing: list[str] = []
            for selector in selectors:
                exists = page.locator(selector).count() > 0
                trace.append({"tool": "browser_element_exists", "selector": selector, "success": exists})
                if not exists:
                    missing.append(selector)
            if write_artifacts:
                page.screenshot(path=str(screenshot_path), full_page=True)
                trace.append({"tool": "browser_screenshot", "success": True, "screenshot_path": str(screenshot_path)})
            else:
                trace.append({
                    "tool": "browser_screenshot",
                    "success": True,
                    "screenshot_path": None,
                    "skipped": True,
                    "reason": "artifact_writes_not_enabled",
                })
            browser.close()
        success = not console_errors and not network_errors and not missing
        report = {
            "success": success,
            "status": "healthy" if success else "findings",
            "error_code": None,
            "error": None,
            "workflow": "BrowserSmokeWorkflow",
            "url": url,
            "browser_executed": True,
            "static_inspection_performed": False,
            "limitations": [],
            "console_errors": len(console_errors),
            "network_errors": len(network_errors),
            "elements_verified": len(selectors) - len(missing),
            "elements_expected": len(selectors),
            "missing_elements": missing,
            "screenshot_path": str(screenshot_path) if write_artifacts else None,
            "browser_backend": "playwright",
            "backend_used": "playwright",
            "trace": trace,
            "artifact_writes_enabled": write_artifacts,
            "written_paths": [str(screenshot_path)] if write_artifacts else [],
        }
        if write_artifacts:
            report_path = output_dir / f"browser_smoke_report_{time.strftime('%Y%m%d_%H%M%S')}.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            report["report_path"] = str(report_path)
            report["written_paths"].append(str(report_path))
        else:
            report["report_path"] = None
        return report
    except Exception as exc:
        return {
            "success": False,
            "status": "error",
            "error_code": ERROR_BROWSER_EXECUTION_FAILED,
            "error": f"Playwright browser execution failed before completion: {exc}",
            "workflow": "BrowserSmokeWorkflow",
            "url": url,
            "browser_executed": False,
            "static_inspection_performed": False,
            "limitations": ["A real browser was launched but execution failed before the smoke check completed."],
            "console_errors": None,
            "network_errors": None,
            "elements_verified": 0,
            "elements_expected": len(selectors),
            "missing_elements": list(selectors),
            "screenshot_path": None,
            "browser_backend": "playwright",
            "backend_used": "playwright",
            "trace": trace,
            "artifact_writes_enabled": write_artifacts,
            "written_paths": [],
            "report_path": None,
        }


def selector_exists(html: str, selector: str) -> bool:
    selector = selector.strip()
    if not selector or not html:
        return False
    index = ElementIndex()
    index.feed(html)
    if selector.startswith("#"):
        return selector[1:] in index.ids
    if selector.startswith("."):
        return selector[1:] in index.classes
    attr_match = re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9_-]*)?\[([A-Za-z_:][-A-Za-z0-9_:.]*)(?:=['\"]?([^'\"]+)['\"]?)?\]", selector)
    if attr_match:
        attr, value = attr_match.groups()
        if value is None:
            return attr in index.attrs
        return value in index.attrs.get(attr, set())
    tag = selector.lower()
    return tag in index.tags


class ElementIndex(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.classes: set[str] = set()
        self.tags: set[str] = set()
        self.attrs: dict[str, set[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag.lower())
        for key, value in attrs:
            self.attrs.setdefault(key, set()).add(value or "")
            if key == "id" and value:
                self.ids.add(value)
            if key == "class" and value:
                self.classes.update(value.split())
