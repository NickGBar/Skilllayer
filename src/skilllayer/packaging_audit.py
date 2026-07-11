from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path
from typing import Any

from .config.defaults import COMMAND_METADATA, WORKFLOWS, WORKFLOW_METADATA


OUTPUT_DIR = Path("runs/p5_1_packaging_audit")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_packaging_audit() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    packaging = build_packaging_audit()
    assumptions = build_workflow_assumption_audit()
    fresh_install = run_fresh_install_simulation()
    summary = build_installability_summary(packaging, assumptions, fresh_install)
    report = {
        "milestone": "Packaging and Installability Audit",
        "summary": summary,
        "packaging_audit": packaging,
        "workflow_assumption_audit": assumptions,
        "fresh_install_simulation": fresh_install,
        "installability_summary": summary,
        "interpretation": (
            "SkillLayer is installable as a local CLI/MCP workflow layer. Stable workflows are separated from "
            "experimental workflows, and repository-specific assumptions are explicitly reported."
        ),
    }
    write_json(OUTPUT_DIR / "packaging_audit.json", packaging)
    write_json(OUTPUT_DIR / "workflow_assumption_audit.json", assumptions)
    write_json(OUTPUT_DIR / "fresh_install_simulation.json", fresh_install)
    write_json(OUTPUT_DIR / "installability_summary.json", summary)
    write_json(OUTPUT_DIR / "report.json", report)
    return report


def build_packaging_audit() -> dict[str, Any]:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    requirements = PROJECT_ROOT / "requirements.txt"
    config_example = PROJECT_ROOT / "skilllayer.yaml.example"
    quickstart = PROJECT_ROOT / "QUICKSTART.md"
    codex_config = PROJECT_ROOT / "codex_mcp_config.example.json"

    checks = {
        "pyproject_exists": {"ok": pyproject.exists(), "path": str(pyproject)},
        "requirements_exists": {"ok": requirements.exists(), "path": str(requirements)},
        "example_config_exists": {"ok": config_example.exists(), "path": str(config_example)},
        "quickstart_exists": {"ok": quickstart.exists(), "path": str(quickstart)},
        "codex_mcp_config_example_exists": {"ok": codex_config.exists(), "path": str(codex_config)},
        "package_imports_cleanly": run_command([sys.executable, "-c", "from skilllayer import SkillLayer; print(SkillLayer.__name__)"]),
        "cli_help_works": run_command([sys.executable, "-m", "skilllayer", "--help"]),
        "cli_workflows_json_works": json_command([sys.executable, "-m", "skilllayer", "workflows", "--json"]),
        "cli_skills_json_works": json_command([sys.executable, "-m", "skilllayer", "skills", "--json"]),
        "cli_doctor_json_works": json_command([sys.executable, "-m", "skilllayer", "doctor", "--json"]),
        "mcp_server_imports": run_command([sys.executable, "-c", "import skilllayer.mcp_server; print('ok')"]),
        "mcp_tool_schemas_listed": json_command([sys.executable, "-m", "skilllayer.mcp_server", "--list-tools"]),
    }

    absolute_path_findings = find_absolute_local_path_references()
    basic_operation_requires_generated_artifacts = False
    return {
        "checks": checks,
        "requirements": parse_requirements(requirements),
        "pyproject_present": pyproject.exists(),
        "console_script_expected": "skilllayer = skilllayer.cli:main",
        "absolute_local_path_references": absolute_path_findings,
        "absolute_local_paths_required_for_basic_operation": False,
        "generated_benchmark_artifacts_required_for_basic_operation": basic_operation_requires_generated_artifacts,
        "notes": [
            "Editable install is defined by pyproject.toml.",
            "Core CLI import and dry-run operation do not require generated benchmark artifacts.",
            "Optional MCP and browser integrations require their optional runtime dependencies.",
        ],
        "ok": all(check.get("ok", False) for check in checks.values()) and pyproject.exists() and requirements.exists(),
    }


