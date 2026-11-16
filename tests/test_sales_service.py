
import pytest
from fastapi.testclient import TestClient

from analyzer.app import create_app
from analyzer.config import Settings
from analyzer.sales_service import SalesService
from analyzer.sales_store import SalesData, SalesJob
from analyzer.store import Store


@pytest.fixture
def sales(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'sales.db'}",
                   sales_secret_key_file=str(tmp_path / 'runtime' / 'settings.key'), worker_enabled=False)
    store = Store(cfg.database_url)
    store.initialize()
    service = SalesService(store, cfg)
    service.save_settings({'engine_enabled': True, 'embedding_model': 'model-a', 'embedding_dimensions': 2,
                           'manual_confirmation_enabled':True})
    class Source:
        def __init__(self, cfg):
            pass
        def profile(self):
            return {'models': [{'model': 'model-a', 'dimensions': 2, 'count': 1}]}
        def project_material(self, key, allow_later):
            return {'project_key': key, 'channel': '采购公告', 'title': '访客软件采购', 'status': 'ready',
                'main_notice_id': 1, 'correction_notice_ids': [], 'warnings': [], 'notices': [],
                'chunks': [{'chunk_id': 7, 'created_at': '2026-11-02T12:00:00', 'notice_id': 1,
                    'attachment_id': None, 'source_type': 'notice', 'content': '采购需求：采购访客管理软件，实现预约审批。',
                    'content_hash': 'a', 'embed_model': 'model-a', 'embedding': [1.0, 0.0]}]}
        def list_projects(self, cursor, limit, allow_later):
            return [{'project_key': 'p', 'channel': '采购公告'}] if not cursor else []
    class Model:
        def __init__(self, cfg):
            self.calls = []
        async def embed_sales(self, items):
            self.calls.extend(items)
            return [[1.0, 0.0] for item in items]
        async def embed_text(self, text):
            return [1.0, 0.0]
        async def embed_documents(self, texts):
            return [[1.0, 0.0] for text in texts]
        async def aclose(self):
            pass
    service.source_factory, service.model_factory = Source, Model
    service.import_items([{'id': 'visit', 'name': '访客管理', 'description': '访客预约审批、登记、通行授权',
                           'categories': ['人员通行', '安保管理'], 'products': ['园区平台']}])
    yield service
    store.close()


def test_settings_encrypt_secrets_and_blank_preserves(sales):
    response = sales.save_settings({'source_dsn': 'postgresql://readonly:secret@db/bidinfo', 'embedding_api_key': 'secret-key'})
    assert response['source_dsn'] == '' and response['source_dsn_configured']
    with sales.store.session() as session:
        stored = str(session.get(SalesData, 'settings').value)
        assert 'readonly' not in stored and 'secret-key' not in stored
    sales.save_settings({'embedding_api_key': ''})
    assert sales.settings()['embedding_api_key'] == 'secret-key'
    sales.save_settings({'embedding_api_key_clear': True})
    assert not sales.settings()['embedding_api_key']


def test_saved_threshold_is_not_silently_replaced_by_new_default(sales):
    sales.save_settings({'candidate_threshold': .55})
    reloaded = SalesService(sales.store, sales.bootstrap)
    assert reloaded.settings()['candidate_threshold'] == .55


@pytest.mark.asyncio
async def test_project_metrics_count_projects_and_ignore_stale_or_disabled_labels(sales):
    from sqlalchemy import select

    from analyzer.sales_store import SalesMatch
    from analyzer.sales_sync import sync_signature
    from analyzer.store import digest

    assert sales.project_metrics()['total'] == 0
    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key':'p'})
    await sales.run_once()
    cfg = sales.settings()
    sales.set_state('source_sync', {'signature':sync_signature(cfg), 'source_scope':digest(cfg['source_dsn']),
        'counts':{'projects':12}, 'synced_at':1})
    values = sales.project_metrics()
    assert values['total']==12 and values['analyzed']==1 and values['matched']==1
    assert values['not_analyzed']==11 and values['hit_rate']==100
    with sales.store.session.begin() as session:
        current = session.scalar(select(SalesMatch))
        session.add(SalesMatch(id='stale-metric', project_key='p', sales_item_id='visit',
            revision='old', item_version=current.item_version, status='candidate', method='vector',
            score=.6, reason='过期结果', evidence=[]))
    assert sales.project_metrics()['needs_review']==0
    with sales.store.session.begin() as session:
        current = session.scalar(select(SalesMatch).where(SalesMatch.id!='stale-metric'))
        current.status = 'candidate'
    values = sales.project_metrics()
    assert values['matched']==0 and values['needs_review']==1 and values['analyzed']==1
    sales.set_enabled('visit', False)
    assert sales.project_metrics()['needs_review']==0


