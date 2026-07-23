"""Verified Task Execution — foundation persistence layer.

Storage root: <project_root>/.skilllayer/tasks/<task-id>/
  contract.json    — write-once
  baseline.json    — write-once factual repository evidence
  scope_amendments.json — append-only approved scope additions
  result.json      — latest-wins (overwrite)
  checkpoint.json  — latest-wins, checkpoint_version strictly increasing

This module implements ONLY the foundation described in
docs/VERIFIED_TASK_EXECUTION_DESIGN.md's MVP boundary: deterministic task IDs,
bounded task-directory creation, a redaction/rejection gate every persisted
value passes through, atomic writes (reusing the existing memory subsystem's
primitives), a write-once contract, and safe recovery on read. Foundation B
adds consent-gated baseline/evidence storage; the pure scope comparison lives
in ``tasks.scope``. It still does not implement source edits, routing, an MCP
surface, cross-session handoff, summaries, or automatic rollback — see
docs/VTE_PERSISTENCE_SECURITY.md for the full boundary and known limitations.

Not registered as a public workflow or MCP tool. Callers within the
package/tests import this module directly.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..memory.skilllayer_memory import MemoryLockTimeoutError, atomic_write_json, memory_lock

CURRENT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Persistence-layer result contract
#
# Distinct from memory/skilllayer_memory.py's healthy/warning/broken/error
# vocabulary — this is the consent/write state machine the milestone asked
# for. READ_COMPLETED is an addition beyond the required set (CONSENT_
# REQUIRED/CONSENT_INVALID/WRITE_COMPLETED/WRITE_REJECTED/RECORD_ALREADY_
# EXISTS/RECORD_NOT_FOUND/PERSISTENCE_BLOCKED): reads are never consent-gated
# (they cannot mutate anything), so they need their own success state rather
# than borrowing "WRITE_COMPLETED" for an operation that wrote nothing.
# ---------------------------------------------------------------------------

PERSISTENCE_STATES = frozenset({
    "CONSENT_REQUIRED",
    "CONSENT_INVALID",
    "WRITE_COMPLETED",
    "WRITE_REJECTED",
    "RECORD_ALREADY_EXISTS",
    "RECORD_NOT_FOUND",
    "PERSISTENCE_BLOCKED",
    "READ_COMPLETED",
})
_SUCCESS_STATES = frozenset({"WRITE_COMPLETED", "READ_COMPLETED"})


def _result(
    state: str,
    *,
    task_id: str | None,
    planned_paths: list[str] | None = None,
    written_paths: list[str] | None = None,
    error: str | None = None,
    summary: str = "",
    **extra: Any,
) -> dict[str, Any]:
    assert state in PERSISTENCE_STATES, f"unknown persistence state: {state}"
    return {
        "state": state,
        "success": state in _SUCCESS_STATES,
        "task_id": task_id,
        "planned_paths": list(planned_paths or []),
        "written_paths": list(written_paths or []),
        "error": error,
        "summary": summary,
        **extra,
    }


# ---------------------------------------------------------------------------
# Task ID — generation and validation
#
# Shape: <UTC timestamp>-<sanitized objective slug>-<short random suffix>
# e.g. 20260723T143005Z-fix-login-timeout-a1b2c3d4
# ---------------------------------------------------------------------------

_TASK_ID_CORE = r"\d{8}T\d{6}Z-[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?-[a-f0-9]{6,10}"
_TASK_ID_RE = re.compile(rf"^{_TASK_ID_CORE}$")
_TASK_ID_MAX_LENGTH = 140


class TaskIdError(ValueError):
    """Raised when a task_id fails structural validation.

    Deliberately never raised for "this task_id refers to a secret" — that
    concern is handled at generation time (see _sanitize_objective_hint_to_slug)
    by never letting sensitive text reach the id in the first place, not by
    rejecting it after the fact once it may already be a directory name.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_task_id(task_id: str) -> None:
    """Raise TaskIdError if task_id is not safe to use as a directory name."""
    if not isinstance(task_id, str) or not task_id or len(task_id) > _TASK_ID_MAX_LENGTH:
        raise TaskIdError("task_id_invalid_type_or_length")
    if "\x00" in task_id or "/" in task_id or "\\" in task_id or ".." in task_id:
        raise TaskIdError("task_id_contains_unsafe_characters")
    if not _TASK_ID_RE.fullmatch(task_id):
        raise TaskIdError("task_id_shape_invalid")


def is_valid_task_id(task_id: str) -> bool:
    try:
        validate_task_id(task_id)
    except TaskIdError:
        return False
    return True


def _sanitize_objective_hint_to_slug(objective_hint: str | None) -> str:
    """Turn free text into a bounded, filesystem-safe slug.

    If the raw hint matches ANY known high-confidence secret pattern, the
    hint is discarded entirely in favor of the generic "task" slug — the
    slug becomes a permanent directory name, so a partial secret surviving
    lowercasing/charset-stripping is not an acceptable risk to take.
    """
    if not objective_hint:
        return "task"
    for _, pattern in _REJECT_SECRET_PATTERNS:
        if pattern.search(objective_hint):
            return "task"
    lowered = objective_hint.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = slug[:40].strip("-")
    return slug or "task"


def generate_task_id(project_root: Path, objective_hint: str | None = None, *, max_attempts: int = 5) -> str:
    """Generate a collision-checked task_id under <project_root>/.skilllayer/tasks/.

    Never silently reuses an existing task directory: each candidate is
    checked against the filesystem before being returned, retrying with a
    fresh random suffix on collision.
    """
    slug = _sanitize_objective_hint_to_slug(objective_hint)
    tasks_root = project_root / ".skilllayer" / "tasks"
    for _ in range(max_attempts):
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = secrets.token_hex(4)
        candidate = f"{timestamp}-{slug}-{suffix}"
        if not (tasks_root / candidate).exists():
            return candidate
    raise TaskIdError("task_id_generation_exhausted_attempts")


