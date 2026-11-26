"""Case log for the change gate: every check that stopped a change set is a case a person labels.

A pilot has to answer how often the gate was right, so each failed or unverified check in a
receipt becomes a case with a stable id. A person records whether stopping the change for that
reason was correct, whether it was fixed and how many minutes it took. Receipts stay exactly as
the gate sealed them; labels go to ``labels.jsonl`` next to them, one line per decision, the
latest line for a case wins. Nothing here leaves the machine.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import gate as g
from .verify import _now

LABELS_FILE = "labels.jsonl"
LABEL_VERSION = 1
VERDICTS = ("correct", "false_positive", "unsure")
STOPPING = frozenset({g.GATE_BLOCKED, g.GATE_UNVERIFIED})
MAX_NOTE_CHARS = 300
MAX_MINUTES = 24 * 60
_CASE_ID_RE = re.compile(r"c-[0-9a-f]{10}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def case_id(digest: str, check: str) -> str:
    """Stable for a given receipt and check, so a label survives re-listing."""
    return "c-" + hashlib.sha256(f"{digest}\0{check}".encode("utf-8")).hexdigest()[:10]


def load_receipts(directory: Path, *, key: bytes | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Gate receipts whose digest (and HMAC, when a key is given) checks out, oldest first,
    each receipt once; plus the names of files that are not valid gate receipts."""
    receipts: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rejected.append(path.name)
            continue
        if (not isinstance(data, dict) or data.get("kind") != "skilllayer.gate"
                or not g.check_receipt(data, key=key)["valid"]):
            rejected.append(path.name)
            continue
        receipts.setdefault(data["integrity"]["digest"], {**data, "_file": path.name})
    return sorted(receipts.values(), key=lambda r: str(r.get("created_at", ""))), rejected


