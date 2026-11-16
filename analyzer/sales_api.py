"""Authenticated sales-capability configuration, retrieval and durable jobs."""
import asyncio
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.routing import APIRoute
from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from analyzer.sales_documents import LocalDocuments
from analyzer.sales_knowledge import identify
from analyzer.sales_models import ModelError
from analyzer.sales_store import SalesItem, SalesJob, record, vector_profile
from analyzer.sales_sync import SourceSync, sync_signature
from analyzer.schemas import StrictModel
from analyzer.source import SourceError
from analyzer.source_tables import SyncBatch, SyncRecord
from analyzer.store import digest


class SalesRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                return await handler(request)
            except ValidationError as error:
                # ValidationError stringification can include supplied API keys or DSNs.
                raise HTTPException(422, '; '.join(str(e['msg']) for e in error.errors(include_input=False))) from None
            except (ModelError, SourceError) as error:
                raise HTTPException(502, str(error)) from None
            except KeyError:
                raise HTTPException(404, '记录不存在') from None
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
        return wrapped


class ImportItems(StrictModel):
    items: list[dict] = Field(min_length=1, max_length=10000)


class VectorizeItems(StrictModel):
    ids: list[str] = Field(default_factory=list, max_length=10000)


class Enabled(StrictModel):
    enabled: bool


class Search(StrictModel):
    text: str = Field(min_length=1, max_length=20000)
    limit: int = Field(default=10, ge=1, le=100)


class StartAnalysis(StrictModel):
    mode: Literal['history', 'incremental'] = 'history'
    project_keys: list[str] = Field(default_factory=list, max_length=500)
    force: bool = False


class StopAnalysis(StrictModel):
    mode: Literal['history', 'incremental'] = 'history'


class TestProject(StrictModel):
    project_key: str = Field(min_length=1, max_length=2000)


