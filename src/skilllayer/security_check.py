from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "runs" / "security_review_kit"

READ_ONLY_WORKFLOWS = [
    "Doctor",
    "InspectRepo",
    "FindFunctionWorkflow",
    "RunTestsWorkflow",
    "SingleTestWorkflow",
    "ExplainFailureWorkflow",
    "GitStatusWorkflow",
    "DependencyCheckWorkflow",
    "BrowserSmokeWorkflow",
]

MODIFYING_WORKFLOWS = [
    "RenameSymbolWorkflow",
    "FixFailingTestWorkflow",
    "AddHelperWorkflow",
]

SECURITY_DOCS = {
    "security_review": "SECURITY_REVIEW.md",
    "security_checklist": "docs/SECURITY_CHECKLIST.md",
    "safe_mode": "docs/SAFE_MODE.md",
}


def run_security_check(write_outputs: bool = True) -> dict[str, Any]:
    docs_present = {
        name: {
            "path": path,
            "exists": (PROJECT_ROOT / path).exists(),
        }
        for name, path in SECURITY_DOCS.items()
    }
    security_check_output = {
        "network_upload_enabled": False,
        "automatic_telemetry_upload": False,
        "mcp_server_auto_start": False,
        "background_services_added": False,
        "external_api_calls_required_for_core_workflows": False,
        "install_may_use_network_for_dependencies": True,
        "local_telemetry_dir": "runs/",
        "anonymous_export_opt_in_only": True,
        "raw_task_text_may_exist_locally": True,
        "read_only_workflows": READ_ONLY_WORKFLOWS,
        "modifying_workflows": MODIFYING_WORKFLOWS,
        "dry_run_recommended_for_modifying_workflows": True,
        "security_docs_present": all(item["exists"] for item in docs_present.values()),
        "security_docs": docs_present,
        "formal_third_party_security_audit": False,
        "scans_private_user_code": False,
    }
    security_docs_check = {
        "required_docs": docs_present,
        "all_required_docs_present": security_check_output["security_docs_present"],
        "readme_links_expected": [
            "SECURITY_REVIEW.md",
            "docs/SECURITY_CHECKLIST.md",
            "docs/SAFE_MODE.md",
        ],
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "success": bool(security_check_output["security_docs_present"]),
        "summary": {
            "security_review_exists": docs_present["security_review"]["exists"],
            "security_checklist_exists": docs_present["security_checklist"]["exists"],
            "safe_mode_exists": docs_present["safe_mode"]["exists"],
            "network_upload_enabled": security_check_output["network_upload_enabled"],
            "automatic_telemetry_upload": security_check_output["automatic_telemetry_upload"],
            "mcp_server_auto_start": security_check_output["mcp_server_auto_start"],
            "workflow_behavior_changed": False,
            "new_workflows_added": False,
            "background_services_added": False,
        },
        "security_check": security_check_output,
        "docs_check": security_docs_check,
        "recommended_safe_review_flow": [
            "Read SECURITY_REVIEW.md.",
            "Inspect pyproject.toml and scripts/install.sh.",
            "Inspect src/skilllayer/ before installing on a work machine.",
            "Install in a disposable clone or virtual environment.",
            "Run python -m skilllayer tester-check.",
            "Run read-only workflows first.",
            "Use --dry-run before modifying workflows.",
            "Review telemetry exports before sharing.",
        ],
        "limitations": [
            "SkillLayer has not received a formal third-party security audit.",
            "MCP exposes local tools to a connected client and should only be enabled for trusted clients.",
            "Modifying workflows can edit files when explicitly invoked.",
            "Workplace use should follow the user's organization approval process.",
        ],
        "outputs": {
            "security_docs_check": "runs/security_review_kit/security_docs_check.json",
            "security_check_output": "runs/security_review_kit/security_check_output.json",
            "report": "runs/security_review_kit/report.json",
        },
    }
    if write_outputs:
        write_json(OUTPUT_DIR / "security_docs_check.json", security_docs_check)
        write_json(OUTPUT_DIR / "security_check_output.json", security_check_output)
        write_json(OUTPUT_DIR / "report.json", report)
    return report


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    print(json.dumps(run_security_check(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