# ---------------------------------------------------------------------------
# Path confinement and symlink safety
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRecordPaths:
    tasks_root: Path
    task_dir: Path
    contract_path: Path
    baseline_path: Path
    amendments_path: Path
    result_path: Path
    checkpoint_path: Path


class TaskPathSafetyError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def task_record_paths(project_root: Path, task_id: str) -> TaskRecordPaths:
    """Compute the confined, symlink-checked paths for one task record.

    Read-only: performs stat/lstat checks only, never creates anything.
    Raises TaskPathSafetyError (never returns an unsafe path) when the
    task_id is invalid, an ancestor is a symlink, or the resolved directory
    would escape <project_root>/.skilllayer/tasks/.
    """
    try:
        validate_task_id(task_id)
    except TaskIdError as exc:
        raise TaskPathSafetyError(exc.reason) from exc

    root = project_root.resolve()
    skilllayer_dir = root / ".skilllayer"
    tasks_root = skilllayer_dir / "tasks"
    task_dir = tasks_root / task_id

    # Never follow a symlink at any ancestor level under the project — a
    # pre-planted symlink at .skilllayer, .skilllayer/tasks, or the task
    # directory itself must never be traversed into.
    for ancestor in (skilllayer_dir, tasks_root, task_dir):
        if ancestor.is_symlink():
            raise TaskPathSafetyError("symlink_escape")

    try:
        task_dir.resolve().relative_to(tasks_root.resolve())
    except ValueError as exc:
        raise TaskPathSafetyError("path_escape") from exc

    return TaskRecordPaths(
        tasks_root=tasks_root,
        task_dir=task_dir,
        contract_path=task_dir / "contract.json",
        baseline_path=task_dir / "baseline.json",
        amendments_path=task_dir / "scope_amendments.json",
        result_path=task_dir / "result.json",
        checkpoint_path=task_dir / "checkpoint.json",
    )


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


# ---------------------------------------------------------------------------
# Redaction / rejection gate
#
# Three field classes (finalized in docs/VERIFIED_TASK_EXECUTION_DECISIONS.md):
#   SAFE_STRUCTURED     — machine-controlled shape (paths, enums, hex hashes).
#                         Validated by exact shape/allowlist; a known secret
#                         pattern embedded inside is rejected, never redacted
#                         in place, because redacting part of a structured
#                         value would silently corrupt its meaning.
#   REDACTABLE_TEXT     — short human-authored labels. Known high-confidence
#                         secret patterns reject the whole field; diagnostic
#                         PII (home paths, emails) is redacted in place;
#                         anything else that still looks high-entropy after
#                         redaction is rejected as low-confidence-sensitive.
#   FORBIDDEN_FREE_TEXT — arbitrary prose is never accepted, unconditionally.
# ---------------------------------------------------------------------------


class FieldPolicy(Enum):
    SAFE_STRUCTURED = "safe_structured"
    REDACTABLE_TEXT = "redactable_text"
    FORBIDDEN_FREE_TEXT = "forbidden_free_text"


@dataclass(frozen=True)
class SanitizationResult:
    accepted: bool
    sanitized_value: Any
    redactions_applied: tuple[str, ...]
    rejection_reason: str | None
    confidence: str  # "high" | "medium" | "low"


# Known, high-confidence sensitive value shapes. A match anywhere rejects the
# whole field — these are never redacted in place, only ever rejected,
# because a partial credential surviving redaction is still a credential.
_REJECT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9\-_]{20,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt_like", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{20,}")),
    ("database_url_with_credentials",
     re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s\"'/]+:[^\s\"'@]+@[^\s\"']+")),
    ("password_bearing_url", re.compile(r"(?i)://[^\s\"'/:@]+:[^\s\"'@]{3,}@")),
    ("slack_webhook_url", re.compile(r"(?i)https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("generic_webhook_url", re.compile(r"(?i)https://[a-z0-9.\-]*webhook[a-z0-9./\-]*")),
    ("private_git_ssh_remote", re.compile(r"(?i)\bgit@[a-z0-9.\-]+:[^\s]+\.git\b")),
    ("credential_bearing_git_remote", re.compile(r"(?i)https?://[^/\s]+:[^/\s]+@[a-z0-9.\-]+/[^\s]+\.git")),
    ("dotenv_style_assignment",
     re.compile(r"(?im)\b[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PWD|CREDENTIAL)[A-Z0-9_]*\s*=\s*\S+")),
    ("credential_cli_flag", re.compile(r"(?i)--(?:password|token|secret|api-key|apikey)[= ]\S+")),
)

# Lower-severity diagnostic PII: redacted in place (replaced with a fixed
# token naming the pattern), not rejected — matches design §13's rule that a
# retained diagnostic snippet passes through redaction rather than blocking
# the whole field outright.
_REDACT_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("home_path_unix", re.compile(r"/Users/[^/\s\"']+|/home/[^/\s\"']+")),
    ("home_path_windows", re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+")),
    ("email_address", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
)

# A long, unbroken alnum/dash/underscore run mixing letters and digits with
# no whitespace does not occur in ordinary prose; treated as low-confidence
# sensitive and rejected per "any low-confidence sensitive field must be
# rejected" rather than guessed at.
_HIGH_ENTROPY_RE = re.compile(r"(?<![A-Za-z0-9_\-])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_\-])")

# Recognizable, structurally-verifiable identifier shapes that are exempt from
# the generic entropy heuristic above — git SHAs, hash digests, UUIDs, this
# system's own task_id, and package-manager integrity hashes are long,
# mixed-alnum, and non-secret BY CONVENTION (they exist to be shared, e.g. in
# a "HEAD advanced to <sha>" freshness message). This does not weaken secret
# detection: every _REJECT_SECRET_PATTERNS check above already ran and found
# nothing, so this exemption only ever narrows the low-confidence FALLBACK,
# never the specific high-confidence patterns. See VTE_PERSISTENCE_SECURITY.md
# "known limitations" for the accepted tradeoff (a raw secret rendered as hex
# or as a UUID is indistinguishable from a legitimate hash/id by shape alone).
_SAFE_ENTROPY_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[0-9a-fA-F]{7,128}\b"),  # git short/full SHA-1, sha256/384/512 hex digests
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),  # UUID
    re.compile(r"\b(?:sha1|sha256|sha384|sha512)[:\-][A-Za-z0-9+/]{20,}={0,2}"),  # pip/npm integrity hash
    re.compile(_TASK_ID_CORE),
)