@pytest.mark.asyncio
async def test_default_low_score_review_does_not_auto_accept_when_model_is_missing(sales):
    sales.save_settings({'candidate_threshold':.5, 'manual_confirmation_enabled':False})
    original = sales.source_factory.project_material
    def material(self, key, allow):
        result = original(self, key, allow)
        result['chunks'][0]['embedding'] = [.6, .8]
        return result
    sales.source_factory.project_material = material
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key':'p'})
    await sales.run_once()
    result = sales.projects(key='p')['items'][0]
    assert result['status'] == 'needs_review'
    assert result['matches'][0]['status'] == 'candidate'
    assert sales.jobs()['items'][0]['status'] == 'failed'
    assert '配置接口及模型' in sales.jobs()['items'][0]['error']


@pytest.mark.asyncio
async def test_project_filters_use_confirmed_sales_categories_and_products(sales):
    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize',{'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    assert sales.projects(hit=True,category='人员通行',product='园区平台')['total']==1
    assert sales.projects(hit=False)['total']==0
    assert sales.projects(product='不存在产品')['total']==0
    sales.set_enabled('visit',False)
    assert sales.projects(hit=True)['total']==0
    assert sales.projects(hit=False)['total']==1


@pytest.mark.asyncio
async def test_list_similarity_threshold_is_strict_and_filters_projects_and_labels(sales):
    from sqlalchemy import select

    from analyzer.sales_store import SalesMatch

    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize',{'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    with sales.store.session.begin() as session:
        session.scalar(select(SalesMatch)).score=.6
    assert sales.projects(min_score=.6)['total']==0
    assert sales.projects(min_score=.59)['total']==1
    assert sales.projects(min_score=.59,product='园区平台')['total']==1
    assert sales.projects(min_score=.6,product='园区平台')['total']==0
    assert sales.projects()['total']==1
    assert sales.projects(key='p')['items'][0]['matches'][0]['score']==.6


@pytest.mark.asyncio
async def test_threshold_filters_each_label_and_combines_with_all_item_filter(sales):
    from analyzer.sales_store import SalesMatch

    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize',{'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    sales.import_items([{'id':'low','name':'低分项','description':'低分能力','products':['low-product']}])
    sales.enqueue('vectorize',{'ids':['low']})
    await sales.run_once()
    revision=sales.projects(key='p')['items'][0]['revision']
    low=next(item for item in sales.items()['items'] if item['id']=='low')
    with sales.store.session.begin() as session:
        session.add(SalesMatch(id='low-match',project_key='p',sales_item_id='low',revision=revision,
            item_version=low['version'],status='confirmed',method='vector_auto',score=.6,reason='test',evidence=[]))
    result=sales.projects(min_score=.8)
    assert result['total']==1
    assert [m['sales_item_id'] for m in result['items'][0]['matches']]==['visit']
    assert sales.projects(min_score=.8,product='low-product')['total']==0
    assert sales.projects(min_score=.8,sales_item_ids=['visit','low'],match_mode='all')['total']==0
    assert sales.projects(min_score=.8,sales_item_ids=['visit','low'],match_mode='any')['total']==1
    assert len(sales.projects(key='p')['items'][0]['matches'])==2


def test_body_endpoint_reads_original_documents_without_analysis(sales):
    from pydantic import SecretStr

    app=create_app(sales.bootstrap.model_copy(update={'analyzer_api_key':SecretStr('body-test')}))
    class Source:
        def project_material(self,key,allow_later):
            return {'status':'ready','warnings':[],'documents':[{'notice_id':1,'source_type':'notice',
                'content':'采购需求：建设访客管理系统。\n供应商资格要求：依法注册。'}]}
    app.state.sales.source_factory=lambda cfg:Source()
    app.state.sales.model_factory=lambda cfg:pytest.fail('reading body must not invoke a model')
    with TestClient(app) as client:
        options=client.get('/api/v3/filters/options',headers={'X-API-Key':'body-test'})
        assert options.status_code==200
        assert '园区平台' in options.json()['products']
        result=client.post('/api/v3/analysis/body',headers={'X-API-Key':'body-test'},json={'project_key':'p'})
        assert result.status_code==200
        assert '供应商资格要求' in result.json()['documents'][0]['content']
        assert result.json()['source']=='current_files'


@pytest.mark.asyncio
@pytest.mark.parametrize('score,verdict,status,calls',[
    (.6,'confirmed','matched',1),(.6,'excluded','no_match',1),
    (.6,'candidate','needs_review',1),(.9,'candidate','matched',0)])
