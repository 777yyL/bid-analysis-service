import pytest
from analyzer.config import Settings


def test_shared_mode_requires_postgres_and_isolated_result_schema():
    with pytest.raises(ValueError,match='同库模式'):
        Settings(_env_file=None,source_mode='shared',database_url='sqlite:///:memory:')
    with pytest.raises(ValueError,match='同库模式'):
        Settings(_env_file=None,source_mode='shared',database_url='postgresql+psycopg://u:p@localhost/main')
    cfg=Settings(_env_file=None,source_mode='shared',database_url='postgresql+psycopg://u:p@localhost/main',analysis_schema='bid_analysis')
    assert cfg.source_mode=='shared'


@pytest.mark.asyncio
async def test_shared_mode_preserves_completed_result_and_does_not_claim_sync(tmp_path):
    from analyzer.store import Store,digest
    from analyzer.sales_service import SalesService
    from analyzer.sales_store import SalesProject,SalesJob
    cfg=Settings(_env_file=None,database_url=f"sqlite:///{tmp_path/'shared.db'}",sales_secret_key_file=str(tmp_path/'key'))
    cfg=cfg.model_copy(update={'source_mode':'shared'})
    store=Store(cfg.database_url)
    store.initialize(sales_only=True)
    service=SalesService(store,cfg)
    service.save_settings({'paused':False,'history_enabled':True,'manual_confirmation_enabled':True,'review_enabled':False})
    effective=service.settings()
    assert effective['review_enabled'] and not effective['manual_confirmation_enabled']
    assert not effective['history_enabled'] and not effective['low_score_review_enabled']
    with store.session.begin() as s:
        s.add(SalesProject(project_key='keep',title='原结果',channel='采购公告',status='matched',revision='old',
            material={'_source_revision':digest('')},updated_at=1))
    sync=service.enqueue('sync_source',{})
    job=service.enqueue('analyze',{'project_key':'keep','force':False})
    await service.run_once()
    with store.session() as s:
        assert s.get(SalesJob,sync['id']).status=='queued'
        assert s.get(SalesJob,job['id']).status=='completed'
        assert s.get(SalesProject,'keep').revision=='old'
    await service.close()
    store.close()