def _looks_high_entropy(token: str) -> bool:
    return any(c.isdigit() for c in token) and any(c.isalpha() for c in token)


def _covered_by_safe_entropy_shape(text: str, start: int, end: int) -> bool:
    """True if [start, end) falls entirely within a recognized safe shape
    match somewhere in text — checked against the whole text (not just the
    token) so a compound shape like an npm 'sha512-<base64>==' integrity hash,
    which the entropy regex itself only partially matches (it stops at '+'/'='
    characters outside its charset), is still recognized as one safe unit."""
    return any(
        m.start() <= start and end <= m.end()
        for pattern in _SAFE_ENTROPY_SHAPE_PATTERNS
        for m in pattern.finditer(text)
    )


def _redact_token(pattern_name: str) -> str:
    return f"«redacted:{pattern_name}»"


def _scan_text(text: str) -> tuple[str | None, tuple[str, ...], str | None, str]:
    """Return (sanitized_text_or_None, redactions_applied, rejection_reason, confidence).

    sanitized_text is None whenever rejection_reason is set — the caller must
    never persist or echo back the original text on rejection (Phase 8: a
    rejected value's raw content must not surface even in diagnostics)."""
    for name, pattern in _REJECT_SECRET_PATTERNS:
        if pattern.search(text):
            return None, (), f"matched_known_secret_pattern:{name}", "high"

    redacted = text
    redactions: list[str] = []
    for name, pattern in _REDACT_PII_PATTERNS:
        if pattern.search(redacted):
            redacted = pattern.sub(_redact_token(name), redacted)
            redactions.append(name)

    for match in _HIGH_ENTROPY_RE.finditer(redacted):
        if _looks_high_entropy(match.group(0)) and not _covered_by_safe_entropy_shape(
            redacted, match.start(), match.end()
        ):
            return None, (), "low_confidence_sensitive_token", "low"

    confidence = "medium" if redactions else "high"
    return redacted, tuple(redactions), None, confidence


_MAX_CONTAINER_DEPTH = 6
_MAX_LIST_LENGTH = 200
_MAX_DICT_KEYS = 100
_DICT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")


def sanitize_persisted_value(
    value: Any,
    policy: FieldPolicy,
    *,
    max_length: int = 200,
    shape_pattern: re.Pattern[str] | None = None,
    allowed_values: frozenset[str] | None = None,
    field_name: str = "value",
    _depth: int = 0,
) -> SanitizationResult:
    """Central gate every value must pass before it may be persisted.

    Recurses through lists/dicts (depth-capped) applying the same leaf
    policy to every string; fails closed — if any leaf anywhere rejects, the
    whole (possibly nested) value is rejected with sanitized_value=None,
    never a partially-sanitized structure."""
    if _depth > _MAX_CONTAINER_DEPTH:
        return SanitizationResult(False, None, (), f"{field_name}:max_depth_exceeded", "low")

    if value is None:
        return SanitizationResult(True, None, (), None, "high")
    if isinstance(value, bool):
        return SanitizationResult(True, value, (), None, "high")
    if isinstance(value, int):
        return SanitizationResult(True, value, (), None, "high")

    if isinstance(value, list):
        if len(value) > _MAX_LIST_LENGTH:
            return SanitizationResult(False, None, (), f"{field_name}:list_too_long", "low")
        sanitized_items: list[Any] = []
        all_redactions: list[str] = []
        for i, item in enumerate(value):
            sub = sanitize_persisted_value(
                item, policy, max_length=max_length, shape_pattern=shape_pattern,
                allowed_values=allowed_values, field_name=f"{field_name}[{i}]", _depth=_depth + 1,
            )
            if not sub.accepted:
                return SanitizationResult(False, None, (), sub.rejection_reason, sub.confidence)
            sanitized_items.append(sub.sanitized_value)
            all_redactions.extend(sub.redactions_applied)
        return SanitizationResult(True, sanitized_items, tuple(all_redactions), None, "medium" if all_redactions else "high")

    if isinstance(value, dict):
        if len(value) > _MAX_DICT_KEYS:
            return SanitizationResult(False, None, (), f"{field_name}:dict_too_large", "low")
        sanitized_map: dict[str, Any] = {}
        all_redactions = []
        for k, v in value.items():
            if not isinstance(k, str) or not _DICT_KEY_RE.fullmatch(k):
                return SanitizationResult(False, None, (), f"{field_name}:invalid_key", "low")
            sub = sanitize_persisted_value(
                v, policy, max_length=max_length, shape_pattern=shape_pattern,
                allowed_values=allowed_values, field_name=f"{field_name}.{k}", _depth=_depth + 1,
            )
            if not sub.accepted:
                return SanitizationResult(False, None, (), sub.rejection_reason, sub.confidence)
            sanitized_map[k] = sub.sanitized_value
            all_redactions.extend(sub.redactions_applied)
        return SanitizationResult(True, sanitized_map, tuple(all_redactions), None, "medium" if all_redactions else "high")

    if not isinstance(value, str):
        return SanitizationResult(False, None, (), f"{field_name}:unsupported_type:{type(value).__name__}", "low")

    if policy is FieldPolicy.FORBIDDEN_FREE_TEXT:
        return SanitizationResult(False, None, (), f"{field_name}:free_text_not_permitted", "high")

    if len(value) > max_length:
        return SanitizationResult(False, None, (), f"{field_name}:value_too_long", "low")

    if not value:
        return SanitizationResult(True, "", (), None, "high")

    if any((ord(c) < 0x20 and c != "\t") for c in value):
        return SanitizationResult(False, None, (), f"{field_name}:control_characters_present", "low")

    if policy is FieldPolicy.SAFE_STRUCTURED:
        if allowed_values is not None:
            if value not in allowed_values:
                return SanitizationResult(False, None, (), f"{field_name}:value_not_allowlisted", "low")
        elif shape_pattern is not None and not shape_pattern.fullmatch(value):
            return SanitizationResult(False, None, (), f"{field_name}:shape_mismatch", "low")
        # Defense in depth: a shape-conforming structured value must still
        # not embed a known high-confidence secret pattern. Rejected outright
        # (never redacted) because redacting inside a structured value (a
        # path, an enum) would silently corrupt its meaning.
        for name, pattern in _REJECT_SECRET_PATTERNS:
            if pattern.search(value):
                return SanitizationResult(False, None, (), f"{field_name}:matched_known_secret_pattern:{name}", "high")
        return SanitizationResult(True, value, (), None, "high")

    # REDACTABLE_TEXT
    sanitized_text, redactions, rejection_reason, confidence = _scan_text(value)
    if rejection_reason is not None:
        return SanitizationResult(False, None, (), f"{field_name}:{rejection_reason}", confidence)
    return SanitizationResult(True, sanitized_text, redactions, None, confidence)


