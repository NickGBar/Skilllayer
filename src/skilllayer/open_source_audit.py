from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = Path("runs/open_source_audit")

MAX_FILE_BYTES = 1_000_000
MAX_FINDINGS = 5000
MAX_FINDINGS_PER_FILE_PATTERN = 20

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".ipynb_checkpoints",
}

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".csv",
    ".env",
    ".example",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

SECRET_PATTERNS = [
    ("openai_api_key_name", re.compile(r"\bOPENAI_API_KEY\b", re.IGNORECASE), "high"),
    ("anthropic_api_key_name", re.compile(r"\bANTHROPIC_API_KEY\b", re.IGNORECASE), "high"),
    ("google_api_key_name", re.compile(r"\bGOOGLE_API_KEY\b", re.IGNORECASE), "high"),
    ("generic_api_key_name", re.compile(r"\bAPI_KEY\b", re.IGNORECASE), "medium"),
    ("secret_name", re.compile(r"\bSECRET\b", re.IGNORECASE), "medium"),
    ("token_name", re.compile(r"\bTOKEN\b", re.IGNORECASE), "medium"),
    ("password_name", re.compile(r"\bPASSWORD\b", re.IGNORECASE), "medium"),
    ("openai_key_like_value", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "critical"),
    ("github_token_like_value", re.compile(r"\bghp_[A-Za-z0-9_]{12,}\b"), "critical"),
    ("github_pat_like_value", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b"), "critical"),
    ("bearer_reference", re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), "high"),
    ("private_key_reference", re.compile(r"PRIVATE[_ -]?KEY|BEGIN [A-Z ]*PRIVATE KEY", re.IGNORECASE), "critical"),
    ("credentials_reference", re.compile(r"\bcredentials?\b", re.IGNORECASE), "medium"),
    ("macos_home_path", re.compile(r"/Users/[^/\s\"'`),\]]+/[^\s\"'`),\]]+"), "medium"),
    ("absolute_local_path", re.compile(r"(?<![A-Za-z0-9_])/(Users|home|private/var|var/folders)/[^\s\"'`),\]]+"), "medium"),
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "medium"),
    ("personal_name", re.compile(r"\b(NickGBar)\b"), "low"),
    ("localhost_port", re.compile(r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0):\d+\b", re.IGNORECASE), "low"),
    ("internal_project_name", re.compile(r"\b(Telepulse|telepulse|Duoscale|duoscale)\b"), "medium"),
]

SENSITIVE_PATH_PATTERNS = [
    (re.compile(r"(^|/)\.env($|\.)"), "env_file", "critical"),
    (re.compile(r"credentials?", re.IGNORECASE), "credentials_path", "high"),
    (re.compile(r"secret", re.IGNORECASE), "secret_path", "high"),
    (re.compile(r"token", re.IGNORECASE), "token_path", "high"),
]

PUBLIC_SAFE_EXACT = {
    ".gitignore",
    "README.md",
    "QUICKSTART.md",
    "codex_mcp_config.example.json",
    "pyproject.toml",
    "requirements.txt",
    "skilllayer.yaml.example",
    "src/skilllayer/__init__.py",
    "src/skilllayer/__main__.py",
    "src/skilllayer/run.py",
    "src/skilllayer/cli.py",
    "src/skilllayer/mcp_server.py",
    "src/skilllayer/config/__init__.py",
    "src/skilllayer/config/loader.py",
    "src/skilllayer/router/__init__.py",
    "src/skilllayer/router/cascade.py",
    "src/skilllayer/tools/inspect.py",
    "src/skilllayer/verifier/__init__.py",
    "src/skilllayer/verifier/test_runner.py",
}

PUBLIC_REVIEW_EXACT = {
    "src/skilllayer/config/defaults.py",
    "src/skilllayer/runner/__init__.py",
    "src/skilllayer/runner/core.py",
    "src/skilllayer/tools/__init__.py",
    "src/skilllayer/tools/execution.py",
    "src/skilllayer/tools/browser.py",
    "src/skilllayer/workflows/__init__.py",
    "src/skilllayer/workflows/definitions.py",
    "src/skilllayer/macros/__init__.py",
    "src/skilllayer/macros/definitions.py",
    "src/skilllayer/llm/__init__.py",
    "src/skilllayer/llm/client.py",
    "src/skilllayer/memory/__init__.py",
    "src/skilllayer/memory/interface.py",
    "src/skilllayer/telemetry.py",
    "src/skilllayer/mcp_smoke_test.py",
}