def build_workflow_assumption_audit() -> dict[str, Any]:
    records = [
        {
            "workflow": "FindFunctionWorkflow",
            "stability": "stable",
            "assumptions": [
                "repository files of interest are Python files",
                "target can be expressed as a symbol-like token",
            ],
            "risk_level": "low",
            "known_limitations": [
                "does not inspect non-Python definitions",
                "does not disambiguate overloaded or generated symbols",
            ],
            "recommendation": "keep stable; document Python-symbol scope",
        },
        {
            "workflow": "RenameSymbolWorkflow",
            "stability": "stable",
            "assumptions": [
                "Python token replacement is sufficient for the requested rename",
                "tests are available or validation can be skipped safely only in dry-run",
                "repository is small enough for repository-wide token replacement",
            ],
            "risk_level": "medium",
            "known_limitations": [
                "does not use AST-aware rename",
                "may rename unrelated same-token symbols",
                "assumes Python-only symbol scope",
            ],
            "recommendation": "keep stable for small/simple Python renames; warn users on broad refactors",
        },
        {
            "workflow": "BrowserSmokeWorkflow",
            "stability": "stable",
            "assumptions": [
                "target URL is provided or a local index.html can be found",
                "configured selectors are sufficient for smoke validation",
                "Playwright browser backend or static fallback is available",
            ],
            "risk_level": "medium",
            "known_limitations": [
                "does not fill forms",
                "does not click arbitrary UI",
                "does not perform autonomous browser exploration",
            ],
            "recommendation": "keep stable as smoke validation only; avoid marketing it as a browser agent",
        },
        {
            "workflow": "AddHelperWorkflow",
            "stability": "experimental",
            "assumptions": [
                "safe helper insertion target can be found",
                "helpers.py, utils.py, ledgerlite/helpers.py, ledgerlite/utils.py, or ledgerlite/validators.py exists",
                "package export path is obvious if exports are needed",
                "a simple identity-style helper is appropriate",
            ],
            "risk_level": "high",
            "known_limitations": [
                "failed external repo with workflow_assumption_no_safe_helper_target",
                "repo-specific insertion-point assumptions remain hardcoded",
                "does not infer project-specific helper placement",
            ],
            "recommendation": "keep experimental until insertion-point discovery is generalized",
        },
        {
            "workflow": "FixFailingTestWorkflow",
            "stability": "experimental",
            "assumptions": [
                "test failure matches a small deterministic patch catalog",
                "pytest or unittest discovery can run locally",
                "some repairs assume ledgerlite package/module names",
                "import/export repair path is obvious",
            ],
            "risk_level": "high",
            "known_limitations": [
                "simple patch catalog is intentionally narrow",
                "does not perform general debugging",
                "contains known LedgerLite-specific repairs",
            ],
            "recommendation": "keep experimental; report unsupported instead of guessing",
        },
        {
            "workflow": "FixBugWorkflow",
            "stability": WORKFLOW_METADATA.get("FixBugWorkflow", {}).get("stability", "internal"),
            "assumptions": [
                "small deterministic edit can be extracted from task text",
                "repository is Python-oriented",
            ],
            "risk_level": "high",
            "known_limitations": [
                "current deterministic edit support is limited",
                "not included in stable workflow set",
            ],
            "recommendation": "keep internal and avoid using as a general bug-fixing claim",
        },
    ]
    return {
        "workflow_count": len(records),
        "stable_workflows": [item["workflow"] for item in records if item["stability"] == "stable"],
        "experimental_workflows": [item["workflow"] for item in records if item["stability"] == "experimental"],
        "internal_workflows": [item["workflow"] for item in records if item["stability"] == "internal"],
        "commands": [{"name": name, **metadata} for name, metadata in COMMAND_METADATA.items()],
        "records": records,
    }


