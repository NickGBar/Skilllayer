"""Review packet: a local Markdown summary for the person who reviews a change set.

It states what changed (from git) and which checks ran (from a gate receipt that matches the
change), and never more than that: no merge decision, no claim about correctness. Without a
matching receipt it says the checks are not confirmed. A verify receipt is shown as context
only, because it cannot prove which working tree was checked.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .gate import check_receipt

_CHECK_NAME = re.compile(r"[A-Za-z0-9_:-]{1,80}")
_CHECK_STATUSES = {"passed", "failed", "not_run", "not_verified"}
_VERIFY_VERDICTS = {"VERIFIED", "TESTS_FAILING", "POLICY_VIOLATION", "TESTS_WEAKENED",
                    "UNVERIFIED_NO_TESTS", "UNVERIFIED_ENVIRONMENT", "UNVERIFIED_TIMEOUT", "UNVERIFIED_UNKNOWN"}
_VERIFY_PROBLEMS = {"TESTS_FAILING", "POLICY_VIOLATION", "TESTS_WEAKENED"}

_RU = {
    "title": "# Пакет для ревью",
    "changed": "## Что изменилось",
    "files": "- Изменённые файлы (статус Git):",
    "no_changes": "  - Нет изменений.",
    "meaning": "Смысл изменений и бизнес-корректность не определялись; для подробностей смотрите diff.",
    "checks": "## Какие проверки выполнены",
    "not_gate": "Файл не является Gate receipt.",
    "bad_digest": "Контрольная сумма Gate receipt неверна или отсутствует.",
    "unsigned": "Gate receipt не подписан, хотя ключ проверки задан.",
    "bad_signature": "Подпись Gate receipt не совпадает с ключом.",
    "other_change": "Gate receipt относится к другому изменению.",
    "unreadable": "Не удалось прочитать или проверить Gate receipt.",
    "no_receipt": "Gate receipt не предоставлен.",
    "digest_only": "- Gate receipt `{name}`: контрольная сумма совпадает; подлинность источника и подпись не подтверждены.",
    "signed": "- Gate receipt `{name}`: контрольная сумма и подпись совпадают.",
    "no_checks": "- Gate не содержит результатов отдельных проверок; их выполнение не подтверждено.",
    "unknown_check": "- Неизвестная проверка: inconclusive.",
    "unknown_name": "неизвестная проверка",
    "details": "- Подробности и команды: смотрите исходный Gate receipt; проверьте его перед передачей.",
    "unconfirmed": "- {why} Результаты проверок не подтверждены.",
    "verify_said": "- Verify: receipt сообщает `{verdict}`. Его подлинность и соответствие проверенного рабочего дерева текущему коммиту не подтверждены; учитывать только как контекст.",
    "verify_bad": "- Verify: receipt не прочитан или имеет неизвестный формат; результат не подтверждён.",
    "verify_none": "- Verify: данные не предоставлены; отдельная проверка не подтверждена.",
    "problems": "## Обнаруженные нарушения",
    "failed_count": "- Gate зафиксировал неуспешных проверок: {count}. Подробности — в receipt.",
    "none_failed": "- Неуспешные проверки в переданном receipt не указаны. Это не доказывает отсутствие дефектов.",
    "unknown_problems": "- Неизвестно: проверенный Gate receipt отсутствует.",
    "verify_problem": "- Verify сообщает `{verdict}`; соответствие этому коммиту не установлено.",
    "human": "## Человеку проверить",
    "todo": [
        "- Сопоставить diff с задачей и проверить бизнес-логику.",
        "- Убедиться, что необходимые проверки действительно выполнены; изучить неуспешные и непроверенные.",
        "- Просмотреть пакет и receipt перед передачей: имена файлов и другие данные могут быть чувствительными.",
        "- Самостоятельно принять решение об остаточном риске и merge.",
    ],
}

_EN = {
    "title": "# Review packet",
    "changed": "## What changed",
    "files": "- Changed files (git status):",
    "no_changes": "  - No changes.",
    "meaning": "The meaning of the change and its business correctness were not assessed; see the diff for details.",
    "checks": "## Which checks ran",
    "not_gate": "The file is not a gate receipt.",
    "bad_digest": "The gate receipt's digest is wrong or missing.",
    "unsigned": "The gate receipt is not signed, although a verification key is set.",
    "bad_signature": "The gate receipt's signature does not match the key.",
    "other_change": "The gate receipt belongs to a different change.",
    "unreadable": "The gate receipt could not be read or checked.",
    "no_receipt": "No gate receipt was given.",
    "digest_only": "- Gate receipt `{name}`: the digest matches; its origin and signature are not confirmed.",
    "signed": "- Gate receipt `{name}`: the digest and the signature match.",
    "no_checks": "- The gate receipt holds no individual check results; that they ran is not confirmed.",
    "unknown_check": "- Unknown check: inconclusive.",
    "unknown_name": "unknown check",
    "details": "- Details and commands: see the gate receipt itself; review it before sharing.",
    "unconfirmed": "- {why} Check results are not confirmed.",
    "verify_said": "- Verify: the receipt reports `{verdict}`. Its origin, and that the checked working tree matches this commit, are not confirmed; treat it as context only.",
    "verify_bad": "- Verify: the receipt could not be read or has an unknown format; no result is confirmed.",
    "verify_none": "- Verify: no data given; no separate check is confirmed.",
    "problems": "## Problems found",
    "failed_count": "- The gate recorded failed checks: {count}. Details are in the receipt.",
    "none_failed": "- The given receipt lists no failed checks. That does not prove the absence of defects.",
    "unknown_problems": "- Unknown: there is no checked gate receipt.",
    "verify_problem": "- Verify reports `{verdict}`; that it applies to this commit is not established.",
    "human": "## For a person to check",
    "todo": [
        "- Compare the diff with the task and check the business logic.",
        "- Make sure the required checks really ran; look into failed and unverified ones.",
        "- Review the packet and the receipt before sharing: file names and other data may be sensitive.",
        "- Decide on the remaining risk and on merging yourself.",
    ],
}

_CHECK_REASONS = {
    "no_integrity_block": "bad_digest",
    "digest_mismatch": "bad_digest",
    "unsigned_receipt": "unsigned",
    "signature_mismatch": "bad_signature",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _code(text: str) -> str:
    return text.replace("`", "'")


def _load_gate(path: Path, head_sha: str, merge_base: str, key: bytes | None) -> tuple[dict[str, Any] | None, str | None, bool]:
    """(receipt, reason it was refused, signature confirmed)."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("kind") != "skilllayer.gate":
            return None, "not_gate", False
        checked = check_receipt(loaded, key=key)
        if not checked.get("valid"):
            return None, _CHECK_REASONS.get(checked.get("reason"), "bad_digest"), False
        if (loaded.get("head") or {}).get("sha") != head_sha or loaded.get("merge_base") != merge_base:
            return None, "other_change", False
        return loaded, None, checked.get("reason") == "signature_ok"
    except (OSError, ValueError, TypeError, AttributeError):
        return None, "unreadable", False


