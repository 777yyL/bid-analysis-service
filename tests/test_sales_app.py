from fastapi.testclient import TestClient

from analyzer.app import create_app
from analyzer.config import Settings


def test_only_sales_endpoints_and_workers_are_exposed(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path/'app.db'}",
                   analyzer_api_key='test', sales_secret_key_file=str(tmp_path/'key'))
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get('/healthz').status_code == 200
        assert client.get('/api/v3/settings').status_code == 401
        result = client.get('/api/v3/settings', headers={'X-API-Key':'test'})
        assert result.status_code == 200
        assert client.get('/api/v3/projects/metrics').status_code == 401
        metrics = client.get('/api/v3/projects/metrics', headers={'X-API-Key':'test'})
        assert metrics.status_code == 200 and metrics.json()['total'] == 0
        assert result.json()['collect_attachments'] is True
        assert result.json()['low_score_review_enabled'] is True
        assert result.json()['review_enabled'] is True
        for url in ('/api/v2/overview', '/api/v2/projects', '/config', '/knowledge', '/jobs'):
            assert client.get(url, headers={'X-API-Key':'test'}).status_code == 404
        names = [t.get_name() for t in app.state.sales_tasks]
        assert len(names) == 33 and all(n.startswith('sales-') for n in names)


def test_daily_and_operations_auth_validation_and_pagination(tmp_path):
    cfg=Settings(_env_file=None,database_url=f"sqlite:///{tmp_path/'daily.db'}",analyzer_api_key='test',
        sales_secret_key_file=str(tmp_path/'key'),worker_enabled=False)
    app=create_app(cfg)
    headers={'X-API-Key':'test'}
    with TestClient(app) as client:
        assert client.get('/api/v3/daily?day=2026-11-16').status_code==401
        assert client.get('/api/v3/daily?day=bad',headers=headers).status_code==422
        assert client.get('/api/v3/daily?day=2026-11-16&days=30',headers=headers).status_code==422
        assert client.get('/api/v3/daily/projects?day=2026-11-16&slice=unknown',headers=headers).status_code==422
        assert client.get('/api/v3/daily?day=2026-11-16',headers=headers).status_code==502
        for i in range(3):
            app.state.sales.enqueue('analyze',{'project_key':str(i)})
        app.state.sales.enqueue('sync_source',{})
        first=client.get('/api/v3/operations?page_size=2',headers=headers).json()
        second=client.get('/api/v3/operations?page_size=2&page=2',headers=headers).json()
        assert first['total']==3 and first['counts']=={'queued':3}
        assert len(first['items'])==2 and len(second['items'])==1
        assert not ({r['id'] for r in first['items']} & {r['id'] for r in second['items']})
        assert client.get('/api/v3/deployment',headers=headers).json()['source_mode']=='snapshot'