async def test_low_score_review_never_auto_accepts_uncertain_low_score(sales,score,verdict,status,calls):
    sales.save_settings({'retrieval_backend':'maas','manual_confirmation_enabled':False,
        'review_enabled':True,'review_base_url':'https://review/v1','review_model':'review',
        'low_score_review_enabled':True,'review_score_threshold':.8})
    class Knowledge:
        async def retrieve(self,query):
            return [{'chunkId':'kb','similarity':score,'chunkContent':'销售项ID：visit'}]
    sales.knowledge_client=lambda cfg:Knowledge()
    reviewed=[]
    async def review(candidate,*args):
        reviewed.append(candidate['sales_item_id'])
        candidate.update(status=verdict,method='model',reason='测试复核结论')
    sales.review_candidate=review
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    assert sales.projects()['items'][0]['status']==status
    assert len(reviewed)==calls


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict,status',[('excluded','no_match'),('candidate','needs_review'),('confirmed','matched')])
async def test_historical_review_updates_low_auto_tags_without_retrieval(sales,verdict,status):
    from sqlalchemy import select

    from analyzer.sales_history_review import preview
    from analyzer.sales_store import SalesMatch

    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize',{'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    with sales.store.session.begin() as session:
        match=session.scalar(select(SalesMatch).where(SalesMatch.project_key=='p'))
        match.score=.6
    sales.save_settings({'review_enabled':True,'review_model':'review','review_base_url':'https://review/v1'})
    assert preview(sales,sales.settings())['projects']==1
    revision=sales.projects(key='p')['items'][0]['revision']
    class NoSource:
        def project_material(self,*args):
            pytest.fail('historical review must not read source')
    sales.source_factory=lambda cfg:NoSource()
    async def review(candidate,*args):
        candidate.update(status=verdict,method='model',reason='历史复核结论')
    sales.review_candidate=review
    from analyzer.store import digest
    sales.enqueue('review_existing',{'project_key':'p','revision':revision,'threshold':.8,
        'source_scope':digest(sales.settings()['source_dsn'])},digest(['analyze','p']))
    assert await sales.run_once()
    detail=sales.projects(key='p')['items'][0]
    assert detail['status']==status
    assert detail['matches'][0]['method']=='model_backfill'
    assert any(a['payload'].get('event')=='historical_low_score_review' for a in detail['history'])
    assert preview(sales,sales.settings())['projects']==0


@pytest.mark.asyncio
async def test_history_review_scans_in_batches_and_preserves_manual_changes(sales):
    from sqlalchemy import select

    from analyzer.sales_history_review import preview
    from analyzer.sales_store import SalesMatch
    from analyzer.store import digest

    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize',{'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    with sales.store.session.begin() as session:
        session.scalar(select(SalesMatch)).score=.6
    sales.save_settings({'review_enabled':True,'review_model':'review','review_base_url':'https://review/v1'})
    parent=sales.enqueue('review_history',{'threshold':.8,'cursor':'','enqueued':0,
        'source_scope':digest(sales.settings()['source_dsn'])},digest(['review_history']))
    await sales.run_once()
    with sales.store.session() as session:
        children=list(session.scalars(select(SalesJob).where(SalesJob.kind=='review_existing')))
        assert len(children)==1
        assert session.get(SalesJob,parent['id']).payload['cursor']=='p'
    async def changed_during_review(candidate,*args):
        with sales.store.session.begin() as session:
            match=session.scalar(select(SalesMatch))
            match.method='manual'
            match.reason='人工已确认'
        candidate.update(status='excluded',reason='模型排除')
    sales.review_candidate=changed_during_review
    await sales.run_once()
    detail=sales.projects(key='p')['items'][0]
    assert detail['matches'][0]['method']=='manual'
    assert detail['matches'][0]['status']=='confirmed'
    assert preview(sales,sales.settings())['projects']==0


def test_overview_and_project_list_do_not_fetch_sales_vectors(sales):
    import re

    from sqlalchemy import event

    statements=[]
    def collect(conn,cursor,statement,parameters,context,executemany):
        statements.append(statement)
    event.listen(sales.store.engine,'before_cursor_execute',collect)
    try:
        assert sales.overview()['enabled_sales_items']==1
        sales.projects()
    finally:
        event.remove(sales.store.engine,'before_cursor_execute',collect)
    assert not any(re.search(r'sales_items_v1\.vector(?:,|\s+FROM)',sql) for sql in statements)


@pytest.mark.asyncio
async def test_project_list_omits_heavy_material_and_evidence_but_detail_preserves(sales):
    sales.enqueue('vectorize',{'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze',{'project_key':'p'})
    await sales.run_once()
    listed=sales.projects()['items'][0]
    assert 'evidence' not in listed['matches'][0]
    assert sales.projects(key='p')['items'][0]['matches'][0]['evidence']


@pytest.mark.asyncio
async def test_project_endpoint_does_not_impose_extra_wait_timeout(sales):
    import asyncio
    import time

    import httpx
    from pydantic import SecretStr

    bootstrap=sales.bootstrap.model_copy(update={'analyzer_api_key':SecretStr('gate-test')})
    app=create_app(bootstrap)
    def slow(*args,**kwargs):
        time.sleep(.4)
        return {'items':[],'total':0}
    app.state.sales.projects=slow
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                    headers={'X-API-Key':'gate-test'}) as client:
            responses=await asyncio.gather(*(client.get('/api/v3/projects') for _ in range(3)))
        assert all(r.status_code==200 for r in responses)
    finally:
        app.state.store.close()