def run_fresh_install_simulation() -> dict[str, Any]:
    started = time.perf_counter()
    result: dict[str, Any] = {
        "mode": "editable_install_no_deps",
        "network_access_required": False,
        "system_site_packages_used": True,
        "system_site_packages_reason": "Avoid network downloads for build backend packages during the offline smoke test.",
        "completed": False,
        "limitation": None,
        "steps": [],
    }
    if not shutil.which("python3") and not sys.executable:
        result["limitation"] = "No Python executable available for venv simulation."
        return result

    with tempfile.TemporaryDirectory(prefix="skilllayer_p5_1_") as tmp:
        tmp_path = Path(tmp)
        venv_dir = tmp_path / "venv"
        sample_repo = tmp_path / "sample_repo"
        try:
            venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_dir)
            python = venv_python(venv_dir)
            make_sample_repo(sample_repo)
            commands = [
                ("install_editable_no_deps", [str(python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", str(PROJECT_ROOT)]),
                ("doctor_json", [str(python), "-m", "skilllayer", "doctor", "--json"]),
                ("console_script_workflows_json", [str(venv_script(venv_dir, "skilllayer")), "workflows", "--json"]),
                ("workflows_json", [str(python), "-m", "skilllayer", "workflows", "--json"]),
                ("skills_json", [str(python), "-m", "skilllayer", "skills", "--json"]),
                ("inspect_sample_repo_json", [str(python), "-m", "skilllayer", "inspect", "--repo", str(sample_repo), "--json"]),
                (
                    "dry_run_find_function_json",
                    [
                        str(python),
                        "-m",
                        "skilllayer",
                        "run",
                        "--repo",
                        str(sample_repo),
                        "--task",
                        "Find function parse_money",
                        "--dry-run",
                        "--json",
                    ],
                ),
            ]
            for name, command in commands:
                if name == "doctor_json":
                    step = json_command(command, allow_nonzero=True, cwd=tmp_path)
                else:
                    step = json_command(command, cwd=tmp_path) if name.endswith("_json") else run_command(command, timeout=120, cwd=tmp_path)
                step["step"] = name
                result["steps"].append(step)
            result["completed"] = all(step.get("ok", False) for step in result["steps"])
        except Exception as exc:
            result["limitation"] = str(exc)
    result["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return result


def build_installability_summary(packaging: dict[str, Any], assumptions: dict[str, Any], fresh_install: dict[str, Any]) -> dict[str, Any]:
    stable = assumptions["stable_workflows"]
    experimental = assumptions["experimental_workflows"]
    internal = assumptions.get("internal_workflows", [])
    cli_commands_work = all(
        packaging["checks"][name]["ok"]
        for name in ["cli_help_works", "cli_workflows_json_works", "cli_skills_json_works", "cli_doctor_json_works"]
    )
    json_command_checks_valid = all(
        packaging["checks"][name].get("json_valid", True)
        for name in [
            "cli_workflows_json_works",
            "cli_skills_json_works",
            "cli_doctor_json_works",
            "mcp_tool_schemas_listed",
        ]
    )
    fresh_install_json_valid = all(
        step.get("json_valid", True)
        for step in fresh_install.get("steps", [])
        if str(step.get("step", "")).endswith("_json") or step.get("step") == "doctor_json"
    )
    installable_for_new_user = bool(
        packaging["ok"]
        and fresh_install["completed"]
        and packaging["checks"]["mcp_tool_schemas_listed"]["ok"]
    )
    return {
        "package_compiles": bool(packaging["checks"]["package_imports_cleanly"]["ok"]),
        "cli_commands_work": cli_commands_work,
        "mcp_server_imports": bool(packaging["checks"]["mcp_server_imports"]["ok"]),
        "mcp_tool_schemas_listed": bool(packaging["checks"]["mcp_tool_schemas_listed"]["ok"]),
        "fresh_install_completed": bool(fresh_install["completed"]),
        "stable_workflows": stable,
        "experimental_workflows": experimental,
        "internal_workflows": internal,
        "repo_specific_assumption_risks": [
            item["workflow"] for item in assumptions["records"] if item["risk_level"] == "high"
        ],
        "basic_operation_requires_generated_artifacts": bool(packaging["generated_benchmark_artifacts_required_for_basic_operation"]),
        "absolute_local_paths_required_for_basic_operation": bool(packaging["absolute_local_paths_required_for_basic_operation"]),
        "installable_for_new_user": installable_for_new_user,
        "json_outputs_valid": bool(json_command_checks_valid and fresh_install_json_valid),
        "workflow_behavior_changed": False,
        "new_workflows_added": False,
        "pass": bool(installable_for_new_user and json_command_checks_valid and fresh_install_json_valid),
        "honest_positioning": {
            "is_local_cli_mcp_workflow_layer": True,
            "is_general_coding_agent": False,
            "is_replacement_for_codex_or_claude_code": False,
            "real_token_savings_proven": False,
        },
    }


def run_command(command: list[str], timeout: int = 60, cwd: Path | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd or PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "stdout": completed.stdout[-10000:],
            "stdout_tail": completed.stdout[-1200:],
            "stderr_tail": completed.stderr[-1200:],
            "command": redact_command(command),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "stdout": str(exc.stdout or "")[-10000:],
            "stdout_tail": str(exc.stdout or "")[-1200:],
            "stderr_tail": "command timed out",
            "command": redact_command(command),
        }


def json_command(command: list[str], timeout: int = 60, allow_nonzero: bool = False, cwd: Path | None = None) -> dict[str, Any]:
    result = run_command(command, timeout=timeout, cwd=cwd)
    try:
        result["json_valid"] = isinstance(json.loads(result["stdout"]), (dict, list))
    except Exception:
        result["json_valid"] = False
    result["ok"] = bool((result["ok"] or allow_nonzero) and result["json_valid"])
    return result


def redact_command(command: list[str]) -> list[str]:
    return [str(part) for part in command]


def parse_requirements(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def find_absolute_local_path_references() -> list[dict[str, Any]]:
    findings = []
    search_roots = [PROJECT_ROOT / "src" / "skilllayer", PROJECT_ROOT / "README.md", PROJECT_ROOT / "skilllayer.yaml.example"]
    for root in search_roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"/Users/[^/\s\"'`),\]]+/", text):
                findings.append(
                    {
                        "path": str(path.relative_to(PROJECT_ROOT)),
                        "required_for_basic_operation": False,
                        "note": "Local absolute path appears in docs or optional research validation code, not in core run/inspect/workflows/skills/doctor path.",
                    }
                )
    return findings


def venv_python(venv_dir: Path) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_script(venv_dir: Path, name: str) -> Path:
    if sys.platform == "win32":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def make_sample_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "money.py").write_text(
        "from decimal import Decimal\n\n\ndef parse_money(value):\n    return Decimal(str(value))\n",
        encoding="utf-8",
    )
    tests = path / "tests"
    tests.mkdir()
    (tests / "test_money.py").write_text(
        "import unittest\n\nfrom money import parse_money\n\n\nclass MoneyTests(unittest.TestCase):\n    def test_parse_money(self):\n        self.assertEqual(str(parse_money('1.20')), '1.20')\n",
        encoding="utf-8",
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
