#!/usr/bin/env python3
"""Render a small local review packet from Git and an optional Gate receipt."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from skilllayer.gate import check_receipt


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def make_packet(repo: Path, base: str, head: str, receipt_path: Path | None, verify_path: Path | None = None) -> str:
    base_sha = git(repo, 'rev-parse', '--verify', base + '^{commit}')
    head_sha = git(repo, 'rev-parse', '--verify', head + '^{commit}')
    merge_base = git(repo, 'merge-base', base_sha, head_sha)
    names = git(repo, 'diff', '--name-status', '--no-renames', merge_base, head_sha)
    lines = ['# Review Packet V0', '', '## Что изменилось', '',
             f'- Base: `{base_sha}`', f'- Head: `{head_sha}`', f'- Merge base: `{merge_base}`',
             '- Изменённые файлы (статус Git):', '']
    lines += [f'  - `{line.replace(chr(96), chr(39))}`' for line in names.splitlines()] if names else ['  - Нет изменений.']
    lines += ['', 'Смысл изменений и бизнес-корректность не определялись; для подробностей смотрите diff.',
              '', '## Какие проверки выполнены', '']
    receipt = None
    explanation = None
    if receipt_path:
        try:
            loaded = json.loads(receipt_path.read_text(encoding='utf-8'))
            if not isinstance(loaded, dict) or loaded.get('kind') != 'skilllayer.gate':
                explanation = 'Файл не является Gate receipt.'
            elif not check_receipt(loaded).get('valid'):
                explanation = 'Контрольная сумма Gate receipt неверна или отсутствует.'
            elif (loaded.get('head') or {}).get('sha') != head_sha or loaded.get('merge_base') != merge_base:
                explanation = 'Gate receipt относится к другому изменению.'
            else:
                receipt = loaded
        except (OSError, ValueError, TypeError, AttributeError):
            explanation = 'Не удалось прочитать или проверить Gate receipt.'
    else:
        explanation = 'Gate receipt не предоставлен.'
    if receipt:
        lines.append(f'- Gate receipt `{receipt_path.name.replace(chr(96), chr(39))}`: контрольная сумма совпадает; подлинность источника и подпись не подтверждены.')
        checks = receipt.get('checks')
        if not isinstance(checks, list) or not checks:
            lines.append('- Gate не содержит результатов отдельных проверок; их выполнение не подтверждено.')
        else:
            for check in checks:
                if not isinstance(check, dict):
                    lines.append('- Неизвестная проверка: inconclusive.')
                    continue
                raw_name = check.get('name', '')
                name = raw_name if isinstance(raw_name, str) and re.fullmatch(r'[A-Za-z0-9_:-]{1,80}', raw_name) else 'неизвестная проверка'
                status = check.get('status')
                status = status if status in {'passed', 'failed', 'not_run', 'not_verified'} else 'inconclusive'
                lines.append(f'- `{name}`: **{status}**.')
        lines.append('- Подробности и команды: смотрите исходный Gate receipt; проверьте его перед передачей.')
    else:
        lines.append(f'- {explanation} Результаты проверок не подтверждены.')
    verify_verdict = None
    if verify_path:
        try:
            verify = json.loads(verify_path.read_text(encoding='utf-8'))
            verdict = verify.get('verdict') if isinstance(verify, dict) and verify.get('kind') == 'skilllayer.verify' else None
            allowed = {'VERIFIED', 'TESTS_FAILING', 'POLICY_VIOLATION', 'TESTS_WEAKENED',
                       'UNVERIFIED_NO_TESTS', 'UNVERIFIED_ENVIRONMENT', 'UNVERIFIED_TIMEOUT', 'UNVERIFIED_UNKNOWN'}
            if verdict not in allowed:
                raise ValueError('invalid verify receipt')
            verify_verdict = verdict
            lines.append(f'- Verify: receipt сообщает `{verdict}`. Его подлинность и соответствие проверенного рабочего дерева текущему коммиту не подтверждены; учитывать только как контекст.')
        except (OSError, ValueError, TypeError):
            lines.append('- Verify: receipt не прочитан или имеет неизвестный формат; результат не подтверждён.')
    else:
        lines.append('- Verify: данные не предоставлены; отдельная проверка не подтверждена.')
    lines += ['', '## Обнаруженные нарушения', '']
    if receipt:
        failed = [c for c in (receipt.get('checks') or []) if isinstance(c, dict) and c.get('status') == 'failed']
        if failed:
            lines.append(f'- Gate зафиксировал неуспешных проверок: {len(failed)}. Подробности — в receipt.')
        else:
            lines.append('- Неуспешные проверки в переданном receipt не указаны. Это не доказывает отсутствие дефектов.')
    else:
        lines.append('- Неизвестно: проверенный Gate receipt отсутствует.')
    if verify_verdict in {'TESTS_FAILING', 'POLICY_VIOLATION', 'TESTS_WEAKENED'}:
        lines.append(f'- Verify сообщает `{verify_verdict}`; соответствие этому коммиту не установлено.')
    lines += ['', '## Человеку проверить', '',
              '- Сопоставить diff с задачей и проверить бизнес-логику.',
              '- Убедиться, что необходимые проверки действительно выполнены; изучить неуспешные и непроверенные.',
              '- Просмотреть пакет и receipt перед передачей: имена файлов и другие данные могут быть чувствительными.',
              '- Самостоятельно принять решение об остаточном риске и merge.', '']
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', default='HEAD')
    parser.add_argument('--gate-receipt', type=Path)
    parser.add_argument('--verify-receipt', type=Path)
    args = parser.parse_args()
    try:
        print(make_packet(args.repo, args.base, args.head, args.gate_receipt, args.verify_receipt), end='')
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        parser.exit(1, f'Cannot create packet: {exc}\n')


if __name__ == '__main__':
    main()