def test_job_logs_preserve_stages_across_heartbeat(sales):
    sales.enqueue('vectorize',{'ids':[]})
    job=sales.claim()
    sales.update_job(job,progress={'phase':'reading_project','project_key':'p'})
    sales.update_job(job,lease_until=123456)
    sales.update_job(job,progress={'phase':'matching','chunks':2})
    with sales.store.session() as session:
        row=session.get(SalesJob,job['id'])
        assert [e['phase'] for e in row.progress['events']]==['reading_project','matching']
        assert row.progress['phase_started_at']>0
        assert row.progress['project_key']=='p'


@pytest.mark.asyncio
async def test_confirmation_off_automatically_applies_candidates_and_on_requires_review(sales):
    from analyzer.sales_models import RuntimeSettings

    assert RuntimeSettings().manual_confirmation_enabled is False
    base_source=sales.source_factory
    class Source(base_source):
        def project_material(self,key,allow_later):
            material=super().project_material(key,allow_later)
            material['chunks'][0]['content']='采购需求：采购访客软件，实现预约审批。'
            return material
    sales.source_factory=Source
    sales.enqueue('vectorize',{'ids':[]})
    assert await sales.run_once()
    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('analyze',{'project_key':'p'})
    assert await sales.run_once()
    assert sales.projects()['items'][0]['status']=='matched'
    assert sales.projects()['items'][0]['matches'][0]['method']=='vector_auto'
    sales.save_settings({'manual_confirmation_enabled':True})
    sales.enqueue('analyze',{'project_key':'p','force':True})
    assert await sales.run_once()
    project=sales.projects()['items'][0]
    assert project['status']=='needs_review'
    assert project['matches'][0]['status']=='candidate'


@pytest.mark.asyncio
async def test_maas_project_without_vectors_preserves_notice_evidence(sales):
    sales.save_settings({'retrieval_backend':'maas','knowledge_api_key':'kb-secret',
                         'knowledge_url':'https://maas/retrieve','knowledge_id':'kb'})
    old_source = sales.source_factory
    class Source(old_source):
        def project_material(self,key,allow_later):
            result = super().project_material(key,allow_later)
            result['documents'] = [{'notice_id':1,'source_type':'notice','content':
                '采购需求：采购访客管理软件，实现预约审批。'}]
            result.pop('chunks')
            return result
    class Knowledge:
        async def retrieve(self,query):
            assert '采购' in query
            return [{'chunkId':'kb-chunk','docId':'doc1','similarity':.65,
                     'chunkContent':'销售项ID：visit\n销售项名称：访客管理'}]
    sales.source_factory = Source
    sales.knowledge_client = lambda cfg: Knowledge()
    sales.model_factory = lambda cfg: pytest.fail('knowledge mode must not create embedding client')
    sales.enqueue('analyze',{'project_key':'p'})
    assert await sales.run_once()
    project = sales.projects()['items'][0]
    assert project['status']=='needs_review'
    assert project['matches'][0]['sales_item_id']=='visit'
    assert project['matches'][0]['method']=='knowledge'
    assert '采购访客管理软件' in sales.projects(key='p')['items'][0]['matches'][0]['evidence'][0]['content']
    assert sales.settings(masked=True)['knowledge_api_key']==''
    with sales.store.session() as session:
        assert 'kb-secret' not in str(session.get(SalesData,'settings').value)


def test_knowledge_api_test_reports_unmapped_chunks(sales):
    app = create_app(sales.bootstrap)
    class Knowledge:
        async def retrieve(self,query):
            return [{'chunkId':'c1','chunkContent':'销售项ID：visit','similarity':.7},
                    {'chunkId':'c2','chunkContent':'销售项ID：unknown','similarity':.8}]
    app.state.sales.knowledge_client = lambda cfg: Knowledge()
    with TestClient(app) as client:
        key = sales.bootstrap.analyzer_api_key.get_secret_value()
        # Use a dedicated test bootstrap key; do not depend on the developer's .env.
        app.state.sales.bootstrap.analyzer_api_key = type(sales.bootstrap.analyzer_api_key)('kb-test')
        # auth closes over the same bootstrap object.
        response = client.post('/api/v3/knowledge/test',headers={'X-API-Key':'kb-test'},
                               json={'text':'采购访客软件'})
        assert response.status_code==200, response.text
        assert response.json()['unresolved']==1
        assert response.json()['items'][0]['sales_item_id']=='visit'
        app.state.sales.bootstrap.analyzer_api_key = type(sales.bootstrap.analyzer_api_key)(key)