def cases_of(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    """One case per check that did not pass."""
    digest = receipt["integrity"]["digest"]
    cases = []
    for check in receipt.get("checks") or []:
        if not isinstance(check, dict) or check.get("status") == g.PASSED:
            continue
        name = str(check.get("name", "unknown"))
        cases.append({
            "id": case_id(digest, name),
            "receipt": receipt["_file"],
            "receipt_digest": digest,
            "created_at": receipt.get("created_at"),
            "head": str((receipt.get("head") or {}).get("sha", ""))[:12],
            "verdict": receipt.get("verdict"),
            "mode": receipt.get("mode"),
            "check": name,
            "status": check.get("status"),
            "ai_assisted": bool(receipt.get("ai_assisted_commits")),
        })
    return cases


def read_labels(directory: Path) -> tuple[dict[str, dict[str, Any]], int]:
    """Latest label per case id, and how many lines were unreadable."""
    labels: dict[str, dict[str, Any]] = {}
    bad = 0
    try:
        lines = (directory / LABELS_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return labels, 0
    for line in lines:
        try:
            label = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if (isinstance(label, dict) and isinstance(label.get("case_id"), str)
                and _CASE_ID_RE.fullmatch(label["case_id"]) and label.get("verdict") in VERDICTS):
            labels[label["case_id"]] = label
        else:
            bad += 1
    return labels, bad


def add_label(directory: Path, case: dict[str, Any], *, verdict: str, fixed: bool | None = None,
              minutes: int | None = None, note: str | None = None) -> dict[str, Any]:
    """Append one label line. Raises ValueError for input the log would not accept."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    if minutes is not None and not (isinstance(minutes, int) and 0 <= minutes <= MAX_MINUTES):
        raise ValueError(f"minutes must be a whole number from 0 to {MAX_MINUTES}")
    if note is not None and (len(note) > MAX_NOTE_CHARS or _CONTROL_RE.search(note)):
        raise ValueError(f"note must be one line of at most {MAX_NOTE_CHARS} characters")
    label = {
        "label_version": LABEL_VERSION,
        "case_id": case["id"],
        "receipt_digest": case["receipt_digest"],
        "check": case["check"],
        "verdict": verdict,
        "fixed": fixed,
        "minutes": minutes,
        "note": note or None,
        "labeled_at": _now(),
    }
    with (directory / LABELS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(label, sort_keys=True, ensure_ascii=False) + "\n")
    return label


def summarize(receipts: list[dict[str, Any]], labels: dict[str, dict[str, Any]], *,
              rejected: int = 0, bad_labels: int = 0) -> dict[str, Any]:
    """The pilot's four numbers: what would have been stopped, why, how often wrongly, at what cost."""
    cases = [case for receipt in receipts for case in cases_of(receipt)]
    verdicts = Counter(labels[c["id"]]["verdict"] for c in cases if c["id"] in labels)
    labelled = [labels[c["id"]] for c in cases if c["id"] in labels]
    judged = verdicts["correct"] + verdicts["false_positive"]
    return {
        "runs": len(receipts),
        "runs_with_changes": sum(1 for r in receipts if r.get("verdict") != g.GATE_NO_CHANGES),
        "ai_assisted_runs": sum(1 for r in receipts if r.get("ai_assisted_commits")),
        "would_stop": sum(1 for r in receipts if r.get("verdict") in STOPPING),
        "blocked": sum(1 for r in receipts if r.get("blocked") is True),
        "reasons": dict(Counter(f"{c['check']}:{c['status']}" for c in cases).most_common()),
        "cases": len(cases),
        "labels": {**{v: verdicts[v] for v in VERDICTS}, "unlabeled": len(cases) - len(labelled)},
        "false_positive_rate": round(verdicts["false_positive"] / judged, 3) if judged else None,
        "fixed": sum(1 for label in labelled if label.get("fixed") is True),
        "review_minutes": sum(label.get("minutes") or 0 for label in labelled),
        "gate_minutes": round(sum(float(r.get("duration_ms") or 0) for r in receipts) / 60000.0, 1),
        "rejected_receipts": rejected,
        "unreadable_label_lines": bad_labels,
    }


# --------------------------------------------------------------------------- rendering

_EN = {
    "none": "No cases: no gate run in this directory stopped a change set.",
    "unlabeled": "not labelled",
    "runs": "Runs: {runs} ({runs_with_changes} with changes, {ai_assisted_runs} with AI-assisted commits)",
    "stop": "Would have stopped: {would_stop}; actually blocked: {blocked}",
    "reasons": "Reasons:",
    "labels": "Labels: correct {correct}, false positive {false_positive}, unsure {unsure}, not labelled {unlabeled}",
    "fp": "False positive share among judged cases: {rate}",
    "fp_none": "False positive share: no judged cases yet",
    "cost": "Fixed: {fixed}; reviewer minutes recorded: {review_minutes}; gate run time: {gate_minutes} min",
    "rejected": "Ignored {rejected} file(s) that are not valid gate receipts.",
    "bad": "Ignored {bad} unreadable line(s) in labels.jsonl.",
}
_RU = {
    "none": "Случаев нет: ни один прогон гейта в этой папке не остановил изменение.",
    "unlabeled": "не размечен",
    "runs": "Прогонов: {runs} (с изменениями: {runs_with_changes}, с коммитами от ИИ: {ai_assisted_runs})",
    "stop": "Остановил бы: {would_stop}; заблокировал на деле: {blocked}",
    "reasons": "Причины:",
    "labels": "Разметка: верно {correct}, ложно {false_positive}, не уверен {unsure}, не размечено {unlabeled}",
    "fp": "Доля ложных среди оценённых: {rate}",
    "fp_none": "Доля ложных: оценённых случаев пока нет",
    "cost": "Исправлено: {fixed}; минут ревьюера записано: {review_minutes}; время гейта: {gate_minutes} мин",
    "rejected": "Пропущено файлов, не являющихся квитанциями гейта: {rejected}.",
    "bad": "Пропущено нечитаемых строк в labels.jsonl: {bad}.",
}
_VERDICT_RU = {"correct": "верно", "false_positive": "ложно", "unsure": "не уверен"}


def render_cases(cases: list[dict[str, Any]], labels: dict[str, dict[str, Any]], *, lang: str = "en") -> str:
    text = _RU if lang == "ru" else _EN
    if not cases:
        return text["none"]
    lines = []
    for case in cases:
        label = labels.get(case["id"])
        state = text["unlabeled"]
        if label:
            state = _VERDICT_RU[label["verdict"]] if lang == "ru" else label["verdict"]
        lines.append(f"{case['id']}  {str(case['created_at'])[:10]}  {case['head']}  "
                     f"{case['check']}:{case['status']}  [{state}]")
    return "\n".join(lines)


def render_summary(summary: dict[str, Any], *, lang: str = "en") -> str:
    text = _RU if lang == "ru" else _EN
    lines = [text["runs"].format(**summary), text["stop"].format(**summary)]
    if summary["reasons"]:
        lines.append(text["reasons"])
        lines += [f"  {reason}: {count}" for reason, count in summary["reasons"].items()]
    lines.append(text["labels"].format(**summary["labels"]))
    rate = summary["false_positive_rate"]
    lines.append(text["fp"].format(rate=f"{rate:.0%}") if rate is not None else text["fp_none"])
    lines.append(text["cost"].format(**summary))
    if summary["rejected_receipts"]:
        lines.append(text["rejected"].format(rejected=summary["rejected_receipts"]))
    if summary["unreadable_label_lines"]:
        lines.append(text["bad"].format(bad=summary["unreadable_label_lines"]))
    return "\n".join(lines)
