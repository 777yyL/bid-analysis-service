from contextlib import closing

import pytest

from analyzer.config import Settings
from analyzer.sales_service import SalesService
from analyzer.sales_sync import SourceSync
from analyzer.store import Store


@pytest.mark.asyncio
async def test_download_batch_resumes_and_preserves_project_warnings(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path/'download.db'}",
                   sales_secret_key_file=str(tmp_path/'key'))
    with closing(Store(cfg.database_url)) as store:
        store.initialize()
        service = SalesService(store, cfg)
        service.save_settings({'source_download_url':'http://share', 'source_download_key':'private-key',
                               'source_download_concurrency':2})
        snapshot = {'ready':True, 'synced_at':123}
        monkeypatch.setattr(SourceSync, 'status', lambda self:snapshot)
        seen = []
        class LocalSource:
            def __init__(self, cfg):
                pass
            def list_projects(self, cursor, limit, later):
                assert limit == 2
                return [{'project_key':'a'}, {'project_key':'b'}] if not cursor else []
            def project_material(self, key, later):
                seen.append(key)
                return {'warnings':['正文不存在'] if key=='b' else []}
        service.source_factory = LocalSource
        job = service.enqueue_download(service.settings(), snapshot)
        assert service.enqueue_download(service.settings(), snapshot)['id'] == job['id']
        assert await service.run_once()
        current = service.jobs()['items'][0]
        assert current['status']=='queued' and current['payload']['cursor']=='b'
        assert current['payload']['processed']==2 and current['payload']['errors']==1
        from analyzer.sales_store import SalesJob
        with store.session.begin() as session:
            session.get(SalesJob, job['id']).available_at = 0
        assert await service.run_once()
        assert service.jobs()['items'][0]['status']=='completed'
        assert sorted(seen)==['a','b']
        assert service.settings(masked=True)['source_download_key']==''
        assert service.settings(masked=True)['source_download_key_configured'] is True


@pytest.mark.asyncio
async def test_download_stops_if_snapshot_changes(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path/'download.db'}",
                   sales_secret_key_file=str(tmp_path/'key'))
    with closing(Store(cfg.database_url)) as store:
        store.initialize()
        service = SalesService(store, cfg)
        service.save_settings({'source_download_url':'http://share'})
        monkeypatch.setattr(SourceSync, 'status', lambda self:{'ready':True, 'synced_at':124})
        from analyzer.material_download import download_batch
        with pytest.raises(ValueError, match='已变化'):
            await download_batch(service, {'payload':{'snapshot':123,'url':'http://share'}}, service.settings())