@pytest.mark.asyncio
async def test_default_threshold_filters_candidates_in_project_and_search(sales):
    import math

    sales.enqueue('vectorize', {'ids': []})
    assert await sales.run_once()
    cfg = sales.settings()
    assert cfg['candidate_threshold'] == .80
    material = sales.source_factory(cfg).project_material('p', False)
    material['chunks'][0]['embedding'] = [.79, math.sqrt(1-.79**2)]
    assert sales.candidates(material, cfg, sales.catalog(cfg)) == []

    class Model:
        score = .79

        async def embed_documents(self, texts):
            return [[self.score, math.sqrt(1-self.score**2)] for _ in texts]

    model = Model()
    sales.client = lambda cfg: model
    text = '采购需求：采购访客管理软件，实现预约审批。'
    assert (await sales.search(text))['items'] == []
    model.score = .81
    assert len((await sales.search(text))['items']) == 1
    material['chunks'][0]['embedding'] = [.81, math.sqrt(1-.81**2)]
    assert len(sales.candidates(material, cfg, sales.catalog(cfg))) == 1


@pytest.mark.asyncio
async def test_import_vectorize_match_and_disable(sales):
    sales.save_settings({'manual_confirmation_enabled':False})
    sales.enqueue('vectorize', {'ids': []})
    assert await sales.run_once()
    assert sales.items()['items'][0]['vector_status'] == 'ready'
    sales.enqueue('analyze', {'project_key': 'p'})
    assert await sales.run_once()
    project = sales.projects()['items'][0]
    assert project['status'] == 'matched'
    assert project['matches'][0]['sales_item_id'] == 'visit'
    assert project['matches'][0]['description'] == '访客预约审批、登记、通行授权'
    assert project['matches'][0]['categories'] == ['人员通行', '安保管理']
    assert sales.projects(key='p')['items'][0]['matches'][0]['evidence'][0]['created_at'] == '2026-11-02T12:00:00'
    assert len(sales.client().calls) == 1  # Only the sales item was encoded, never an announcement.
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    assert len(sales.client().calls) == 1
    assert sales.projects(category='人员通行', sales_item_ids=['visit'])['total'] == 1
    sales.set_enabled('visit', False)
    assert sales.projects()['items'][0]['matches'] == []
    assert sales.projects(sales_item_ids=['visit'])['total'] == 0


@pytest.mark.asyncio
async def test_description_edit_invalidates_vector_but_relations_do_not(sales):
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    item = sales.items()['items'][0]
    sales.import_items([{**item, 'products': ['另一个产品']}])
    assert sales.items()['items'][0]['vector_status'] == 'ready'
    sales.import_items([{**item, 'description': '更新后的访客预约及登记能力'}])
    assert sales.items()['items'][0]['vector_status'] == 'pending'
    with pytest.raises(ValueError, match='待向量化'):
        sales.catalog(sales.settings())


@pytest.mark.asyncio
async def test_manual_override_and_revision_guard(sales):
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    project = sales.projects()['items'][0]
    with pytest.raises(ValueError, match='版本'):
        sales.review('p', {'sales_item_id': 'visit', 'status': 'excluded', 'reason': '人工核实', 'expected_revision': 'old'})
    sales.review('p', {'sales_item_id': 'visit', 'status': 'excluded', 'reason': '人工核实', 'expected_revision': project['revision']})
    sales.enqueue('analyze', {'project_key': 'p', 'force': True})
    await sales.run_once()
    assert sales.projects()['items'][0]['matches'][0]['method'] == 'manual'
    assert sales.projects()['items'][0]['matches'][0]['status'] == 'excluded'


@pytest.mark.asyncio
async def test_configuration_change_during_embedding_rejects_stale_result(sales):
    original = sales.client().embed_sales
    async def changed(items):
        result = await original(items)
        sales.save_settings({'embedding_query_prefix': 'new-prefix: '})
        return result
    sales.client().embed_sales = changed
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    assert sales.jobs()['items'][0]['status'] == 'failed'
    assert sales.items()['items'][0]['vector_status'] != 'ready'


def test_sales_api_auth_import_and_configuration_validation(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'api.db'}", worker_enabled=False,
        analyzer_api_key='test', sales_secret_key_file=str(tmp_path / 'key'))
    with TestClient(create_app(cfg)) as client:
        assert client.get('/api/v3/settings').status_code == 401
        headers = {'X-API-Key': 'test'}
        response = client.put('/api/v3/settings', headers=headers, json={'embedding_api_key': 'secret', 'candidate_threshold': 2})
        assert response.status_code == 422 and 'secret' not in response.text
        assert client.post('/api/v3/sales-items/import', headers=headers, json={'items': [
            {'name': '访客管理', 'description': '访客授权'}]}).status_code == 200
        assert client.get('/api/v3/sales-items', headers=headers).json()['total'] == 1
        assert client.post('/api/v3/analysis/start', headers=headers, json={'mode': 'history'}).status_code == 409


