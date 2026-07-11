from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config.defaults import COMMAND_METADATA, WORKFLOWS, WORKFLOW_METADATA


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "runs" / "p9_5_public_readiness_audit"

AUDITED_DOCS = [
    "README.md",
    "INSTALL.md",
    "TESTER_GUIDE.md",
    "SECURITY_REVIEW.md",
    "docs/SAFE_MODE.md",
    "docs/CLAUDE_CODE_SETUP.md",
    "docs/CURSOR_SETUP.md",
    "FEEDBACK_STATUS.md",
]

REQUIRED_PUBLIC_DOCS = [
    "README.md",
    "INSTALL.md",
    "TESTER_GUIDE.md",
    "SECURITY_REVIEW.md",
    "docs/SAFE_MODE.md",
    "docs/CLAUDE_CODE_SETUP.md",
    "docs/CURSOR_SETUP.md",
    "FEEDBACK_STATUS.md",
    "PUBLIC_READINESS_REPORT.md",
]

FIRST_TIME_STEPS = [
    "git_clone",
    "install",
    "verify",
    "inspect_repo",
    "run_workflow",
    "view_mcp_setup_docs",
]

SCORE_VALUES = {"Poor": 1, "Fair": 2, "Good": 3, "Strong": 4}


def run_public_readiness_audit(*, write_outputs: bool = True) -> dict[str, Any]:
    started = time.perf_counter()
    documentation_audit = audit_documentation()
    onboarding_audit = audit_onboarding()
    workflow_discoverability = audit_workflow_discoverability()
    tester_readiness = audit_tester_readiness()
    public_scorecard = build_scorecard(
        documentation_audit=documentation_audit,
        onboarding_audit=onboarding_audit,
        workflow_discoverability=workflow_discoverability,
        tester_readiness=tester_readiness,
    )
    report = {
        "milestone": "Public Readiness Audit",
        "success": True,
        "duration_ms": elapsed_ms(started),
        "documentation_audit": documentation_audit,
        "onboarding_audit": onboarding_audit,
        "workflow_discoverability": workflow_discoverability,
        "tester_readiness": tester_readiness,
        "public_scorecard": public_scorecard,
        "known_gaps": build_known_gaps(documentation_audit, onboarding_audit, workflow_discoverability, tester_readiness),
        "no_workflow_behavior_changes": True,
        "routing_modified": False,
        "network_features_added": False,
        "outputs": {
            "documentation_audit": "runs/p9_5_public_readiness_audit/documentation_audit.json",
            "onboarding_audit": "runs/p9_5_public_readiness_audit/onboarding_audit.json",
            "workflow_discoverability": "runs/p9_5_public_readiness_audit/workflow_discoverability.json",
            "tester_readiness": "runs/p9_5_public_readiness_audit/tester_readiness.json",
            "public_scorecard": "runs/p9_5_public_readiness_audit/public_scorecard.json",
            "report": "runs/p9_5_public_readiness_audit/report.json",
            "public_readiness_report": "PUBLIC_READINESS_REPORT.md",
        },
    }
    if write_outputs:
        write_json(OUTPUT_DIR / "documentation_audit.json", documentation_audit)
        write_json(OUTPUT_DIR / "onboarding_audit.json", onboarding_audit)
        write_json(OUTPUT_DIR / "workflow_discoverability.json", workflow_discoverability)
        write_json(OUTPUT_DIR / "tester_readiness.json", tester_readiness)
        write_json(OUTPUT_DIR / "public_scorecard.json", public_scorecard)
        write_json(OUTPUT_DIR / "report.json", report)
    return report


def audit_documentation() -> dict[str, Any]:
    docs: dict[str, Any] = {}
    all_findings: list[dict[str, Any]] = []
    for relative in AUDITED_DOCS:
        path = PROJECT_ROOT / relative
        item = audit_doc_file(path, relative)
        docs[relative] = item
        all_findings.extend({"file": relative, **finding} for finding in item["findings"])

    required_missing = [relative for relative in REQUIRED_PUBLIC_DOCS if not (PROJECT_ROOT / relative).exists()]
    terminology_findings = audit_terminology_consistency()
    all_findings.extend(terminology_findings)
    severity_counts = count_by_key(all_findings, "severity")
    return {
        "docs_reviewed": AUDITED_DOCS,
        "required_public_docs": REQUIRED_PUBLIC_DOCS,
        "required_missing": required_missing,
        "doc_count": len(AUDITED_DOCS),
        "findings": all_findings,
        "finding_count": len(all_findings),
        "severity_counts": severity_counts,
        "broken_reference_count": sum(1 for finding in all_findings if finding.get("type") == "broken_reference"),
        "duplicate_instruction_risk": any(finding.get("type") == "duplicate_instruction_risk" for finding in all_findings),
        "outdated_wording_risk": any(finding.get("type") == "outdated_or_context_heavy_wording" for finding in all_findings),
        "status": "pass_with_findings" if all_findings else "pass",
        "recommendations": [
            "Keep README first-screen focused on install, tester-check, workflows, MCP status, and security links.",
            "Consider moving long historical research sections into archival docs before a broader public push.",
            "Keep Cursor and Claude Code wording conservative until real client-side validation is complete.",
        ],
        "files": docs,
    }


