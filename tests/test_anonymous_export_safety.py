import json

from skilllayer.telemetry_export import export_anonymous_telemetry, privacy_scan


def test_export_drops_untrusted_text_and_dynamic_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / 'runs/skilllayer_demand'
    path.mkdir(parents=True)
    (path / 'opportunity_ranking.json').write_text(json.dumps({'opportunities': [
        {'candidate_workflow': 'alice@example.com /home/alice/private'}]}))
    (path / 'demand_summary.json').write_text(json.dumps({'warnings': ['secret text'],
        'deduped_counts_by_category': {'alice@example.com': 3}}))
    result = export_anonymous_telemetry(output=tmp_path / 'export.json')
    payload = (tmp_path / 'export.json').read_text()
    assert result['success'] and result['privacy_scan']['passed']
    assert 'alice@example.com' not in payload
    assert '/home/alice/private' not in payload
    assert 'secret text' not in payload


def test_unknown_text_cannot_pass_scan():
    assert not privacy_scan({'unknown': 'arbitrary content'})['passed']
    assert not privacy_scan({'summary': {'unknown': 1}})['passed']
    assert not privacy_scan({})['passed']
    assert not privacy_scan({'summary': {'total_runs': 'anonymous'}})['passed']


def test_unsafe_export_options_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for option in ('include_raw_tasks', 'include_local_paths', 'allow_unsafe'):
        result = export_anonymous_telemetry(output=tmp_path / 'export.json', **{option: True})
        assert not result['success']
        assert not result['privacy_scan']['passed']
        assert not (tmp_path / 'export.json').exists()


def test_failed_scan_does_not_recommend_unsafe_export(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('skilllayer.telemetry_export.privacy_scan', lambda _: {
        'passed': False, 'finding_count': 1, 'findings': [], 'patterns_checked': [],
    })
    output = tmp_path / 'export.json'
    result = export_anonymous_telemetry(output=output)
    assert not result['success']
    assert not result['export_written']
    assert '--allow-unsafe' not in result['error']
    assert not output.exists()