def test_sales_api_search_and_pagination_reach_beyond_first_hundred(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'search.db'}", worker_enabled=False,
        analyzer_api_key='test', sales_secret_key_file=str(tmp_path / 'key'))
    with TestClient(create_app(cfg)) as client:
        headers = {'X-API-Key': 'test'}
        rows = [{'id': f's{i:03}', 'name': f'能力{i:03}', 'description': '采购能力'} for i in range(151)]
        assert client.post('/api/v3/sales-items/import', headers=headers, json={'items': rows}).status_code == 200
        ids = []
        for page in range(1, 5):
            response = client.get('/api/v3/sales-items', headers=headers,
                params={'enabled': 'true', 'page': page, 'page_size': 50})
            assert response.status_code == 200
            ids.extend(item['id'] for item in response.json()['items'])
        assert len(ids) == len(set(ids)) == 151
        result = client.get('/api/v3/sales-items', headers=headers,
            params={'q': '能力150', 'enabled': 'true', 'page_size': 50}).json()
        assert [item['id'] for item in result['items']] == ['s150']
        client.post('/api/v3/sales-items/s150/enabled', headers=headers, json={'enabled': False})
        assert client.get('/api/v3/sales-items', headers=headers,
            params={'q': '能力150', 'enabled': 'true'}).json()['total'] == 0


@pytest.mark.asyncio
async def test_project_results_expose_all_selected_main_notices(sales):
    original = sales.source_factory.project_material
    def material(self, key, allow_later):
        result = original(self, key, allow_later)
        result.update(main_notice_ids=[1, 2], correction_notice_ids=[3], notices=[
            {'notice_id': i, 'title': f'公告{i}', 'channel': '采购公告' if i < 3 else '更正公告'} for i in (1, 2, 3)])
        return result
    sales.source_factory.project_material = material
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    project = sales.projects()['items'][0]
    assert project['main_notice_ids'] == [1, 2]
    assert project['correction_notice_ids'] == [3]
    assert project['notices'][0]['channel'] == '采购公告'
    assert sales.projects(key='p')['items'][0]['matches'][0]['evidence'][0]['notice_id'] == 1


@pytest.mark.asyncio
async def test_local_material_pipeline_respects_attachment_switch_and_reuses_body(sales):
    sales.save_settings({"collect_attachments":False})
    from sqlalchemy import select

    from analyzer.sales_store import SalesDocumentChunk
    calls = []
    async def encode(texts):
        calls.extend(texts)
        return [[1., 0.] for text in texts]
    sales.client().embed_documents = encode
    def material(self, key, allow_later):
        return {'project_key': key, 'channel': '采购公告', 'status': 'ready', 'main_notice_id': 1,
            'main_notice_ids': [1], 'correction_notice_ids': [], 'documents': [
                {'notice_id': 1, 'source_type': 'notice', 'content': '采购需求：采购访客管理软件。'},
                {'notice_id': 1, 'source_type': 'attachment', 'attachment_id': 3, 'content': '采购附件：访客授权管理。'}]}
    sales.source_factory.project_material = material
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    assert len(calls) == 1
    first = sales.projects()['items'][0]
    assert first['material_storage'] == 'local'
    assert not first['collect_attachments']
    sales.save_settings({'collect_attachments': True})
    # Non-model setting changes retain the same client and rate-limit budget.
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    assert len(calls) == 2
    second = sales.projects()['items'][0]
    assert second['vectorization']['attachment_documents'] == 1
    sales.save_settings({'collect_attachments': False})
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    third = sales.projects()['items'][0]
    assert third['vectorization']['chunks'] == 1
    assert len(calls) == 2
    with sales.store.session() as session:
        assert len(list(session.scalars(select(SalesDocumentChunk)))) == 2


def test_single_project_test_api_queues_only_one_and_reports_status(tmp_path):
    from contextlib import closing

    from analyzer.sales_store import SalesItem, vector_profile
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'test.db'}", worker_enabled=True,
        analyzer_api_key='test', sales_secret_key_file=str(tmp_path / 'key'))
    app = create_app(cfg)
    # Do not run lifespan workers: exercise durable scheduling and status deterministically.
    service = app.state.sales
    service.source_factory = lambda cfg: object()  # Isolate task scheduling from PostgreSQL snapshots.
    service.store.initialize()
    service.save_settings({'source_dsn':'postgresql://readonly/source','source_file_root':'/source-data'})
    service.import_items([{'id':'visit','name':'访客管理','description':'访客预约'}])
    with service.store.session.begin() as session:
        row = session.get(SalesItem, 'visit')
        row.vector, row.vector_version, row.vector_profile = [1.,0.], row.version, vector_profile(service.settings())
    with closing(TestClient(app)) as client:
        headers = {'X-API-Key':'test'}
        assert client.post('/api/v3/analysis/test', json={'project_key':'p'}).status_code == 401
        service.save_settings({'paused':True})
        assert client.post('/api/v3/analysis/test', headers=headers, json={'project_key':'p'}).status_code == 409
        service.save_settings({'paused':False})
        service.enqueue('analyze', {'project_key':'bulk'})
        response = client.post('/api/v3/analysis/test', headers=headers, json={'project_key':'p'})
        assert response.status_code == 200
        job = response.json()
        assert job['payload'] == {'project_key':'p','force':True,'test':True}
        assert not service.settings()['history_enabled'] and not service.settings()['incremental_enabled']
        assert client.get('/api/v3/jobs/'+job['id'], headers=headers).status_code == 200
        assert client.post('/api/v3/analysis/test', headers=headers,
            json={'project_key':'p'}).json()['id'] == job['id']
        assert service.claim()['id'] == job['id']


