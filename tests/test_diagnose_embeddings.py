import importlib.util
import json
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts/diagnose_embeddings.py'


def load_script():
    spec = importlib.util.spec_from_file_location('diagnose_embeddings', PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vectors_follow_api_indexes_and_reject_duplicates():
    script = load_script()
    payload = {'data': [{'index': 1, 'embedding': [0, 2]}, {'index': 0, 'embedding': [3, 0]}]}
    assert script.parse_vectors(payload, 2, 2) == [[3.0, 0.0], [0.0, 2.0]]
    payload['data'][1]['index'] = 1
    with pytest.raises(ValueError):
        script.parse_vectors(payload, 2, 2)


@pytest.mark.parametrize('value', [[0, 0], [float('nan'), 1], [1], ['1', 0]])
def test_invalid_vectors_are_rejected(value):
    with pytest.raises(ValueError):
        load_script().parse_vectors({'data': [{'index': 0, 'embedding': value}]}, 1, 2)


def test_normalization_difference_is_distinguished_from_direction_difference():
    script = load_script()
    same = script.compare([1, 0], [2, 0])
    assert same['cosine'] == pytest.approx(1)
    assert same['normalized_max_abs_diff'] == pytest.approx(0)
    assert same['raw_max_abs_diff'] == pytest.approx(1)
    different = script.compare([1, 0], [0, 1])
    assert different['cosine'] == pytest.approx(0)
    assert script.compare([1, 0], [1])['error']


def test_profile_matches_service_digest_without_secrets():
    from analyzer.sales_store import vector_profile
    cfg = dict(embedding_base_url='http://localhost/v1', embedding_model='test',
               embedding_dimensions=2, embedding_query_prefix='', embedding_document_prefix='')
    assert load_script().profile(cfg) == vector_profile(cfg)


def test_report_uses_database_text_prefix_and_hides_credentials(monkeypatch, tmp_path):
    import httpx

    script = load_script()
    cfg = script.DEFAULTS | dict(embedding_base_url='http://internal/v1',
        embedding_model='test', embedding_dimensions=2, embedding_query_prefix='instruction\n',
        embedding_api_key='encrypted-not-for-output')
    row = dict(id='item1', name='销售项A', description='数据库能力描述', enabled=True,
        version='v1', vector_version='v1', vector_profile=script.profile(cfg), vector=[0, 1])
    monkeypatch.setattr(script, 'read_database', lambda args: (cfg, [row], [], ['销售项A']))
    monkeypatch.setenv('DIAG_EMBEDDING_API_KEY', 'test-credential-not-for-output')
    captured = []

    def respond(request):
        payload = json.loads(request.content)
        captured.extend(payload['input'])
        assert request.headers['Authorization'] == 'Bearer test-credential-not-for-output'
        return httpx.Response(200, json={'data': [
            {'index': i, 'embedding': [1, 0]} for i in range(len(payload['input']))]})

    original_client = httpx.Client
    monkeypatch.setattr(script.httpx, 'Client',
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    target = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['diagnostic', '--dsn', 'postgresql://unused',
        '--output', str(target), '--reference', str(tmp_path / 'absent.json')])
    script.main()
    assert captured[0] == 'instruction\n销售项：销售项A\n能力描述：数据库能力描述'
    report_text = target.read_text(encoding='utf-8')
    assert 'test-credential-not-for-output' not in report_text
    assert 'encrypted-not-for-output' not in report_text
    report = json.loads(report_text)
    item = report['sales_items'][0]
    assert item['old_vs_new']['cosine'] == 0
    assert item['stored_sales_vs_fresh_document_max'] == 0
    assert item['fresh_sales_vs_fresh_document_max'] == 1
    assert item['flags']
