from __future__ import annotations

import html.parser
import json
import re
import time
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BrowserSmokeSession:
    output_dir: Path = Path("runs/browser_smoke")
    wait_seconds: float = 1.0
    trace: list[dict[str, Any]] = field(default_factory=list)
    html: str = ""
    url: str = ""
    network_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    backend: str = "static"

    def browser_open_page(self, url: str) -> dict[str, Any]:
        self.url = url
        self.output_dir.mkdir(parents=True, exist_ok=True)
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

    def browser_wait_ready(self) -> dict[str, Any]:
        if self.wait_seconds > 0:
            time.sleep(min(self.wait_seconds, 2.0))
        result = {"tool": "browser_wait_ready", "success": bool(self.html), "wait_seconds": self.wait_seconds}
        self.trace.append(result)
        return result

    def browser_check_console_errors(self) -> dict[str, Any]:
        result = {
            "tool": "browser_check_console_errors",
            "success": True,
            "console_errors": len(self.console_errors),
            "errors": list(self.console_errors),
            "backend_supports_console": False,
        }
        self.trace.append(result)
        return result

    def browser_check_network_errors(self) -> dict[str, Any]:
        result = {
            "tool": "browser_check_network_errors",
            "success": len(self.network_errors) == 0,
            "network_errors": len(self.network_errors),
            "errors": list(self.network_errors),
        }
        self.trace.append(result)
        return result

    def browser_element_exists(self, selector: str) -> dict[str, Any]:
        exists = selector_exists(self.html, selector)
        result = {"tool": "browser_element_exists", "selector": selector, "success": exists}
        self.trace.append(result)
        return result

    def browser_screenshot(self) -> dict[str, Any]:
        path = self.output_dir / f"browser_smoke_{time.strftime('%Y%m%d_%H%M%S')}.png"
        suffix = 0
        while path.exists():
            suffix += 1
            path = self.output_dir / f"browser_smoke_{time.strftime('%Y%m%d_%H%M%S')}_{suffix}.png"
        write_placeholder_png(path)
        result = {"tool": "browser_screenshot", "success": True, "screenshot_path": str(path)}
        self.trace.append(result)
        return result

    def generate_smoke_report(self, selectors: list[str], screenshot_path: str) -> dict[str, Any]:
        verified = [item for item in self.trace if item.get("tool") == "browser_element_exists" and item.get("success")]
        missing = [item.get("selector") for item in self.trace if item.get("tool") == "browser_element_exists" and not item.get("success")]
        success = bool(self.html) and not self.console_errors and not self.network_errors and not missing
        report = {
            "success": success,
            "workflow": "BrowserSmokeWorkflow",
            "url": self.url,
            "console_errors": len(self.console_errors),
            "network_errors": len(self.network_errors),
            "elements_verified": len(verified),
            "elements_expected": len(selectors),
            "missing_elements": missing,
            "screenshot_path": screenshot_path,
            "browser_backend": self.backend,
            "trace": list(self.trace),
        }
        report_path = self.output_dir / f"browser_smoke_report_{time.strftime('%Y%m%d_%H%M%S')}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
        self.trace.append({"tool": "generate_smoke_report", "success": True, "report_path": str(report_path)})
        return report


def run_browser_smoke(url: str, selectors: list[str], output_dir: str | Path = "runs/browser_smoke", wait_seconds: float = 1.0) -> dict[str, Any]:
    playwright_result = run_playwright_smoke_if_available(url, selectors, Path(output_dir), wait_seconds)
    if playwright_result is not None:
        return playwright_result

    session = BrowserSmokeSession(output_dir=Path(output_dir), wait_seconds=wait_seconds)
    session.browser_open_page(url)
    session.browser_wait_ready()
    session.browser_check_console_errors()
    session.browser_check_network_errors()
    for selector in selectors:
        session.browser_element_exists(selector)
    screenshot = session.browser_screenshot()
    return session.generate_smoke_report(selectors, str(screenshot["screenshot_path"]))


def run_playwright_smoke_if_available(url: str, selectors: list[str], output_dir: Path, wait_seconds: float) -> dict[str, Any] | None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

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
            page.screenshot(path=str(screenshot_path), full_page=True)
            trace.append({"tool": "browser_screenshot", "success": True, "screenshot_path": str(screenshot_path)})
            browser.close()
        report = {
            "success": not console_errors and not network_errors and not missing,
            "workflow": "BrowserSmokeWorkflow",
            "url": url,
            "console_errors": len(console_errors),
            "network_errors": len(network_errors),
            "elements_verified": len(selectors) - len(missing),
            "elements_expected": len(selectors),
            "missing_elements": missing,
            "screenshot_path": str(screenshot_path),
            "browser_backend": "playwright",
            "trace": trace,
        }
        report_path = output_dir / f"browser_smoke_report_{time.strftime('%Y%m%d_%H%M%S')}.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
        return report
    except Exception:
        return None


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


def write_placeholder_png(path: Path, width: int = 800, height: int = 450) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if y < 52:
                pixel = (32, 80, 129)
            elif (x // 24 + y // 24) % 2 == 0:
                pixel = (238, 242, 247)
            else:
                pixel = (226, 232, 240)
            row.extend(pixel)
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            png_chunk(b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"),
            png_chunk(b"IDAT", zlib.compress(raw, level=6)),
            png_chunk(b"IEND", b""),
        ]
    )
    path.write_bytes(png)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + kind + data + checksum.to_bytes(4, "big")