@pytest.mark.asyncio
async def test_sync_job_runs_while_analysis_is_paused_without_starting_scans(sales, monkeypatch):
    from analyzer.sales_sync import sync_signature
    sales.save_settings({'paused':True, 'engine_enabled':False})
    bulk = sales.enqueue('analyze', {'project_key':'bulk'})
    sync = sales.enqueue('sync_source', {'signature':sync_signature(sales.settings())})
    calls = []
    def refresh(self, progress, batch_id=None):
        calls.append(self.cfg['source_dsn'])
        progress({'phase':'sync_projects'})
        return {'next_sync_at':9999999999}
    monkeypatch.setattr('analyzer.sales_service.SourceSync.refresh', refresh)
    assert await sales.run_once()
    assert calls == ['']
    states = {job['id']:job['status'] for job in sales.jobs()['items']}
    assert states[sync['id']] == 'completed' and states[bulk['id']] == 'queued'
    assert not await sales.run_once()
    assert not sales.settings()['history_enabled'] and not sales.settings()['incremental_enabled']


def test_scan_cursor_cannot_overwrite_a_new_snapshot_generation(sales):
    sales.set_state('history', {'cursor':'', 'snapshot':2, 'processed':0})
    assert sales.update_history({'cursor':'old-key', 'snapshot':1, 'processed':100}) is False
    assert sales.state('history')['cursor'] == ''
    assert sales.update_history({'cursor':'new-key', 'snapshot':2, 'processed':20}) is True
    assert sales.state('history')['cursor'] == 'new-key'


@pytest.mark.asyncio
async def test_old_snapshot_analysis_is_retried_instead_of_overwriting_new_results(sales):
    original = sales.source_factory.project_material
    def material(self, key, allow_later):
        return {**original(self, key, allow_later), '_snapshot_at':1}
    sales.source_factory.project_material = material
    sales.set_state('source_sync', {'synced_at':2})
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    job = sales.enqueue('analyze', {'project_key':'p'})
    await sales.run_once()
    task = next(row for row in sales.jobs()['items'] if row['id']==job['id'])
    assert task['status'] == 'retry' and '快照已更新' in task['error']
    assert sales.projects()['total'] == 0


@pytest.mark.asyncio
async def test_new_snapshot_refreshes_notice_metadata_without_rematching(sales):
    original = sales.source_factory.project_material
    version = {'at':1, 'title':'旧标题'}
    def material(self, key, allow_later):
        return {**original(self, key, allow_later), '_snapshot_at':version['at'], 'title':version['title'],
            'notices':[{'notice_id':1,'title':version['title'],'channel':'采购公告'}]}
    sales.source_factory.project_material = material
    sales.set_state('source_sync', {'synced_at':1})
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key':'p'})
    await sales.run_once()
    first = sales.projects()['items'][0]
    version.update(at=2, title='新标题')
    sales.set_state('source_sync', {'synced_at':2})
    sales.enqueue('analyze', {'project_key':'p'})
    await sales.run_once()
    second = sales.projects()['items'][0]
    assert second['revision'] == first['revision']
    assert second['title'] == second['notices'][0]['title'] == '新标题'
    assert second['matches'][0]['status'] == first['matches'][0]['status']


@pytest.mark.asyncio
async def test_extracted_uncertainty_is_not_promoted_by_a_keyword_in_the_chunk(sales):
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    material = sales.source_factory(sales.settings()).project_material('p', False)
    material['chunks'][0].update(procurement_extracted=True, section_role='uncertain')
    matches = sales.candidates(material, sales.settings(), sales.catalog(sales.settings()))
    assert matches and all(match['status']=='candidate' for match in matches)
    material['chunks'][0].update(section_role='demand', content='|访客管理|1套|')
    matches = sales.candidates(material, sales.settings(), sales.catalog(sales.settings()))
    assert matches[0]['status'] == 'confirmed'


@pytest.mark.asyncio
async def test_semantic_search_extracts_before_embedding_and_labels_candidates(sales):
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    inputs = []
    async def documents(texts):
        inputs.extend(texts)
        return [[1.,0.] for text in texts]
    async def raw_text(text):
        pytest.fail('must not embed the whole raw announcement')
    sales.client().embed_documents = documents
    sales.client().embed_text = raw_text
    text = ('## **2.采购范围**  2.1 采购范围：本项目主要采购电客车继电器备件。  '
            '## **4.采购文件的获取**  4.2.1供应商登陆阳光采购平台（SRM）网站注册账号，下载操作手册。')
    result = await sales.search(text)
    assert inputs and all('SRM' not in value and '操作手册' not in value for value in inputs)
    assert result['extraction']['removed_chars'] > 0
    assert result['result_type'] == 'candidates'
    assert all(item['status']=='candidate' for item in result['items'])
    assert '继电器' in result['items'][0]['matched_text']


