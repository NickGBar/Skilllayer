import json
import subprocess

import pytest

from skilllayer import review_packet as packet
from skilllayer.cli import main
from skilllayer.gate import RECEIPT_KEY_ENV, seal_receipt


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()
    git('init', '-q')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (tmp_path / 'code.py').write_text('VALUE = 1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (tmp_path / 'code.py').write_text('VALUE = 2\n')
    git('add', '.')
    git('commit', '-qm', 'change')
    return tmp_path, base, git('rev-parse', 'HEAD')


def _gate(head, base, key=None):
    return seal_receipt({'kind': 'skilllayer.gate', 'head': {'sha': head}, 'merge_base': base,
                         'checks': [{'name': 'tests', 'status': 'passed'}]}, key=key)


def test_packet_shows_only_matching_checked_receipt(repo, tmp_path):
    path, base, head = repo
    source = tmp_path / 'receipt.json'
    source.write_text(json.dumps(_gate(head, base)))
    result = packet.make_packet(path, base, head, source)
    assert 'code.py' in result and head in result
    assert '`tests`: **passed**' in result
    assert 'подпись не подтверждены' in result
    assert 'отсутствие дефектов' in result


def test_packet_refuses_tampered_or_wrong_commit_receipt(repo, tmp_path):
    path, base, head = repo
    receipt = _gate(head, base)
    source = tmp_path / 'receipt.json'
    receipt['head']['sha'] = base
    source.write_text(json.dumps(receipt))
    result = packet.make_packet(path, base, head, source)
    assert 'контрольная сумма' in result.lower()
    assert '`tests`: **passed**' not in result
    receipt = seal_receipt(receipt)
    source.write_text(json.dumps(receipt))
    assert 'другому изменению' in packet.make_packet(path, base, head, source)


def test_packet_without_receipt_is_explicitly_unverified(repo):
    path, base, head = repo
    result = packet.make_packet(path, base, head, None)
    assert 'Результаты проверок не подтверждены' in result
    assert 'Verify: данные не предоставлены' in result


def test_verify_receipt_is_context_not_proof_for_commit(repo, tmp_path):
    path, base, head = repo
    source = tmp_path / 'verify.json'
    source.write_text(json.dumps({'kind': 'skilllayer.verify', 'verdict': 'VERIFIED'}))
    result = packet.make_packet(path, base, head, None, source)
    assert 'receipt сообщает `VERIFIED`' in result
    assert 'не подтверждены; учитывать только как контекст' in result
    source.write_text(json.dumps({'kind': 'skilllayer.verify', 'verdict': 'TESTS_FAILING'}))
    assert 'Verify сообщает `TESTS_FAILING`' in packet.make_packet(path, base, head, None, source)


def test_signature_is_confirmed_only_with_the_right_key(repo, tmp_path):
    path, base, head = repo
    source = tmp_path / 'receipt.json'
    source.write_text(json.dumps(_gate(head, base, key=b'k1')))
    assert 'контрольная сумма и подпись совпадают' in packet.make_packet(path, base, head, source, key=b'k1')
    wrong = packet.make_packet(path, base, head, source, key=b'k2')
    assert 'Подпись Gate receipt не совпадает' in wrong and '**passed**' not in wrong
    source.write_text(json.dumps(_gate(head, base)))
    assert 'не подписан' in packet.make_packet(path, base, head, source, key=b'k1')


def test_english_packet_says_the_same(repo, tmp_path):
    path, base, head = repo
    source = tmp_path / 'receipt.json'
    source.write_text(json.dumps(_gate(head, base)))
    result = packet.make_packet(path, base, head, source, lang='en')
    assert '# Review packet' in result and '`tests`: **passed**' in result
    assert 'origin and signature are not confirmed' in result
    assert 'does not prove the absence of defects' in result
    assert 'Check results are not confirmed' in packet.make_packet(path, base, head, None, lang='en')


def test_cli_writes_the_packet_and_reads_the_key_from_the_environment(repo, tmp_path, monkeypatch, capsys):
    path, base, head = repo
    source = tmp_path / 'receipt.json'
    source.write_text(json.dumps(_gate(head, base, key=b'ci-key')))
    out = tmp_path / 'packet.md'
    monkeypatch.setenv(RECEIPT_KEY_ENV, 'ci-key')
    assert main(['review-packet', '--repo', str(path), '--base', base, '--gate-receipt', str(source),
                 '--output', str(out), '--lang', 'en']) == 0
    assert 'the digest and the signature match' in out.read_text()
    assert main(['review-packet', '--repo', str(path), '--base', 'no-such-ref']) == 1
    assert 'cannot create the packet' in capsys.readouterr().err