PRIVATE_EXACT = {
    "src/skilllayer/analytics.py",
    "src/skilllayer/benchmark.py",
    "src/skilllayer/cost_optimization.py",
    "src/skilllayer/failure_analysis.py",
    "src/skilllayer/generalization.py",
    "src/skilllayer/open_source_audit.py",
    "src/skilllayer/packaging_audit.py",
    "src/skilllayer/workflow_repair.py",
}

PRIVATE_PREFIXES = (
    "src/a",
    "src/r",
    "src/s",
    "src/deprecated/",
)


def run_open_source_audit() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = list_repo_files()
    secret_scan = scan_sensitive_data(files)
    classification = classify_files(files, secret_scan)
    public_manifest = build_public_manifest(classification)
    private_manifest = build_private_manifest(classification)
    gitignore = build_gitignore_proposal()
    license_recommendation = build_license_recommendation()
    risk_report = build_risk_report(secret_scan, classification)
    report = build_summary_report(
        secret_scan=secret_scan,
        classification=classification,
        public_manifest=public_manifest,
        private_manifest=private_manifest,
        risk_report=risk_report,
        license_recommendation=license_recommendation,
    )

    write_json(OUTPUT_DIR / "secret_scan.json", secret_scan)
    write_json(OUTPUT_DIR / "file_classification.json", classification)
    write_json(OUTPUT_DIR / "public_manifest_proposal.json", public_manifest)
    write_json(OUTPUT_DIR / "private_manifest_proposal.json", private_manifest)
    (OUTPUT_DIR / "gitignore_proposal.txt").write_text(gitignore, encoding="utf-8")
    write_json(OUTPUT_DIR / "license_recommendation.json", license_recommendation)
    write_json(OUTPUT_DIR / "risk_report.json", risk_report)
    write_json(OUTPUT_DIR / "report.json", report)
    return report


def list_repo_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        rel = relative_path(path)
        if rel.startswith(f"{OUTPUT_DIR.as_posix()}/"):
            continue
        parts = set(Path(rel).parts)
        if parts & SKIP_DIR_NAMES:
            continue
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: relative_path(item))