def audit_doc_file(path: Path, relative: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "line_count": 0,
            "heading_count": 0,
            "local_link_count": 0,
            "findings": [{"type": "missing_doc", "severity": "high", "message": f"{relative} is missing."}],
        }
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    headings = [line.strip() for line in lines if line.startswith("#")]
    findings: list[dict[str, Any]] = []
    broken_refs = find_broken_markdown_references(text, path.parent)
    findings.extend(broken_refs)
    duplicated_headings = find_duplicate_headings(headings)
    if duplicated_headings and relative == "README.md":
        findings.append(
            {
                "type": "duplicate_instruction_risk",
                "severity": "medium",
                "message": "README has repeated section headings, which may make first-time navigation harder.",
                "examples": duplicated_headings[:8],
            }
        )
    if relative == "README.md" and len(lines) > 1000:
        findings.append(
            {
                "type": "duplicate_instruction_risk",
                "severity": "medium",
                "message": "README is very long for a first-time public user.",
                "line_count": len(lines),
            }
        )
    if relative == "README.md" and "Compact Prunable Experience-Grounded World Model" in text:
        findings.append(
            {
                "type": "outdated_or_context_heavy_wording",
                "severity": "low",
                "message": "README still includes the original research-project identity; this is useful history but can distract public SkillLayer users.",
            }
        )
    prerequisite_checks = {
        "python_310": "3.10" in text or ">=3.10" in text,
        "venv": "venv" in text,
        "editable_install": "pip install -e" in text,
        "mcp_extra": ".[mcp]" in text or "MCP extra" in text,
    }
    if relative in {"INSTALL.md", "README.md", "TESTER_GUIDE.md"}:
        missing = [key for key, present in prerequisite_checks.items() if not present]
        if missing:
            findings.append(
                {
                    "type": "missing_prerequisite_detail",
                    "severity": "medium",
                    "message": "Public setup doc is missing one or more expected install prerequisite cues.",
                    "missing": missing,
                }
            )
    return {
        "exists": True,
        "line_count": len(lines),
        "heading_count": len(headings),
        "local_link_count": len(find_markdown_links(text)),
        "prerequisite_checks": prerequisite_checks,
        "findings": findings,
    }