def _contains_rejected_secret(value: Any) -> bool:
    """Defence-in-depth for generated structured evidence.

    Baselines and scope results are generated by internal code, but their paths
    ultimately come from a user repository.  They must receive the same
    high-confidence secret rejection guarantee as hand-supplied contract data.
    """
    try:
        serialized = json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return True
    return any(pattern.search(serialized) for _, pattern in _REJECT_SECRET_PATTERNS)


# ---------------------------------------------------------------------------
# Field shapes for the minimal schemas
# ---------------------------------------------------------------------------

_GIT_HEAD_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REPO_RELATIVE_PATTERN_RE = re.compile(r"(?!.*\.\.)[A-Za-z0-9_.\-][A-Za-z0-9_.\-/*]{0,199}")
_CHECK_LABEL_RE = re.compile(r"[A-Za-z0-9_.:/\-]{1,100}")
_COMPONENT_LABEL_RE = re.compile(r"[a-z][a-z0-9_]{2,60}")
_VERDICT_RE = re.compile(r"[A-Z][A-Z0-9_]{2,40}")
_REQUESTED_BY_RE = re.compile(r"[a-z][a-z0-9_]{1,39}")

TASK_STATUSES = frozenset({
    "ACTIVE", "WAITING_FOR_HOST_AGENT", "VALIDATING", "VALIDATED",
    "INCOMPLETE", "BLOCKED", "FAILED", "STALE", "CANCELLED", "ARCHIVED",
})
CONSTRAINT_KINDS = frozenset({
    "no_new_dependencies", "no_public_api_change", "no_config_changes", "custom",
})


def _sanitize_constraints(constraints: list[dict[str, str]]) -> SanitizationResult:
    if not isinstance(constraints, list):
        return SanitizationResult(False, None, (), "constraints:not_a_list", "low")
    if len(constraints) > 20:
        return SanitizationResult(False, None, (), "constraints:too_many", "low")
    sanitized: list[dict[str, str]] = []
    redactions: list[str] = []
    for i, item in enumerate(constraints):
        if not isinstance(item, dict) or set(item.keys()) - {"kind", "detail_label"}:
            return SanitizationResult(False, None, (), f"constraints[{i}]:unexpected_shape", "low")
        kind = item.get("kind")
        if kind not in CONSTRAINT_KINDS:
            return SanitizationResult(False, None, (), f"constraints[{i}]:kind_not_allowlisted", "low")
        detail = sanitize_persisted_value(
            item.get("detail_label", ""), FieldPolicy.REDACTABLE_TEXT, max_length=120,
            field_name=f"constraints[{i}].detail_label",
        )
        if not detail.accepted:
            return SanitizationResult(False, None, (), detail.rejection_reason, detail.confidence)
        sanitized.append({"kind": kind, "detail_label": detail.sanitized_value})
        redactions.extend(detail.redactions_applied)
    return SanitizationResult(True, sanitized, tuple(redactions), None, "medium" if redactions else "high")