def make_packet(repo: Path, base: str, head: str, receipt_path: Path | None,
                verify_path: Path | None = None, *, lang: str = "ru", key: bytes | None = None) -> str:
    text = _RU if lang == "ru" else _EN
    base_sha = _git(repo, "rev-parse", "--verify", base + "^{commit}")
    head_sha = _git(repo, "rev-parse", "--verify", head + "^{commit}")
    merge_base = _git(repo, "merge-base", base_sha, head_sha)
    names = _git(repo, "diff", "--name-status", "--no-renames", merge_base, head_sha)
    lines = [text["title"], "", text["changed"], "",
             f"- Base: `{base_sha}`", f"- Head: `{head_sha}`", f"- Merge base: `{merge_base}`",
             text["files"], ""]
    lines += [f"  - `{_code(line)}`" for line in names.splitlines()] if names else [text["no_changes"]]
    lines += ["", text["meaning"], "", text["checks"], ""]

    receipt, refused, signed = (None, "no_receipt", False)
    if receipt_path:
        receipt, refused, signed = _load_gate(receipt_path, head_sha, merge_base, key)
    if receipt:
        name = _code(receipt_path.name)
        lines.append(text["signed" if signed else "digest_only"].format(name=name))
        checks = receipt.get("checks")
        if not isinstance(checks, list) or not checks:
            lines.append(text["no_checks"])
        else:
            for check in checks:
                if not isinstance(check, dict):
                    lines.append(text["unknown_check"])
                    continue
                raw = check.get("name", "")
                check_name = raw if isinstance(raw, str) and _CHECK_NAME.fullmatch(raw) else text["unknown_name"]
                status = check.get("status")
                status = status if status in _CHECK_STATUSES else "inconclusive"
                lines.append(f"- `{check_name}`: **{status}**.")
        lines.append(text["details"])
    else:
        lines.append(text["unconfirmed"].format(why=text[refused]))

    verify_verdict = None
    if verify_path:
        try:
            verify = json.loads(verify_path.read_text(encoding="utf-8"))
            verdict = verify.get("verdict") if isinstance(verify, dict) and verify.get("kind") == "skilllayer.verify" else None
            if verdict not in _VERIFY_VERDICTS:
                raise ValueError("invalid verify receipt")
            verify_verdict = verdict
            lines.append(text["verify_said"].format(verdict=verdict))
        except (OSError, ValueError, TypeError):
            lines.append(text["verify_bad"])
    else:
        lines.append(text["verify_none"])

    lines += ["", text["problems"], ""]
    if receipt:
        failed = [c for c in (receipt.get("checks") or []) if isinstance(c, dict) and c.get("status") == "failed"]
        lines.append(text["failed_count"].format(count=len(failed)) if failed else text["none_failed"])
    else:
        lines.append(text["unknown_problems"])
    if verify_verdict in _VERIFY_PROBLEMS:
        lines.append(text["verify_problem"].format(verdict=verify_verdict))
    lines += ["", text["human"], "", *text["todo"], ""]
    return "\n".join(lines)