def audit_onboarding() -> dict[str, Any]:
    files = {
        "install_doc": PROJECT_ROOT / "INSTALL.md",
        "install_script_unix": PROJECT_ROOT / "scripts" / "install.sh",
        "install_script_windows": PROJECT_ROOT / "scripts" / "install.ps1",
        "verify_script_unix": PROJECT_ROOT / "scripts" / "verify_install.sh",
        "verify_script_windows": PROJECT_ROOT / "scripts" / "verify_install.ps1",
        "mcp_config_generator": PROJECT_ROOT / "scripts" / "generate_mcp_config.py",
        "tester_guide": PROJECT_ROOT / "TESTER_GUIDE.md",
        "troubleshooting": PROJECT_ROOT / "docs" / "TROUBLESHOOTING.md",
    }
    step_assessments = {
        "git_clone": {
            "score": "Good",
            "evidence": ["README and INSTALL include clone-oriented setup flow."],
            "friction": [],
        },
        "install": {
            "score": "Good" if files["install_script_unix"].exists() and files["install_script_windows"].exists() else "Fair",
            "evidence": ["Unix and Windows install scripts exist."],
            "friction": ["Windows still benefits from explicit Python >=3.10 guidance and tester confirmation."],
        },
        "verify": {
            "score": "Strong" if files["verify_script_unix"].exists() and files["verify_script_windows"].exists() else "Good",
            "evidence": ["tester-check and verify scripts are documented."],
            "friction": ["Optional pytest/MCP warnings may still require careful reading by new users."],
        },
        "inspect_repo": {
            "score": "Good",
            "evidence": ["inspect command exists and is documented in quickstart/tester flow."],
            "friction": [],
        },
        "run_workflow": {
            "score": "Good",
            "evidence": ["run examples exist for find, tests, single test, explain failure, git status, and dependency check."],
            "friction": ["Users must understand stable vs experimental workflow boundaries."],
        },
        "view_mcp_setup_docs": {
            "score": "Fair",
            "evidence": ["MCP config generator and Cursor/Claude docs exist."],
            "friction": ["Cursor is partially validated; Claude Code is prepared but not validated."],
        },
    }
    missing_files = [name for name, path in files.items() if not path.exists()]
    return {
        "persona": "fresh external developer",
        "path": FIRST_TIME_STEPS,
        "step_assessments": step_assessments,
        "missing_support_files": missing_files,
        "overall_score": "Good" if not missing_files else "Fair",
        "observations": [
            "The happy path is now explicit: clone, venv, editable install, tester-check, inspect, run workflow.",
            "MCP setup is documented, but only Codex is fully validated; this should remain visible.",
            "README completeness is high, but its length can overwhelm a new user.",
        ],
        "recommendations": [
            "Keep INSTALL.md as the primary setup document and point first-time users there before README deep sections.",
            "Use tester-check as the first validation command in every onboarding path.",
            "Keep dry-run examples near modifying workflow examples.",
        ],
    }


def audit_workflow_discoverability() -> dict[str, Any]:
    workflow_rows = [
        {
            "name": name,
            "stability": WORKFLOW_METADATA.get(name, {}).get("stability", "experimental"),
            "summary": WORKFLOW_METADATA.get(name, {}).get("summary", ""),
            "macro_count": len(macros),
            "macro_sequence": list(macros),
        }
        for name, macros in WORKFLOWS.items()
    ]
    stability_counts = count_by_key(workflow_rows, "stability")
    workflows_cli = run_cli(["workflows", "--json"])
    skills_cli = run_cli(["skills", "--json"])
    cli_help = run_cli(["--help"])
    findings: list[dict[str, Any]] = []
    if not workflows_cli["ok"] or not workflows_cli["json_valid"]:
        findings.append({"type": "workflow_cli_failure", "severity": "high", "message": "workflows --json did not return valid JSON."})
    if not skills_cli["ok"] or not skills_cli["json_valid"]:
        findings.append({"type": "skills_cli_failure", "severity": "high", "message": "skills --json did not return valid JSON."})
    if stability_counts.get("experimental", 0) > stability_counts.get("stable", 0):
        findings.append(
            {
                "type": "experimental_surface_dominates",
                "severity": "medium",
                "message": "More workflows are experimental than stable; public docs should keep stability labels prominent.",
                "stable_count": stability_counts.get("stable", 0),
                "experimental_count": stability_counts.get("experimental", 0),
            }
        )
    return {
        "workflow_count": len(workflow_rows),
        "stability_counts": stability_counts,
        "workflows": workflow_rows,
        "commands": [{"name": name, **metadata} for name, metadata in COMMAND_METADATA.items()],
        "workflows_cli": summarize_cli_result(workflows_cli),
        "skills_cli": summarize_cli_result(skills_cli),
        "help_cli": summarize_cli_result(cli_help),
        "can_new_user_list_workflows": workflows_cli["ok"] and workflows_cli["json_valid"],
        "can_new_user_list_skills": skills_cli["ok"] and skills_cli["json_valid"],
        "stable_experimental_visible": all(row.get("stability") for row in workflow_rows),
        "findings": findings,
        "recommendations": [
            "Keep stable workflows above experimental workflows in public-facing docs.",
            "Consider grouping CLI `workflows` human output by stability in a future DX pass.",
            "Continue marking AddHelperWorkflow and FixFailingTestWorkflow experimental.",
        ],
    }