def _root_fingerprint(project_root: Path) -> str:
    """Non-reversible identity for a project root — never the path itself."""
    digest = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def _git_head(project_root: Path) -> str | None:
    """Best-effort HEAD SHA; None on any failure (not a git repo, no commits,
    git unavailable). Never raises — this is advisory identity, not a gate."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root), capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.strip()
    return head if _GIT_HEAD_RE.fullmatch(head) else None


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------

DEFAULT_DECLARED_RECORD_TYPES = frozenset({"contract", "baseline", "amendment", "result", "checkpoint"})


@dataclass(frozen=True)
class TaskConsent:
    granted: bool
    granted_at: str
    task_id: str
    project_root_fingerprint: str
    declared_record_types: frozenset[str]


def grant_task_consent(
    project_root: Path,
    task_id: str,
    declared_record_types: frozenset[str] | set[str] = DEFAULT_DECLARED_RECORD_TYPES,
    *,
    granted_at: str | None = None,
) -> TaskConsent:
    """Construct a consent object scoped to exactly one task_id + project.

    Changing either requires a fresh call — _check_consent verifies both the
    task_id and project_root_fingerprint match before authorizing any write."""
    return TaskConsent(
        granted=True,
        granted_at=granted_at or _utc_iso(),
        task_id=task_id,
        project_root_fingerprint=_root_fingerprint(project_root),
        declared_record_types=frozenset(declared_record_types),
    )


def _check_consent(
    consent: TaskConsent | None, *, task_id: str, project_root: Path, record_type: str,
) -> str | None:
    """Return None if consent authorizes this write; otherwise a state name."""
    if consent is None:
        return "CONSENT_REQUIRED"
    if not isinstance(consent, TaskConsent) or not consent.granted or not consent.granted_at:
        return "CONSENT_INVALID"
    if consent.task_id != task_id:
        return "CONSENT_INVALID"
    if consent.project_root_fingerprint != _root_fingerprint(project_root):
        return "CONSENT_INVALID"
    if record_type not in consent.declared_record_types:
        return "CONSENT_INVALID"
    return None


# ---------------------------------------------------------------------------
# Reads — never consent-gated (read-only, cannot mutate); safe on malformed state
# ---------------------------------------------------------------------------


def _read_task_record(path: Path, task_id: str, record_type: str) -> dict[str, Any]:
    """Shared recovery-safe reader for contract/result/checkpoint.

    Never rewrites what it finds, however malformed. A symlink at the record
    path is refused, not followed. Unsupported schema_version or a task_id
    mismatch between the record's own field and the requested task_id both
    block rather than guess."""
    if path.is_symlink():
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, error="symlink_not_permitted",
                        summary=f"Refusing to read {record_type}.json: path is a symlink.")
    if not path.exists():
        return _result("RECORD_NOT_FOUND", task_id=task_id,
                        summary=f"No {record_type}.json found for task {task_id}.")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, error="io_error", summary=str(exc))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, error="malformed_json",
                        summary=f"{record_type}.json exists but is not valid JSON.")
    if not isinstance(data, dict):
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, error="malformed_json",
                        summary=f"{record_type}.json does not contain a JSON object.")
    if data.get("schema_version") != CURRENT_SCHEMA_VERSION:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, error="unsupported_schema_version",
                        summary=f"{record_type}.json has schema_version={data.get('schema_version')!r}, "
                                f"not {CURRENT_SCHEMA_VERSION}.")
    if data.get("task_id") != task_id:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, error="task_id_mismatch",
                        summary=f"{record_type}.json's task_id does not match {task_id}.")
    out = _result("READ_COMPLETED", task_id=task_id, summary=f"{record_type}.json read successfully.")
    out["record"] = data
    return out


def _resolve_paths_or_blocked(project_root: Path, task_id: str) -> tuple[TaskRecordPaths | None, dict[str, Any] | None]:
    try:
        validate_task_id(task_id)
    except TaskIdError as exc:
        return None, _result("PERSISTENCE_BLOCKED", task_id=task_id, error=exc.reason,
                              summary="task_id is not safe to use as a directory name.")
    try:
        paths = task_record_paths(project_root, task_id)
    except TaskPathSafetyError as exc:
        return None, _result("PERSISTENCE_BLOCKED", task_id=task_id, error=exc.reason,
                              summary="The task path failed a safety check.")
    return paths, None


def read_task_contract(project_root: Path, task_id: str) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    return _read_task_record(paths.contract_path, task_id, "contract")


def read_task_baseline(project_root: Path, task_id: str) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    return _read_task_record(paths.baseline_path, task_id, "baseline")


def read_scope_amendments(project_root: Path, task_id: str) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    return _read_task_record(paths.amendments_path, task_id, "scope_amendments")


def read_task_result(project_root: Path, task_id: str) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    return _read_task_record(paths.result_path, task_id, "result")


def read_task_checkpoint(project_root: Path, task_id: str) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    return _read_task_record(paths.checkpoint_path, task_id, "checkpoint")


# ---------------------------------------------------------------------------
# create_task_contract — write-once
# ---------------------------------------------------------------------------


def create_task_contract(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    objective_label: str,
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
    expected_checks: list[str] | None = None,
    constraints: list[dict[str, str]] | None = None,
    scope_mode: str = "EXPLICIT",
    max_changed_files: int | None = None,
    allow_new_files: bool = True,
    allow_deleted_files: bool = True,
    allow_test_files: bool = True,
    allowed_generated_paths: list[str] | None = None,
    requested_by: str = "user",
) -> dict[str, Any]:
    """Create contract.json. Write-once: a second call for an existing
    task_id returns RECORD_ALREADY_EXISTS and leaves the original untouched.

    Order of operations matters for the consent boundary: task_id shape and
    path/symlink safety are pure read-only checks and run first (so planned
    paths can be reported even when blocked); consent is checked before any
    directory, lock, or temp file is created; field sanitization runs before
    the lock is acquired, so a rejected write touches no filesystem state at
    all — only a fully-validated write ever enters the locked section."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked

    root = project_root.resolve()
    planned = [_rel(p, root) for p in (paths.contract_path, paths.baseline_path, paths.amendments_path, paths.result_path, paths.checkpoint_path)]

    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="contract")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned,
                        summary="No contract created: valid task-lifecycle consent is required first.")

    if paths.contract_path.exists() or paths.contract_path.is_symlink():
        return _result("RECORD_ALREADY_EXISTS", task_id=task_id, planned_paths=planned,
                        summary=f"contract.json already exists for task {task_id}; it was left unchanged.")

    san_objective = sanitize_persisted_value(
        objective_label, FieldPolicy.REDACTABLE_TEXT, max_length=80, field_name="objective_label")
    # Scope paths need Foundation B's deterministic semantics (and stricter
    # protected-path rules) rather than the earlier generic path regex.
    from .scope import validate_scope_contract
    scope_candidate = {
        "task_id": task_id, "allowed_paths": allowed_paths or [], "forbidden_paths": forbidden_paths or [],
        "scope_mode": scope_mode, "max_changed_files": max_changed_files,
        "allow_new_files": allow_new_files, "allow_deleted_files": allow_deleted_files,
        "allow_test_files": allow_test_files, "allowed_generated_paths": allowed_generated_paths or [],
    }
    scope_checked = validate_scope_contract(scope_candidate, task_id=task_id)
    if not scope_checked["valid"]:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error=scope_checked["error_code"], summary="No contract created: scope declaration is invalid.")
    san_allowed = sanitize_persisted_value(
        scope_checked["normalized"]["allowed_paths"], FieldPolicy.SAFE_STRUCTURED,
        shape_pattern=_REPO_RELATIVE_PATTERN_RE, field_name="allowed_paths")
    san_forbidden = sanitize_persisted_value(
        scope_checked["normalized"]["forbidden_paths"], FieldPolicy.SAFE_STRUCTURED,
        shape_pattern=_REPO_RELATIVE_PATTERN_RE, field_name="forbidden_paths")
    san_generated = sanitize_persisted_value(
        scope_checked["normalized"]["allowed_generated_paths"], FieldPolicy.SAFE_STRUCTURED,
        shape_pattern=_REPO_RELATIVE_PATTERN_RE, field_name="allowed_generated_paths")
    san_checks = sanitize_persisted_value(
        expected_checks or [], FieldPolicy.SAFE_STRUCTURED, shape_pattern=_CHECK_LABEL_RE, field_name="expected_checks")
    san_constraints = _sanitize_constraints(constraints or [])
    san_requested_by = sanitize_persisted_value(
        requested_by, FieldPolicy.SAFE_STRUCTURED, shape_pattern=_REQUESTED_BY_RE, field_name="requested_by")

    rejections = [r for r in (san_objective, san_allowed, san_forbidden, san_generated, san_checks, san_constraints, san_requested_by)
                  if not r.accepted]
    if rejections:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error=rejections[0].rejection_reason,
                        summary="No contract created: a field failed the persistence redaction/rejection gate.")

    baseline_head = _git_head(project_root)
    contract = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "task_id": task_id,
        "created_at": _utc_iso(),
        "objective_label": san_objective.sanitized_value,
        "repository_identity": {
            "path_fingerprint": _root_fingerprint(project_root),
            "is_git_repo": baseline_head is not None,
        },
        "baseline_git_head": baseline_head,
        "allowed_paths": san_allowed.sanitized_value,
        "forbidden_paths": san_forbidden.sanitized_value,
        "scope_mode": scope_checked["normalized"]["scope_mode"],
        "max_changed_files": scope_checked["normalized"]["max_changed_files"],
        "allow_new_files": scope_checked["normalized"]["allow_new_files"],
        "allow_deleted_files": scope_checked["normalized"]["allow_deleted_files"],
        "allow_test_files": scope_checked["normalized"]["allow_test_files"],
        "allowed_generated_paths": san_generated.sanitized_value,
        "scope_amendments": [],
        "expected_checks": san_checks.sanitized_value,
        "constraints": san_constraints.sanitized_value,
        "requested_by": san_requested_by.sanitized_value,
        "consent": {
            "type": "task_lifecycle",
            "granted_at": consent.granted_at,
            "declared_paths": planned,
        },
        "status": "ACTIVE",
    }

    skilllayer_dir = root / ".skilllayer"
    try:
        with memory_lock(skilllayer_dir):
            if paths.contract_path.exists() or paths.contract_path.is_symlink():
                return _result("RECORD_ALREADY_EXISTS", task_id=task_id, planned_paths=planned,
                                summary=f"contract.json already exists for task {task_id}; it was left unchanged.")
            atomic_write_json(paths.contract_path, contract)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                        error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                        error="io_error", summary=str(exc))

    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned,
                    written_paths=[_rel(paths.contract_path, root)],
                    summary=f"Task contract created for {task_id}.")