def router(service, auth, worker_enabled=True):
    async def query_projects(*args,**kwargs):
        try:
            return await asyncio.to_thread(service.projects,*args,**kwargs)
        except SQLAlchemyError:
            raise HTTPException(503,'数据库查询失败，请检查数据库连接和服务日志') from None
    api = APIRouter(prefix='/api/v3', dependencies=[Depends(auth)], route_class=SalesRoute)

    @api.get('/settings')
    async def settings():
        return service.settings(masked=True)

    @api.put('/settings')
    async def save_settings(payload: dict):
        return service.save_settings(payload)

    @api.post('/source/test')
    async def source_test():
        return await asyncio.to_thread(SourceSync(service.store, service.settings()).check)

    @api.post('/source/files/test')
    async def files_test(payload: TestProject):
        cfg = service.settings()
        await asyncio.to_thread(service.require_snapshot, cfg)
        material = await asyncio.to_thread(service.source_factory({**cfg, '_refresh_downloads':True}).project_material,
                                          payload.project_key, cfg['allow_later_stages'])
        return {'status':material['status'], 'documents':len(material.get('documents', [])),
                'attachments':len(material.get('attachments', [])), 'warnings':material.get('warnings', []),
                'storage':material.get('material_storage')}

    @api.post('/source/files/download')
    async def download_files():
        if not worker_enabled:
            raise HTTPException(409, '后台 worker 未启动，无法执行下载')
        cfg = service.settings()
        await asyncio.to_thread(service.require_snapshot, cfg)
        if not cfg['source_download_url']:
            raise HTTPException(409, '请先配置文件分享服务地址')
        return service.enqueue_download(cfg, SourceSync(service.store, cfg).status())

    @api.get('/source/files/status')
    def files_status():
        state = service.state('download_source_job', {})
        with service.store.session() as session:
            row = session.get(SalesJob, state.get('id', ''))
            return record(row) if row else None

    @api.get('/source/sync/status')
    async def sync_status():
        cfg = service.settings()
        status = SourceSync(service.store, cfg).status()
        with service.store.session() as session:
            row = session.scalar(select(SalesJob).where(SalesJob.kind == 'sync_source')
                .order_by(SalesJob.created_at.desc()).limit(1))
            status['job'] = record(row) if row else None
        return status

    @api.post('/source/sync')
    async def sync_source():
        if not worker_enabled:
            raise HTTPException(409, '后台 worker 未启动，无法执行同步')
        cfg = service.settings()
        if service.store.engine.dialect.name != 'postgresql':
            raise HTTPException(409, '本地来源表需要 PostgreSQL 分析库')
        if not cfg['source_dsn']:
            raise HTTPException(409, '请先保存主服务数据库连接')
        return service.enqueue('sync_source', {'signature': sync_signature(cfg)},
            digest(['sync_source', sync_signature(cfg)]))

    @api.get('/source/sync/batches')
    def sync_batches(page: int=Query(1,ge=1),page_size: int=Query(20,ge=1,le=100)):
        from sqlalchemy import func
        scope=digest(service.settings()['source_dsn'])
        with service.store.session() as session:
            query=select(SyncBatch).where(SyncBatch.source_scope==scope)
            total=session.scalar(select(func.count()).select_from(query.subquery()))
            rows=session.scalars(query.order_by(SyncBatch.started_at.desc()).offset((page-1)*page_size).limit(page_size))
            return {'total':total,'items':[record(row) for row in rows]}

    @api.get('/source/sync/batches/{batch_id}/records')
    def sync_records(batch_id: str,page: int=Query(1,ge=1),page_size: int=Query(30,ge=1,le=100)):
        from sqlalchemy import func
        with service.store.session() as session:
            batch=session.get(SyncBatch,batch_id)
            if not batch or batch.source_scope!=digest(service.settings()['source_dsn']):
                raise HTTPException(404,'批次不存在')
            query=select(SyncRecord).where(SyncRecord.batch_id==batch_id)
            total=session.scalar(select(func.count()).select_from(query.subquery()))
            rows=session.scalars(query.order_by(SyncRecord.kind,SyncRecord.source_id).offset((page-1)*page_size).limit(page_size))
            return {'total':total,'items':[record(row) for row in rows]}

    @api.post('/embedding/test')
    async def embedding_test():
        cfg = service.settings()
        vectors = await service.client(cfg).embed_sales([{'id': 'test', 'name': '访客管理', 'description': '访客预约、登记及通行授权'}])
        dimensions = len(vectors[0])
        return {'dimensions': dimensions, 'storage': 'local', 'ok': True}

    @api.post('/knowledge/test')
    async def knowledge_test(payload: Search):
        cfg = service.settings()
        chunks = await service.knowledge_client(cfg).retrieve(payload.text)
        with service.store.session() as session:
            entries = [{'id':r.id,'name':r.name} for r in session.scalars(select(SalesItem).where(SalesItem.enabled.is_(True)))]
        items = [{**chunk,'sales_item_id':identify(chunk['chunkContent'],entries)} for chunk in chunks]
        return {'ok':True,'total':len(items),'items':items,
                'unresolved':sum(item['sales_item_id'] is None for item in items)}

    @api.post('/review/test')
    async def review_test():
        cfg = service.settings()
        if not cfg['review_enabled']:
            raise HTTPException(409, '请先开启并配置模型复核')
        result = await service.client(cfg).review([{'sales_item_id': 'test', 'name': '访客管理',
            'description': '访客登记及授权'}], [{'key': 'test:1', 'content': '本次采购访客管理软件，实现访客登记及通行授权。'}])
        return {'ok': True, 'result': result}

    @api.get('/review/history/preview')
    async def review_history_preview():
        from analyzer.sales_history_review import preview
        return await asyncio.to_thread(preview,service,service.settings())

    @api.post('/review/history/start')
    async def review_history_start():
        require_worker()
        cfg=service.settings()
        if not cfg['review_enabled'] or not cfg['review_base_url'] or not cfg['review_model']:
            raise HTTPException(409,'请先开启模型复核并配置接口和模型')
        job=service.enqueue('review_history',{'threshold':cfg['review_score_threshold'],'cursor':'',
            'enqueued':0,'source_scope':digest(cfg['source_dsn'])},digest(['review_history']))
        service.set_state('history_review_job',{'id':job['id']})
        return job

    @api.get('/review/history/status')
    def review_history_status():
        state=service.state('history_review_job',{})
        with service.store.session() as session:
            row=session.get(SalesJob,state.get('id',''))
            return record(row) if row else None

    @api.get('/overview')
    async def overview():
        return await asyncio.to_thread(service.overview)

    @api.get('/views/status')
    async def view_status():
        return service.state('view_status', {})

    @api.post('/views/refresh')
    async def refresh_view():
        require_worker()
        if service.store.engine.dialect.name != 'postgresql':
            raise HTTPException(409, '物化视图仅支持 PostgreSQL；本地请使用项目查询页面')
        return service.enqueue('refresh_view', {})

    @api.get('/sales-items')
    async def items(q: str = '', enabled: bool | None = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=1000)):
        return service.items(q, enabled, page, page_size)

    @api.post('/sales-items/import')
    async def import_items(payload: ImportItems):
        return service.import_items(payload.items)

    @api.put('/sales-items/{sid}')
    async def edit_item(sid: str, payload: dict):
        with service.store.session() as s:
            row = s.get(SalesItem, sid)
            if not row:
                raise HTTPException(404, '销售项不存在')
            existing = record(row)
        existing.update(payload, id=sid)
        return service.import_items([existing])

    @api.post('/sales-items/{sid}/enabled')
    async def enabled_item(sid: str, payload: Enabled):
        service.set_enabled(sid, payload.enabled)
        return {'id': sid, 'enabled': payload.enabled}

    @api.post('/sales-items/vectorize')
    async def vectorize(payload: VectorizeItems):
        require_worker()
        return service.enqueue('vectorize', payload.model_dump(), digest(['vectorize', payload.ids, vector_profile(service.settings())]))

    @api.post('/search')
    async def search(payload: Search):
        return await service.search(payload.text, payload.limit)

    @api.post('/sales-items/{sid}/search')
    async def reverse_search(sid: str):
        if service.settings()['retrieval_backend']=='maas':
            raise HTTPException(409,'知识库模式请使用采购需求检索或单项目测试；反向公告向量检索仅支持本地模式')
        cfg = service.settings()
        with service.store.session() as s:
            row = s.get(SalesItem, sid)
            if not row:
                raise HTTPException(404, '销售项不存在')
            if not row.enabled or service.item_json(row, cfg)['vector_status'] != 'ready':
                raise HTTPException(409, '请先启用并完成当前版本向量化')
            vector = row.vector
        results = await asyncio.to_thread(LocalDocuments(service.store, cfg).search, vector, 20)
        return {'items': [{k: v for k, v in row.items() if k != 'embedding'} | {
            'score': row.get('similarity'), 'title': row.get('notice_title', '')} for row in results]}

    @api.get('/jobs')
    async def jobs():
        return await asyncio.to_thread(service.jobs)

    @api.get('/jobs/{jid}')
    def job_status(jid: str):
        with service.store.session() as session:
            row = session.get(SalesJob, jid)
            if not row:
                raise HTTPException(404, '任务不存在')
            return record(row)

    @api.post('/analysis/test')
    async def test_project(payload: TestProject):
        require_worker()
        cfg = service.settings()
        key = payload.project_key.strip()
        if not key:
            raise HTTPException(422, '请输入项目标识 project_key')
        if cfg['paused']:
            raise HTTPException(409, '调度已暂停，请先取消暂停；无需开启存量或增量扫描')
        if not cfg['source_dsn'] or not (cfg['source_file_root'] or cfg['source_download_url']):
            raise HTTPException(409, '请先配置主库连接，以及文件分享地址或本地文本根目录')
        await asyncio.to_thread(service.require_snapshot, cfg)
        await asyncio.to_thread(service.catalog, cfg)
        job = service.enqueue('analyze', {'project_key': key, 'force': True, 'test': True})
        with service.store.session.begin() as session:
            row = session.get(SalesJob, job['id'], with_for_update=True)
            if row and row.status in ('queued', 'retry'):
                row.payload = {**row.payload, 'force': True, 'test': True}
                job = record(row)
        return job

    @api.post('/jobs/{jid}/retry')
    async def retry(jid: str):
        require_worker()
        with service.store.session.begin() as s:
            row = s.get(SalesJob, jid, with_for_update=True)
            if not row:
                raise HTTPException(404, '任务不存在')
            if row.status not in ('failed', 'skipped'):
                raise HTTPException(409, '仅失败或跳过任务可以重试')
            if row.kind in ('analyze', 'review_existing'):
                active_key = digest(['analyze', row.payload['project_key']])
            elif row.kind == 'review_history':
                active_key = digest(['review_history'])
            elif row.kind == 'download_source':
                active_key = digest(['download_source', row.payload['snapshot'], row.payload['url']])
            elif row.kind == 'sync_source':
                active_key = digest(['sync_source', row.payload['signature']])
            elif row.kind == 'refresh_view':
                active_key = digest(['refresh_view'])
            else:
                active_key = digest(['vectorize', row.payload.get('ids', []), vector_profile(service.settings())])
            if s.scalar(select(SalesJob.id).where(SalesJob.active_key == active_key, SalesJob.id != jid)):
                raise HTTPException(409, '已有相同项目或向量化任务排队／执行，无需重复重试')
            row.status, row.attempts, row.available_at = 'queued', 0, time.time()
            row.error, row.active_key = None, active_key
        return {'id': jid, 'status': 'queued'}

    @api.post('/analysis/start')
    async def start(payload: StartAnalysis):
        require_worker()
        cfg = service.settings()
        if not cfg['source_dsn']:
            raise HTTPException(409, '请先在系统配置中保存并测试主服务数据库连接')
        if payload.mode!='incremental':
            await asyncio.to_thread(service.require_snapshot, cfg)
        await asyncio.to_thread(service.catalog, cfg)
        if payload.project_keys:
            return {'items': [service.enqueue('analyze', {'project_key': key, 'force': payload.force}, digest(['analyze', key])) for key in payload.project_keys]}
        service.save_settings({payload.mode + '_enabled': True})
        if payload.mode=='incremental':
            service.set_state('source_schedule:'+sync_signature(cfg), {'next_sync_at':0})
        if payload.force:
            service.set_state('history', {'cursor': '', 'processed': 0, 'phase': 'waiting'})
        return {'enabled': True, 'mode': payload.mode}

    @api.post('/analysis/stop')
    async def stop(payload: StopAnalysis):
        service.save_settings({payload.mode + '_enabled': False})
        return {'enabled': False, 'mode': payload.mode}

    @api.get('/projects/metrics')
    async def project_metrics():
        try:
            return await asyncio.to_thread(service.project_metrics)
        except SQLAlchemyError:
            raise HTTPException(503, '项目统计查询失败，请检查分析库连接及日志') from None

    @api.get('/projects')
    async def projects(q: str = '', category: str | None = None, sales_item_id: str | None = None,
                       match_mode: Literal['any', 'all'] = 'any', page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                       product: str | None = None, hit: bool | None = None,
                       min_score: float | None = Query(None,ge=0,le=1)):
        return await query_projects(q,category,[v for v in (sales_item_id or '').split(',') if v],match_mode,page,page_size,product=product,hit=hit,min_score=min_score)

    @api.get('/filters/options')
    def filter_options():
        with service.store.session() as session:
            rows=session.execute(select(SalesItem.id,SalesItem.name,SalesItem.categories,SalesItem.products).where(SalesItem.enabled.is_(True))).all()
        return {'categories':sorted({v for r in rows for v in r.categories}),
                'products':sorted({v for r in rows for v in r.products}),
                'sales_items':[{'id':r.id,'name':r.name,'categories':r.categories} for r in rows]}

    @api.post('/analysis/body')
    async def project_body(payload: TestProject):
        cfg=service.settings()
        material=await asyncio.to_thread(service.source_factory(cfg).project_material,payload.project_key,cfg['allow_later_stages'])
        return {'documents':material.get('documents',[]),'warnings':material.get('warnings',[]),
                'status':material.get('status'),'source':'current_files'}

    @api.get('/projects/{key:path}')
    async def project(key: str):
        result = await query_projects(key=key)
        if not result['items']:
            raise HTTPException(404, '项目尚未进行销售项匹配')
        return result['items'][0]

    @api.post('/projects/{key:path}/review')
    async def review(key: str, payload: dict):
        service.review(key, payload)
        result = await asyncio.to_thread(service.projects,key=key)
        return result['items'][0]

    def require_worker():
        if not worker_enabled:
            raise HTTPException(409, '后台 worker 未启动，请检查部署引导配置')
        if not service.enabled():
            raise HTTPException(409, '请在系统配置页面启用销售项匹配引擎')

    return api