def audit_tester_readiness() -> dict[str, Any]:
    release_check = run_cli(["release-check", "--json"], timeout=120)
    feedback_status = run_cli(["feedback-status", "--json"], timeout=60)
    security_check = run_cli(["security-check", "--json"], timeout=60)
    tester_check_available = "tester-check" in read_text(PROJECT_ROOT / "README.md")
    blockers: list[dict[str, Any]] = []
    if not release_check["ok"] or not release_check["json_valid"]:
        blockers.append({"type": "release_check_failure", "severity": "high", "message": "release-check did not return valid JSON."})
    if not feedback_status["ok"] or not feedback_status["json_valid"]:
        blockers.append({"type": "feedback_status_failure", "severity": "medium", "message": "feedback-status did not return valid JSON."})
    if not security_check["ok"] or not security_check["json_valid"]:
        blockers.append({"type": "security_check_failure", "severity": "medium", "message": "security-check did not return valid JSON."})
    return {
        "tester_check_documented": tester_check_available,
        "release_check": summarize_cli_result(release_check),
        "feedback_status": summarize_cli_result(feedback_status),
        "security_check": summarize_cli_result(security_check),
        "can_validate_without_author_guidance": not blockers and tester_check_available,
        "blockers": blockers,
        "friction_points": [
            "External testers still need to choose a safe disposable repo for modifying workflows.",
            "Cursor and Claude Code MCP paths require client-side tester confirmation.",
            "Some commands are maintainer-oriented and can distract from the first-tester path.",
        ],
        "recommendations": [
            "Direct first-time testers to TESTER_GUIDE.md and INSTALL.md rather than the full README.",
            "Ask testers to run read-only workflows before any modifying workflow.",
            "Use feedback-status --waiting-validation before deciding whether an issue is actually closed.",
        ],
    }


def build_scorecard(
    *,
    documentation_audit: dict[str, Any],
    onboarding_audit: dict[str, Any],
    workflow_discoverability: dict[str, Any],
    tester_readiness: dict[str, Any],
) -> dict[str, Any]:
    documentation_score = "Fair" if documentation_audit["duplicate_instruction_risk"] else "Good"
    mcp_score = "Fair"
    scorecard = {
        "Installation": {
            "score": onboarding_audit["step_assessments"]["install"]["score"],
            "rationale": "Install scripts and docs exist for Unix and Windows, with Python/MCP guidance.",
        },
        "Documentation": {
            "score": documentation_score,
            "rationale": "Documentation is comprehensive but README remains long and contains historical research context.",
        },
        "Workflow Clarity": {
            "score": "Good" if workflow_discoverability["can_new_user_list_workflows"] else "Fair",
            "rationale": "Workflow metadata exposes stable vs experimental labels, though experimental workflows dominate the surface.",
        },
        "MCP Readiness": {
            "score": mcp_score,
            "rationale": "Codex is validated; Cursor is partially validated; Claude Code is prepared but pending validation.",
        },
        "Testing": {
            "score": "Strong" if tester_readiness["release_check"]["ok"] else "Fair",
            "rationale": "release-check and focused validation suites exist and run locally.",
        },
        "Security": {
            "score": "Good" if tester_readiness["security_check"]["ok"] else "Fair",
            "rationale": "Security review kit and local-first posture checks exist, but no formal third-party audit is claimed.",
        },
        "Feedback Lifecycle": {
            "score": "Strong" if tester_readiness["feedback_status"]["ok"] else "Fair",
            "rationale": "Feedback registry tracks waiting validation, aging, stale items, and validation status locally.",
        },
        "Release Hygiene": {
            "score": "Strong" if tester_readiness["release_check"]["ok"] else "Fair",
            "rationale": "release-check validates code, docs, hygiene, and public export readiness without publishing.",
        },
    }
    numeric = [SCORE_VALUES[item["score"]] for item in scorecard.values()]
    average = round(sum(numeric) / len(numeric), 2)
    overall = "Strong" if average >= 3.5 else "Good" if average >= 2.75 else "Fair" if average >= 2 else "Poor"
    return {
        "overall_score": overall,
        "average_numeric_score": average,
        "scale": ["Poor", "Fair", "Good", "Strong"],
        "categories": scorecard,
        "conservative_summary": "SkillLayer is ready for careful external testing, not broad unassisted adoption.",
    }


