import copy

import pytest
from sqlalchemy import func, select

from analyzer.sales_documents import LocalDocuments
from analyzer.sales_models import RuntimeSettings
from analyzer.sales_store import SalesDocumentChunk, SalesProject
from analyzer.source import SourceError
from analyzer.store import Store, digest


@pytest.fixture
def local(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'local.db'}")
    store.initialize()
    cfg = RuntimeSettings(source_dsn='postgresql://read-only/source', embedding_model='m',
        embedding_dimensions=2, source_chunk_chars=200, embedding_batch_size=2).model_dump()
    yield store, cfg
    store.close()


def material():
    return {'project_key': 'p', 'status': 'ready', 'documents': [
        {'notice_id': 1, 'source_type': 'notice', 'attachment_id': None,
         'chunk_title': '采购公告', 'content': '采购需求：访客预约管理软件' * 30},
        {'notice_id': 1, 'source_type': 'attachment', 'attachment_id': 3,
         'chunk_title': '附件', 'content': '访客审批能力'}]}


class Model:
    def __init__(self):
        self.calls = []
    async def embed_documents(self, texts):
        self.calls.extend(texts)
        return [[1., 0.] for text in texts]


@pytest.mark.asyncio
async def test_disabled_attachments_body_cache_and_changed_content(local):
    store, cfg = local
    cfg['collect_attachments'] = False
    model = Model()
    docs = LocalDocuments(store, cfg)
    first = await docs.prepare(material(), model, lambda p: None, 2)
    assert first['material_storage'] == 'local'
    assert all(c['source_type'] == 'notice' for c in first['chunks'])
    calls = len(model.calls)
    second = await docs.prepare(material(), model, lambda p: None, 2)
    assert len(model.calls) == calls
    assert second['vectorization']['generated'] == 0
    assert first['local_chunk_ids'] == second['local_chunk_ids']
    changed = material()
    changed['documents'][0]['content'] += '新采购要求'
    third = await docs.prepare(changed, model, lambda p: None, 2)
    assert third['local_chunk_ids'] != first['local_chunk_ids']
    assert len(model.calls) > calls
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(SalesDocumentChunk)) > 0


@pytest.mark.asyncio
async def test_attachment_toggle_and_model_profile_isolation(local):
    store, cfg = local
    cfg['collect_attachments'] = True
    model = Model()
    result = await LocalDocuments(store, cfg).prepare(material(), model, lambda p: None, 2)
    assert result['vectorization']['attachment_documents'] == 1
    calls = len(model.calls)
    cfg['embedding_document_prefix'] = 'new prefix'
    updated = await LocalDocuments(store, cfg).prepare(material(), model, lambda p: None, 2)
    assert len(model.calls) > calls
    assert set(updated['local_chunk_ids']).isdisjoint(result['local_chunk_ids'])


@pytest.mark.asyncio
async def test_dimension_failure_does_not_persist_vectors(local):
    store, cfg = local
    with pytest.raises(SourceError, match='维度'):
        await LocalDocuments(store, cfg).prepare(material(), Model(), lambda p: None, 3)
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(SalesDocumentChunk)) == 0


@pytest.mark.asyncio
async def test_local_reverse_search_only_current_manifest_and_attachment_policy(local):
    store, cfg = local
    cfg['collect_attachments'] = True
    docs = LocalDocuments(store, cfg)
    result = await docs.prepare(material(), Model(), lambda p: None, 2)
    snapshot = {k:v for k,v in result.items() if k!='chunks'}
    snapshot['_source_revision'] = digest(cfg['source_dsn'])
    with store.session.begin() as session:
        session.add(SalesProject(project_key='p', title='项目', channel='采购公告', status='matched',
            revision='r', material=snapshot, updated_at=1))
    assert any(r['source_type']=='attachment' for r in docs.search([1,0]))
    cfg = copy.copy(cfg)
    cfg['collect_attachments'] = False
    assert all(r['source_type']=='notice' for r in LocalDocuments(store, cfg).search([1,0]))


@pytest.mark.asyncio
async def test_only_extracted_requirements_are_sent_to_embedding_with_original_offsets(local):
    store, cfg = local
    raw = '一、采购需求\n访客管理提供预约、审批和签离。\n二、供应商资格要求\n须提供视频监控项目业绩。\n三、开标\n采用视频会议远程开标。'
    source = {'project_key':'p','status':'ready','documents':[
        {'notice_id':1,'source_type':'notice','content':raw,'chunk_title':'采购'}]}
    model = Model()
    result = await LocalDocuments(store, cfg).prepare(source, model, lambda p:None, 2)
    assert model.calls and all('开标' not in text and '视频监控' not in text for text in model.calls)
    assert any('访客管理' in text for text in model.calls)
    assert result['procurement_extraction']['removed_chars'] > 0
    for chunk in result['chunks']:
        assert chunk['content'] == raw[chunk['char_start']:chunk['char_end']]
        assert chunk['procurement_extracted'] is True


@pytest.mark.asyncio
async def test_no_requirement_never_embeds_full_process_text(local):
    store, cfg = local
    source = {'project_key':'p','status':'ready','documents':[
        {'notice_id':1,'source_type':'notice','content':'供应商须自备电脑参加远程开标。投标文件通过电子交易平台上传。'}]}
    model = Model()
    result = await LocalDocuments(store, cfg).prepare(source, model, lambda p:None, 2)
    assert not model.calls
    assert result['status'] == 'no_procurement_demand' and not result['chunks']
    assert result['procurement_extraction']['extracted_chars'] == 0