# ---------------------------------------------------------------------------
# write_task_baseline — write-once factual evidence
# ---------------------------------------------------------------------------


def write_task_baseline(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Persist a validated Foundation B baseline inside the approved task dir.

    The caller may always capture evidence in memory.  This writer is the
    explicit consent-gated boundary for the optional ``baseline.json`` record.
    """
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    root = project_root.resolve()
    planned = [_rel(paths.baseline_path, root)]
    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="baseline")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned,
                        summary="No baseline written: valid task-lifecycle consent for 'baseline' is required.")
    from .baseline import baseline_is_persistable
    if not baseline_is_persistable(baseline) or baseline.get("task_id") != task_id or _contains_rejected_secret(baseline):
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error="baseline_schema_invalid", summary="No baseline written: baseline evidence has an invalid schema.")
    contract_read = _read_task_record(paths.contract_path, task_id, "contract")
    if not contract_read["success"]:
        return contract_read
    if paths.baseline_path.exists() or paths.baseline_path.is_symlink():
        return _result("RECORD_ALREADY_EXISTS", task_id=task_id, planned_paths=planned,
                        summary="baseline.json already exists and was left unchanged.")
    try:
        with memory_lock(root / ".skilllayer"):
            if paths.baseline_path.exists() or paths.baseline_path.is_symlink():
                return _result("RECORD_ALREADY_EXISTS", task_id=task_id, planned_paths=planned,
                                summary="baseline.json already exists and was left unchanged.")
            atomic_write_json(paths.baseline_path, baseline)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="io_error", summary=str(exc))
    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned, written_paths=[_rel(paths.baseline_path, root)],
                   summary=f"baseline.json written for task {task_id}.")


def append_scope_amendment(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    amendment: dict[str, Any],
) -> dict[str, Any]:
    """Append an approved bounded scope addition without rewriting contract.json."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked
    root = project_root.resolve()
    planned = [_rel(paths.amendments_path, root)]
    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="amendment")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned,
                        summary="No amendment written: explicit task-lifecycle amendment consent is required.")
    contract_read = _read_task_record(paths.contract_path, task_id, "contract")
    if not contract_read["success"]:
        return contract_read
    if not isinstance(amendment, dict):
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error="amendment_not_object", summary="No amendment written.")
    required = {"amendment_id", "approved_at", "added_allowed_paths", "added_generated_paths", "reason_label", "consent_reference"}
    if set(amendment) != required or not all(isinstance(amendment.get(key), str) and amendment[key] for key in ("amendment_id", "approved_at", "reason_label", "consent_reference")):
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error="amendment_schema_invalid", summary="No amendment written.")
    san_amendment_id = sanitize_persisted_value(amendment["amendment_id"], FieldPolicy.SAFE_STRUCTURED, shape_pattern=_CHECK_LABEL_RE, field_name="amendment_id")
    san_approved_at = sanitize_persisted_value(amendment["approved_at"], FieldPolicy.SAFE_STRUCTURED, shape_pattern=re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"), field_name="approved_at")
    san_reason = sanitize_persisted_value(amendment["reason_label"], FieldPolicy.REDACTABLE_TEXT, max_length=120, field_name="reason_label")
    san_consent = sanitize_persisted_value(amendment["consent_reference"], FieldPolicy.SAFE_STRUCTURED, shape_pattern=_CHECK_LABEL_RE, field_name="consent_reference")
    if not all(item.accepted for item in (san_amendment_id, san_approved_at, san_reason, san_consent)):
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error="amendment_sensitive_or_invalid", summary="No amendment written.")
    amendment = {
        **amendment,
        "amendment_id": san_amendment_id.sanitized_value,
        "approved_at": san_approved_at.sanitized_value,
        "reason_label": san_reason.sanitized_value,
        "consent_reference": san_consent.sanitized_value,
    }
    from .scope import apply_scope_amendments
    prior = _read_task_record(paths.amendments_path, task_id, "scope_amendments")
    if prior["state"] == "RECORD_NOT_FOUND":
        records: list[dict[str, Any]] = []
    elif prior["success"]:
        records = prior["record"].get("amendments", [])
        if not isinstance(records, list):
            return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="amendments_malformed", summary="No amendment written.")
    else:
        return prior
    if any(item.get("amendment_id") == amendment["amendment_id"] for item in records if isinstance(item, dict)):
        return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned, written_paths=[], summary="Scope amendment already present; no duplicate written.")
    checked = apply_scope_amendments(contract_read["record"], records + [amendment])
    if not checked["valid"]:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned, error=checked["errors"][0], summary="No amendment written: it would broaden scope unsafely.")
    record = {"schema_version": CURRENT_SCHEMA_VERSION, "task_id": task_id, "amendments": records + [amendment]}
    try:
        with memory_lock(root / ".skilllayer"):
            if paths.amendments_path.is_symlink():
                return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="symlink_not_permitted", summary="scope_amendments.json path is a symlink.")
            atomic_write_json(paths.amendments_path, record)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned, error="io_error", summary=str(exc))
    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned, written_paths=[_rel(paths.amendments_path, root)], summary="Approved scope amendment recorded.")


