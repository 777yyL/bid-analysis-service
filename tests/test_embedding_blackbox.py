import importlib.util
import json
from pathlib import Path

import pytest


def load_script(monkeypatch):
    folder = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location('embedding_blackbox', folder/'test_embedding_blackbox.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gap_and_auc_measure_separation_without_assuming_absolute_threshold(monkeypatch):
    script = load_script(monkeypatch)
    docs = [{'id':'positive','text':'p','expected_sales':['A'],'kind':'positive'},
            {'id':'negative','text':'n','expected_sales':[],'kind':'negative'}]
    result = script.evaluate_scores(['A'], docs, [[.9,.7]])
    metric = result['per_sales_item'][0]
    assert metric['separation_gap'] == pytest.approx(.2)
    assert metric['pairwise_positive_win_rate'] == 1
    assert metric['negative_above_055'] == 1


def test_ties_and_inverted_ranking_are_visible(monkeypatch):
    script = load_script(monkeypatch)
    docs = [{'id':'p','text':'p','expected_sales':['A'],'kind':'positive'},
            {'id':'n','text':'n','expected_sales':[],'kind':'negative'}]
    metric = script.evaluate_scores(['A'], docs, [[.7,.7]])['per_sales_item'][0]
    assert metric['pairwise_positive_win_rate'] == .5
    assert metric['separation_gap'] == 0
    metric = script.evaluate_scores(['A'], docs, [[.6,.8]])['per_sales_item'][0]
    assert metric['pairwise_positive_win_rate'] == 0
    assert metric['separation_gap'] < 0


def test_fixture_contains_positive_and_hard_negative_cases(monkeypatch):
    script = load_script(monkeypatch)
    for item in script.SAMPLES:
        assert any(item['name'] in d['expected_sales'] for d in script.DOCUMENTS)
        assert any(item['name'] not in d['expected_sales'] for d in script.DOCUMENTS)
    assert any(d['kind']=='bidding_process' and not d['expected_sales'] for d in script.DOCUMENTS)


def test_full_run_matches_exact_reference_without_writing_secrets(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import httpx

    script = load_script(monkeypatch)
    cfg = script.diagnostic.DEFAULTS | {'embedding_base_url':'http://internal/v1',
        'embedding_model':'test','embedding_dimensions':2,'embedding_batch_size':3}
    monkeypatch.setattr(script.diagnostic, 'read_database', lambda args:(cfg,[],[],[]))
    monkeypatch.setattr(script.diagnostic, 'get_key', lambda cfg,args:'private-test-key')

    def respond(request):
        assert request.headers['Authorization']=='Bearer private-test-key'
        batch = json.loads(request.content)['input']
        return httpx.Response(200,json={'data':[
            {'index':i,'embedding':[1,0]} for i in reversed(range(len(batch)))]})

    original_client = httpx.Client
    monkeypatch.setattr(script.httpx,'Client',lambda **kwargs:
        original_client(transport=httpx.MockTransport(respond),**kwargs))
    args = SimpleNamespace(trust_env=False,reference=tmp_path/'missing.json',
        output=tmp_path/'report.json',save_reference=tmp_path/'reference.json')
    script.run(args)
    args.reference=args.save_reference
    args.save_reference=None
    script.run(args)
    output=args.output.read_text(encoding='utf-8')
    result=json.loads(output)
    assert result['read_only']
    assert result['online_reference']['loaded_for_identical_inputs']
    assert len(result['variants']['empty_prefix']['per_document'])==8
    assert 'private-test-key' not in output
    assert cfg['embedding_query_prefix']==''