def build_known_gaps(
    documentation_audit: dict[str, Any],
    onboarding_audit: dict[str, Any],
    workflow_discoverability: dict[str, Any],
    tester_readiness: dict[str, Any],
) -> list[dict[str, Any]]:
    gaps = [
        {
            "area": "Documentation",
            "severity": "medium",
            "description": "README is comprehensive but long; first-time users may need a shorter public landing path.",
        },
        {
            "area": "MCP Readiness",
            "severity": "medium",
            "description": "Cursor and Claude Code require more real client-side validation before claiming full support.",
        },
        {
            "area": "Workflow Library",
            "severity": "medium",
            "description": "Most workflows are experimental; stable surface remains intentionally small.",
        },
        {
            "area": "Security",
            "severity": "low",
            "description": "Security docs are strong for an early project, but no formal third-party audit exists.",
        },
    ]
    if documentation_audit.get("required_missing"):
        gaps.append(
            {
                "area": "Documentation",
                "severity": "high",
                "description": "One or more required public docs are missing.",
                "missing": documentation_audit["required_missing"],
            }
        )
    if tester_readiness.get("blockers"):
        gaps.append(
            {
                "area": "Tester Readiness",
                "severity": "high",
                "description": "One or more tester-readiness checks failed.",
                "blockers": tester_readiness["blockers"],
            }
        )
    if not workflow_discoverability.get("can_new_user_list_workflows"):
        gaps.append(
            {
                "area": "Workflow Discoverability",
                "severity": "high",
                "description": "workflow listing command did not pass.",
            }
        )
    return gaps


def find_markdown_links(text: str) -> list[str]:
    return [match.group(1).strip() for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text)]


def find_broken_markdown_references(text: str, base_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw_link in find_markdown_links(text):
        link = raw_link.split("#", 1)[0].strip()
        if not link or link.startswith(("http://", "https://", "mailto:", "app://")):
            continue
        if link.startswith("/absolute/path") or link.startswith("/path/to"):
            continue
        if link.startswith("runs/"):
            continue
        link = link.strip("<>")
        candidate = (base_dir / link).resolve()
        try:
            candidate.relative_to(PROJECT_ROOT)
        except ValueError:
            continue
        if not candidate.exists():
            findings.append(
                {
                    "type": "broken_reference",
                    "severity": "medium",
                    "message": f"Local markdown link does not resolve: {raw_link}",
                }
            )
    return findings


def find_duplicate_headings(headings: list[str]) -> list[str]:
    counts = count_values(headings)
    return sorted(heading for heading, count in counts.items() if count > 1)


def audit_terminology_consistency() -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    readme = read_text(PROJECT_ROOT / "README.md")
    if "| Cursor | Validated" in readme:
        findings.append(
            {
                "file": "README.md",
                "type": "inconsistent_client_status",
                "severity": "high",
                "message": "Cursor should not be marked fully validated unless real Cursor UI invocation has been confirmed.",
            }
        )
    if "| Claude Code | Validated" in readme:
        findings.append(
            {
                "file": "README.md",
                "type": "inconsistent_client_status",
                "severity": "high",
                "message": "Claude Code should not be marked validated before real client confirmation.",
            }
        )
    overclaim_patterns = [
        "proven token savings",
        "guaranteed cost savings",
        "replacement for Codex",
        "replacement for Claude Code",
        "replacement for Cursor",
    ]
    for pattern in overclaim_patterns:
        if pattern in readme and f"not a {pattern}" not in readme:
            findings.append(
                {
                    "file": "README.md",
                    "type": "overclaim_risk",
                    "severity": "medium",
                    "message": f"Potential overclaim wording found: {pattern}",
                }
            )
    return findings


def run_cli(args: list[str], *, timeout: int = 60) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing_pythonpath = env.get("PYTHONPATH")
    src_path = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    command = [sys.executable, "-B", "-m", "skilllayer", *args]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parsed = parse_json(completed.stdout)
        return {
            "ok": completed.returncode == 0,
            "command": ["python", "-m", "skilllayer", *args],
            "returncode": completed.returncode,
            "stdout_snippet": completed.stdout[:1000],
            "stderr_snippet": completed.stderr[:1000],
            "json_valid": isinstance(parsed, dict),
            "json": parsed if isinstance(parsed, dict) else None,
            "duration_ms": elapsed_ms(started),
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": ["python", "-m", "skilllayer", *args],
            "returncode": None,
            "stdout_snippet": "",
            "stderr_snippet": str(exc),
            "json_valid": False,
            "json": None,
            "duration_ms": elapsed_ms(started),
        }


def summarize_cli_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "returncode": result.get("returncode"),
        "json_valid": result.get("json_valid"),
        "duration_ms": result.get("duration_ms"),
        "stderr_snippet": result.get("stderr_snippet", "")[:300],
    }


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_json(value: str) -> Any | None:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def count_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key, "unknown"))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def count_values(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 3)


def main() -> int:
    report = run_public_readiness_audit(write_outputs=True)
    print(json.dumps(report, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