def scan_sensitive_data(files: list[Path]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    counts_by_pattern: Counter[str] = Counter()
    counts_by_severity: Counter[str] = Counter()
    skipped_large_files: list[str] = []
    skipped_binary_files = 0
    per_file_pattern_counts: defaultdict[tuple[str, str], int] = defaultdict(int)

    for path in files:
        rel = relative_path(path)
        for pattern, pattern_type, severity in SENSITIVE_PATH_PATTERNS:
            if pattern.search(rel):
                finding = make_finding(
                    file_path=rel,
                    line_number=None,
                    pattern_type=pattern_type,
                    severity=severity,
                    excerpt=f"path contains {pattern_type}",
                )
                append_finding(findings, finding, counts_by_pattern, counts_by_severity)

        if not is_probably_text(path):
            skipped_binary_files += 1
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped_large_files.append(rel)
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern_type, regex, severity in SECRET_PATTERNS:
                if not regex.search(line):
                    continue
                severity, context = adjust_finding_context(rel, line, pattern_type, severity)
                key = (rel, pattern_type)
                if per_file_pattern_counts[key] >= MAX_FINDINGS_PER_FILE_PATTERN:
                    continue
                per_file_pattern_counts[key] += 1
                finding = make_finding(
                    file_path=rel,
                    line_number=line_number,
                    pattern_type=pattern_type,
                    severity=severity,
                    excerpt=line,
                    context=context,
                )
                append_finding(findings, finding, counts_by_pattern, counts_by_severity)
                if len(findings) >= MAX_FINDINGS:
                    return build_secret_scan_result(
                        findings,
                        counts_by_pattern,
                        counts_by_severity,
                        skipped_large_files,
                        skipped_binary_files,
                        truncated=True,
                    )

    return build_secret_scan_result(
        findings,
        counts_by_pattern,
        counts_by_severity,
        skipped_large_files,
        skipped_binary_files,
        truncated=False,
    )


def append_finding(
    findings: list[dict[str, Any]],
    finding: dict[str, Any],
    counts_by_pattern: Counter[str],
    counts_by_severity: Counter[str],
) -> None:
    findings.append(finding)
    counts_by_pattern[finding["pattern_type"]] += 1
    counts_by_severity[finding["severity"]] += 1


def make_finding(
    file_path: str,
    line_number: int | None,
    pattern_type: str,
    severity: str,
    excerpt: str,
    context: str = "content",
) -> dict[str, Any]:
    return {
        "file_path": file_path,
        "line_number": line_number,
        "pattern_type": pattern_type,
        "severity": severity,
        "context": context,
        "redacted_excerpt": redact_excerpt(excerpt),
    }


def adjust_finding_context(rel: str, line: str, pattern_type: str, severity: str) -> tuple[str, str]:
    if rel == "src/skilllayer/open_source_audit.py" and (
        pattern_type in line or "SECRET_PATTERNS" in line or "SENSITIVE_PATH_PATTERNS" in line
    ):
        return "low", "audit_detector_definition"
    return severity, "content"


def build_secret_scan_result(
    findings: list[dict[str, Any]],
    counts_by_pattern: Counter[str],
    counts_by_severity: Counter[str],
    skipped_large_files: list[str],
    skipped_binary_files: int,
    truncated: bool,
) -> dict[str, Any]:
    severity_order = ["critical", "high", "medium", "low"]
    return {
        "scan_root": str(PROJECT_ROOT),
        "patterns_checked": [name for name, _, _ in SECRET_PATTERNS],
        "finding_count": len(findings),
        "counts_by_pattern": dict(sorted(counts_by_pattern.items())),
        "counts_by_severity": {severity: counts_by_severity.get(severity, 0) for severity in severity_order},
        "truncated": truncated,
        "max_findings": MAX_FINDINGS,
        "max_findings_per_file_pattern": MAX_FINDINGS_PER_FILE_PATTERN,
        "skipped_large_files": skipped_large_files[:200],
        "skipped_large_file_count": len(skipped_large_files),
        "skipped_binary_file_count": skipped_binary_files,
        "findings": findings,
        "redaction_applied": True,
        "advisory_only": True,
    }


def classify_files(files: list[Path], secret_scan: dict[str, Any]) -> dict[str, Any]:
    severity_by_path: defaultdict[str, set[str]] = defaultdict(set)
    pattern_by_path: defaultdict[str, set[str]] = defaultdict(set)
    for finding in secret_scan["findings"]:
        severity_by_path[finding["file_path"]].add(finding["severity"])
        pattern_by_path[finding["file_path"]].add(finding["pattern_type"])

    records: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for path in files:
        rel = relative_path(path)
        category, reasons = classify_path(rel, severity_by_path[rel], pattern_by_path[rel])
        record = {
            "file_path": rel,
            "category": category,
            "reasons": reasons,
            "secret_scan_severities": sorted(severity_by_path[rel]),
            "secret_scan_patterns": sorted(pattern_by_path[rel]),
        }
        records.append(record)
        counts[category] += 1

    category_order = [
        "public_safe",
        "public_review_needed",
        "private_keep",
        "exclude_generated",
        "exclude_sensitive",
    ]
    return {
        "categories": category_order,
        "counts": {category: counts.get(category, 0) for category in category_order},
        "records": records,
        "conservative_default": "private_keep",
        "advisory_only": True,
    }


def classify_path(rel: str, severities: set[str], patterns: set[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if is_generated_path(rel):
        return "exclude_generated", ["generated artifact, cache, benchmark output, or local build artifact"]
    if is_sensitive_path(rel) or "critical" in severities or "high" in severities and any(
        pattern in patterns for pattern in ["openai_key_like_value", "github_token_like_value", "github_pat_like_value", "private_key_reference"]
    ):
        return "exclude_sensitive", ["sensitive path or critical/high secret-like finding"]

    if rel in PUBLIC_SAFE_EXACT:
        reasons.append("part of public CLI/MCP/docs/stable core")
        category = "public_safe"
    elif rel in PUBLIC_REVIEW_EXACT:
        reasons.append("needed for public runtime but should be reviewed before release")
        category = "public_review_needed"
    elif rel in PRIVATE_EXACT or rel.startswith(PRIVATE_PREFIXES):
        reasons.append("research, analytics, optimization, failure analysis, or private/pro component")
        category = "private_keep"
    elif rel.startswith("src/skilllayer/"):
        reasons.append("SkillLayer package file not explicitly whitelisted")
        category = "public_review_needed"
    elif rel.startswith("src/"):
        reasons.append("research or experiment code outside the public SkillLayer package")
        category = "private_keep"
    elif rel.startswith("REPORT_"):
        reasons.append("research milestone report with benchmark or experimental claims")
        category = "private_keep"
    elif rel.endswith((".md", ".txt", ".toml", ".yaml", ".yml", ".json")):
        reasons.append("top-level text artifact requiring manual publication review")
        category = "public_review_needed"
    else:
        reasons.append("not needed for public core release")
        category = "private_keep"

    if severities:
        reasons.append(f"secret scan findings: {', '.join(sorted(severities))}")
        if category == "public_safe":
            category = "public_review_needed"
    if patterns & {"nikolai_local_path", "absolute_local_path", "personal_name", "internal_project_name", "email_address"}:
        reasons.append("contains local path, identity, or internal-name signal")
        if category == "public_safe":
            category = "public_review_needed"
    return category, reasons


def build_public_manifest(classification: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for record in classification["records"]:
        rel = record["file_path"]
        category = record["category"]
        if category == "public_safe":
            entries.append(make_manifest_entry(record, review_required=False))
        elif category == "public_review_needed" and should_include_in_public_review(rel):
            entries.append(make_manifest_entry(record, review_required=True))
    return {
        "proposed_repo": "skilllayer-community/",
        "proposal_only": True,
        "copy_performed": False,
        "include_count": len(entries),
        "entries": entries,
        "notes": [
            "Only public_safe files and explicitly required public_review_needed files are proposed.",
            "Review-required files should be manually scrubbed before publication.",
            "Experimental/pro analytics and generated runs are intentionally excluded.",
        ],
    }


def build_private_manifest(classification: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for record in classification["records"]:
        if record["category"] in {"private_keep", "public_review_needed"} and not should_include_in_public_review(record["file_path"]):
            entries.append(make_manifest_entry(record, review_required=False))
        elif record["category"] == "exclude_generated" and record["file_path"].startswith("runs/"):
            entries.append(
                {
                    "source_path": record["file_path"],
                    "target_path": f"skilllayer-private/{record['file_path']}",
                    "category": "generated_optional_private_archive",
                    "review_required": True,
                    "reason": "generated experiment/benchmark artifact; keep out of public repo and archive privately only if needed",
                }
            )
    return {
        "proposed_repo": "skilllayer-private/",
        "proposal_only": True,
        "copy_performed": False,
        "include_count": len(entries),
        "entries": entries,
        "notes": [
            "Private manifest includes research, analytics, ROI, optimization, failure-analysis, and generated artifacts if retention is needed.",
            "Generated artifacts should still be reviewed before private archival if they may contain local paths or sensitive data.",
        ],
    }


def build_gitignore_proposal() -> str:
    return "\n".join(
        [
            "# SkillLayer local/generated artifacts",
            "runs/",
            ".venv/",
            "__pycache__/",
            "**/__pycache__/",
            ".pytest_cache/",
            ".ruff_cache/",
            "*.pyc",
            "",
            "# Secrets and local credentials",
            ".env",
            ".env.*",
            "credentials*",
            "*credentials*",
            "*secret*",
            "*token*",
            "",
            "# Browser smoke artifacts",
            "browser_screenshots/",
            "*screenshot*.png",
            "runs/browser_smoke/",
            "",
            "# Benchmark/report outputs",
            "runs/**/*benchmark*.json",
            "runs/**/*results*.json",
            "runs/**/*summary*.json",
            "runs/**/*report*.json",
            "",
            "# Local MCP configs with absolute paths",
            "codex_mcp_config.local.json",
            "*mcp*local*.json",
            "",
        ]
    )


def build_license_recommendation() -> dict[str, Any]:
    return {
        "advisory_only": True,
        "license_not_applied": True,
        "community_repo": {
            "recommended_default": "Apache-2.0",
            "rationale": (
                "Apache-2.0 is friendly to community use while including an explicit patent grant. "
                "MIT is also viable if maximum permissiveness is preferred."
            ),
            "alternatives": ["MIT"],
        },
        "private_pro_repo": {
            "recommended_default": "keep proprietary/private",
            "source_available_options_if_needed": ["Business Source License", "PolyForm Noncommercial", "source-available custom license"],
            "rationale": (
                "Advanced analytics, optimization experiments, benchmarks, and future learning systems are intended "
                "as private/pro components, so they should not inherit the community license by accident."
            ),
        },
        "legal_review_required": True,
    }


def build_risk_report(secret_scan: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    findings = secret_scan["findings"]
    critical = [item for item in findings if item["severity"] == "critical"]
    high = [item for item in findings if item["severity"] == "high"]
    local_path = [item for item in findings if item["pattern_type"] in {"nikolai_local_path", "absolute_local_path"}]
    generated_count = classification["counts"].get("exclude_generated", 0)
    private_count = classification["counts"].get("private_keep", 0)
    high_risk_files = sorted({item["file_path"] for item in critical + high})[:100]
    critical_blockers = []
    if critical:
        critical_blockers.append("critical secret-like findings require manual review before any public release")
    if generated_count:
        critical_blockers.append("generated runs/benchmark artifacts must be excluded from any public repository")
    if local_path:
        critical_blockers.append("local absolute path and identity leakage must be scrubbed from public files")

    top_risks = [
        {
            "risk": "generated_artifact_leakage",
            "severity": "high",
            "evidence": f"{generated_count} files classified as exclude_generated",
        },
        {
            "risk": "secret_or_token_like_strings",
            "severity": "high" if critical or high else "medium",
            "evidence": f"{len(critical)} critical and {len(high)} high suspicious findings",
        },
        {
            "risk": "local_path_or_identity_leakage",
            "severity": "medium",
            "evidence": f"{len(local_path)} local-path findings",
        },
        {
            "risk": "benchmark_overclaim",
            "severity": "medium",
            "evidence": "README and milestone reports contain many benchmark interpretations that are not public-product claims",
        },
        {
            "risk": "ip_leakage",
            "severity": "medium",
            "evidence": f"{private_count} files classified as private_keep",
        },
    ]
    return {
        "safe_to_publish_now": False,
        "advisory_only": True,
        "critical_blockers": critical_blockers,
        "high_risk_files": high_risk_files,
        "local_path_leakage": {
            "finding_count": len(local_path),
            "sample_files": sorted({item["file_path"] for item in local_path})[:30],
        },
        "generated_artifact_leakage": {
            "file_count": generated_count,
            "recommendation": "exclude runs/, caches, screenshots, checkpoints, and benchmark outputs from public release",
        },
        "benchmark_overclaim_risk": {
            "risk_level": "medium",
            "recommendation": "keep benchmark reports private or rewrite public docs to clearly label proxy metrics and limitations",
        },
        "ip_leakage_risk": {
            "risk_level": "medium",
            "recommendation": "do not publish research branches, analytics internals, optimization scripts, or future learning/distillation code",
        },
        "top_5_risks": top_risks,
        "recommended_next_actions": [
            "Do not publish the current repository as-is.",
            "Create a fresh public repository from public_manifest_proposal.json only after manual review.",
            "Exclude runs/, research scripts, analytics, ROI, optimization, failure-analysis, and generated reports.",
            "Scrub local paths, personal identifiers, internal project names, and secret-like strings from public-review files.",
            "Get legal/security review before applying a license or publishing.",
        ],
    }


def build_summary_report(
    secret_scan: dict[str, Any],
    classification: dict[str, Any],
    public_manifest: dict[str, Any],
    private_manifest: dict[str, Any],
    risk_report: dict[str, Any],
    license_recommendation: dict[str, Any],
) -> dict[str, Any]:
    counts = classification["counts"]
    must_clean = [
        "exclude generated artifacts and caches",
        "review or scrub secret-like findings",
        "remove local absolute paths and identity/internal project references from public files",
        "keep private/pro analytics, ROI, optimization, failure analysis, and research artifacts out of community repo",
        "rewrite benchmark claims for public docs if any are kept",
    ]
    return {
        "milestone": "Open Source Readiness Audit",
        "safe_to_publish_now": False,
        "advisory_only": True,
        "no_files_deleted": True,
        "no_files_moved": True,
        "no_repo_published": True,
        "secret_scan": {
            "finding_count": secret_scan["finding_count"],
            "counts_by_severity": secret_scan["counts_by_severity"],
            "truncated": secret_scan["truncated"],
            "redaction_applied": secret_scan["redaction_applied"],
        },
        "file_classification_counts": counts,
        "public_repo_safe_files": [
            item["source_path"] for item in public_manifest["entries"] if not item["review_required"]
        ],
        "public_repo_review_required_files": [
            item["source_path"] for item in public_manifest["entries"] if item["review_required"]
        ],
        "private_repo_file_count": private_manifest["include_count"],
        "must_be_removed_or_cleaned_first": must_clean,
        "top_5_risks": risk_report["top_5_risks"],
        "recommended_publication_strategy": {
            "strategy": "partial public release via fresh curated repo",
            "community_repo": "skilllayer-community with docs, CLI, MCP server, stable workflows, examples, and MCP config template",
            "private_repo": "skilllayer-private with analytics, ROI, benchmarks, workflow optimization, failure analysis, research artifacts, and learning systems",
            "license_recommendation": license_recommendation,
        },
        "answers": {
            "is_repo_safe_to_publish_now": False,
            "what_must_be_removed_or_cleaned_first": must_clean,
            "which_files_are_safe_for_public_repo": "See public_manifest_proposal.json public_safe entries.",
            "which_files_should_stay_private": "See private_manifest_proposal.json and file_classification.json private_keep entries.",
            "top_5_risks": risk_report["top_5_risks"],
            "recommended_publication_strategy": "Do not publish this repo directly; export a curated community repo from the proposed manifest after manual review.",
        },
    }


def make_manifest_entry(record: dict[str, Any], review_required: bool) -> dict[str, Any]:
    rel = record["file_path"]
    return {
        "source_path": rel,
        "target_path": f"skilllayer-community/{rel}",
        "category": record["category"],
        "review_required": review_required,
        "reason": "; ".join(record["reasons"]),
    }


def should_include_in_public_review(rel: str) -> bool:
    return rel in PUBLIC_REVIEW_EXACT or rel in {
        "README.md",
        "QUICKSTART.md",
        "skilllayer.yaml.example",
        "codex_mcp_config.example.json",
    }


def is_generated_path(rel: str) -> bool:
    parts = Path(rel).parts
    if not parts:
        return False
    return (
        parts[0] == "runs"
        or parts[0] in {".venv", ".pytest_cache", ".ruff_cache"}
        or "__pycache__" in parts
        or rel.endswith((".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pt", ".pth", ".onnx", ".DS_Store"))
    )


def is_sensitive_path(rel: str) -> bool:
    return any(pattern.search(rel) for pattern, _, _ in SENSITIVE_PATH_PATTERNS)


def is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    if path.name in {".gitignore", "Dockerfile", "Makefile"}:
        return True
    return False


def redact_excerpt(text: str) -> str:
    excerpt = text.strip()
    excerpt = re.sub(r"sk-[A-Za-z0-9_-]{8,}", lambda match: redact_token(match.group(0)), excerpt)
    excerpt = re.sub(r"ghp_[A-Za-z0-9_]{8,}", lambda match: redact_token(match.group(0)), excerpt)
    excerpt = re.sub(r"github_pat_[A-Za-z0-9_]{8,}", lambda match: redact_token(match.group(0)), excerpt)
    excerpt = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}", r"\1[REDACTED]", excerpt)
    excerpt = re.sub(r"/Users/[^/\s\"'`),\]]+/[^\s\"'`),\]]+", "/Users/[REDACTED]/[PATH]", excerpt)
    excerpt = re.sub(r"(?<![A-Za-z0-9_])/(Users|home|private/var|var/folders)/[^\s\"'`),\]]+", r"/\1/[REDACTED_PATH]", excerpt)
    excerpt = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", excerpt)
    if len(excerpt) > 220:
        excerpt = excerpt[:217] + "..."
    return excerpt


def redact_token(token: str) -> str:
    if len(token) <= 10:
        return "[REDACTED]"
    return f"{token[:4]}...[REDACTED]...{token[-4:]}"


def relative_path(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(make_json_safe(payload), indent=2), encoding="utf-8")


def make_json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [make_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [make_json_safe(item) for item in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value