# ---------------------------------------------------------------------------
# write_task_result — latest-wins, atomic
# ---------------------------------------------------------------------------


def write_task_result(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    verdict: str,
    verdict_reasons: list[str] | None = None,
    written_by_component: str,
    attempt: int | None = None,
    scope_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write result.json (placeholder evidence shape — real execution
    evidence integration is deferred to a later VTE milestone)."""
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked

    root = project_root.resolve()
    planned = [_rel(paths.result_path, root)]

    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="result")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned,
                        summary="No result written: valid task-lifecycle consent for 'result' is required.")

    contract_read = _read_task_record(paths.contract_path, task_id, "contract")
    if contract_read["state"] == "RECORD_NOT_FOUND":
        return _result("RECORD_NOT_FOUND", task_id=task_id, planned_paths=planned,
                        summary="No contract exists for this task; create one before writing a result.")
    if not contract_read["success"]:
        return contract_read

    san_verdict = sanitize_persisted_value(
        verdict, FieldPolicy.SAFE_STRUCTURED, shape_pattern=_VERDICT_RE, field_name="verdict")
    san_reasons = sanitize_persisted_value(
        verdict_reasons or [], FieldPolicy.REDACTABLE_TEXT, max_length=200, field_name="verdict_reasons")
    san_component = sanitize_persisted_value(
        written_by_component, FieldPolicy.SAFE_STRUCTURED, shape_pattern=_COMPONENT_LABEL_RE,
        field_name="written_by_component")

    rejections = [r for r in (san_verdict, san_reasons, san_component) if not r.accepted]
    if rejections:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error=rejections[0].rejection_reason,
                        summary="No result written: a field failed the persistence redaction/rejection gate.")
    if scope_validation is not None:
        from .scope import scope_validation_is_persistable
        if not scope_validation_is_persistable(scope_validation) or scope_validation.get("task_id") != task_id or _contains_rejected_secret(scope_validation):
            return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                            error="scope_validation_schema_invalid", summary="No result written: scope validation evidence is invalid.")

    if attempt is None:
        prior = _read_task_record(paths.result_path, task_id, "result")
        if prior["state"] == "RECORD_NOT_FOUND":
            attempt = 1
        elif prior["success"]:
            attempt = int(prior["record"].get("attempt", 0)) + 1
        else:
            return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                            error=prior.get("error") or "existing_result_unreadable",
                            summary="No result written: existing result.json could not be safely read; "
                                    "refusing to overwrite blindly. Pass attempt= explicitly to override.")
    if not isinstance(attempt, int) or attempt < 1:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error="attempt_must_be_positive_int",
                        summary="No result written: attempt must be a positive integer.")

    record = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "task_id": task_id,
        "attempt": attempt,
        "observed_at": _utc_iso(),
        "verdict": san_verdict.sanitized_value,
        "verdict_reasons": san_reasons.sanitized_value,
        "written_by_component": san_component.sanitized_value,
    }
    if scope_validation is not None:
        record["scope_validation"] = scope_validation

    skilllayer_dir = root / ".skilllayer"
    try:
        with memory_lock(skilllayer_dir):
            if paths.result_path.is_symlink():
                return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                                error="symlink_not_permitted", summary="result.json path is a symlink.")
            atomic_write_json(paths.result_path, record)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                        error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                        error="io_error", summary=str(exc))

    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned,
                    written_paths=[_rel(paths.result_path, root)],
                    summary=f"result.json written for task {task_id}, attempt {attempt}.")


# ---------------------------------------------------------------------------
# write_task_checkpoint — latest-wins, checkpoint_version strictly increasing
# ---------------------------------------------------------------------------


def write_task_checkpoint(
    project_root: Path,
    task_id: str,
    *,
    consent: TaskConsent | None,
    checkpoint_version: int,
    status: str,
    exact_next_action_label: str,
) -> dict[str, Any]:
    paths, blocked = _resolve_paths_or_blocked(project_root, task_id)
    if blocked is not None:
        return blocked

    root = project_root.resolve()
    planned = [_rel(paths.checkpoint_path, root)]

    consent_state = _check_consent(consent, task_id=task_id, project_root=project_root, record_type="checkpoint")
    if consent_state is not None:
        return _result(consent_state, task_id=task_id, planned_paths=planned,
                        summary="No checkpoint written: valid task-lifecycle consent for 'checkpoint' is required.")

    contract_read = _read_task_record(paths.contract_path, task_id, "contract")
    if contract_read["state"] == "RECORD_NOT_FOUND":
        return _result("RECORD_NOT_FOUND", task_id=task_id, planned_paths=planned,
                        summary="No contract exists for this task; create one before writing a checkpoint.")
    if not contract_read["success"]:
        return contract_read

    if not isinstance(checkpoint_version, int) or checkpoint_version < 1:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error="checkpoint_version_must_be_positive_int",
                        summary="No checkpoint written: checkpoint_version must be a positive integer.")

    prior = _read_task_record(paths.checkpoint_path, task_id, "checkpoint")
    if prior["state"] != "RECORD_NOT_FOUND":
        if not prior["success"]:
            return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                            error=prior.get("error") or "existing_checkpoint_unreadable",
                            summary="No checkpoint written: existing checkpoint.json could not be safely read; "
                                    "refusing to overwrite blindly.")
        prior_version = prior["record"].get("checkpoint_version", 0)
        if checkpoint_version <= prior_version:
            return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                            error="checkpoint_version_not_monotonic",
                            summary=f"No checkpoint written: version {checkpoint_version} is not greater "
                                    f"than the existing {prior_version}.")

    san_status = sanitize_persisted_value(status, FieldPolicy.SAFE_STRUCTURED, allowed_values=TASK_STATUSES, field_name="status")
    san_next_action = sanitize_persisted_value(
        exact_next_action_label, FieldPolicy.REDACTABLE_TEXT, max_length=160, field_name="exact_next_action_label")

    rejections = [r for r in (san_status, san_next_action) if not r.accepted]
    if rejections:
        return _result("WRITE_REJECTED", task_id=task_id, planned_paths=planned,
                        error=rejections[0].rejection_reason,
                        summary="No checkpoint written: a field failed the persistence redaction/rejection gate.")

    record = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "task_id": task_id,
        "checkpoint_version": checkpoint_version,
        "created_at": _utc_iso(),
        "repository_git_head": _git_head(project_root),
        "status": san_status.sanitized_value,
        "exact_next_action_label": san_next_action.sanitized_value,
    }

    skilllayer_dir = root / ".skilllayer"
    try:
        with memory_lock(skilllayer_dir):
            if paths.checkpoint_path.is_symlink():
                return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                                error="symlink_not_permitted", summary="checkpoint.json path is a symlink.")
            atomic_write_json(paths.checkpoint_path, record)
    except MemoryLockTimeoutError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                        error="memory_lock_timeout", summary=str(exc))
    except OSError as exc:
        return _result("PERSISTENCE_BLOCKED", task_id=task_id, planned_paths=planned,
                        error="io_error", summary=str(exc))

    return _result("WRITE_COMPLETED", task_id=task_id, planned_paths=planned,
                    written_paths=[_rel(paths.checkpoint_path, root)],
                    summary=f"checkpoint.json written for task {task_id} (version {checkpoint_version}).")