@pytest.mark.asyncio
async def test_semantic_search_can_return_empty_instead_of_forcing_top_k(sales):
    sales.enqueue('vectorize', {'ids':[]})
    await sales.run_once()
    async def opposite(texts):
        return [[-1.,0.] for text in texts]
    sales.client().embed_documents = opposite
    result = await sales.search('采购需求：采购访客管理软件。')
    assert result['items'] == [] and result['total'] == 0


@pytest.mark.asyncio
async def test_process_only_semantic_search_does_not_call_embedding(sales):
    async def fail(texts):
        pytest.fail('process text must not reach embedding')
    sales.client().embed_documents = fail
    result = await sales.search('供应商登录阳光采购平台注册账号，并在服务中心下载操作手册。')
    assert result['items'] == [] and result['extraction']['extracted_chars'] == 0


@pytest.mark.asyncio
async def test_lease_recovery_and_history_backpressure(sales):
    sales.enqueue('vectorize', {'ids': []})
    first = sales.claim()
    with sales.store.session.begin() as s:
        row = s.get(SalesJob, first['id'])
        row.lease_until = 0
    second = sales.claim()
    assert second['owner'] != first['owner']
    assert not sales.update_job(first, progress={'bad': True})
    sales.finish(second, 'failed', 'test')
    sales.save_settings({'history_enabled': True})
    await sales.scan_once()
    assert sales.state('history')['cursor'] == 'p'
    await sales.scan_once()
    assert not sales.settings()['history_enabled']


@pytest.mark.asyncio
async def test_completed_vectors_are_not_encoded_again_and_relations_refresh(sales):
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    assert len(sales.client().calls) == 1
    assert (await sales.search('访客授权'))['items'][0]['products'] == ['园区平台']
    item = sales.items()['items'][0]
    sales.import_items([{**item, 'products': ['新平台']}])
    assert (await sales.search('访客授权'))['items'][0]['products'] == ['新平台']


@pytest.mark.asyncio
async def test_changed_material_marks_manual_stale_and_records_history(sales):
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    project = sales.projects()['items'][0]
    sales.review('p', {'sales_item_id': 'visit', 'status': 'excluded', 'reason': '人工确认不相关', 'expected_revision': project['revision']})
    original = sales.source_factory.project_material
    def changed(self, key, allow):
        material = original(self, key, allow)
        material['chunks'][0]['content'] += ' 新增采购要求。'
        material['chunks'][0]['content_hash'] = None
        return material
    sales.source_factory.project_material = changed
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    detail = sales.projects(key='p')['items'][0]
    assert detail['revision'] != project['revision']
    assert detail['matches'][0]['method'] == 'manual_stale'
    assert detail['matches'][0]['status'] == 'candidate'
    assert detail['history']


@pytest.mark.asyncio
async def test_later_stage_metadata_updates_without_analysis_and_preserves_tags(sales):
    sales.enqueue('vectorize', {'ids': []})
    await sales.run_once()
    sales.enqueue('analyze', {'project_key': 'p'})
    await sales.run_once()
    sales.source_factory.list_projects = lambda self, cursor, limit, allow: [{'project_key': 'p', 'channel': '合同公告'}]
    sales.save_settings({'history_enabled': True})
    await sales.scan_once()
    assert sales.projects()['total'] == 0
    detail = sales.projects(key='p')['items'][0]
    assert detail['channel'] == '合同公告' and detail['matches']
    assert len(sales.jobs()['items']) == 2  # metadata refresh did not enqueue a later-stage analysis


@pytest.mark.asyncio
async def test_review_batches_all_evidence_and_cache_and_correction_precedence(sales):
    sales.save_settings({'review_enabled': True, 'review_max_input_chars': 6000})
    seen = []
    async def review(candidates, sources):
        assert len(sources) <= 15
        assert 'evidence' not in candidates[0]  # Do not duplicate full evidence inside the candidate.
        seen.extend(s['key'] for s in sources)
        excluded = any('删除' in s['content'] for s in sources)
        return [{'sales_item_id': 'visit', 'status': 'excluded' if excluded else 'confirmed',
            'reason': '当前采购要求', 'evidence_keys': [s['key'] for s in sources]}]
    sales.client().review = review
    evidence = [{'key': str(i), 'notice_id': 1, 'content': '采购访客管理。' * 50} for i in range(35)]
    evidence.append({'key': 'correction', 'notice_id': 2, 'content': '删除访客管理采购要求。'})
    candidate = {'sales_item_id': 'visit', 'name': '访客管理', 'description': '访客登记授权', 'evidence': evidence}
    await sales.review_candidate(candidate, sales.settings(), 'v1', [2])
    assert set(seen) == {s['key'] for s in evidence}
    assert candidate['status'] == 'excluded'
    before = len(seen)
    candidate['evidence'] = evidence
    await sales.review_candidate(candidate, sales.settings(), 'v1', [2])
    assert len(seen) == before
