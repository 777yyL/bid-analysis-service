"""Shared database mode: read public sources, write only isolated result tables."""
from contextlib import contextmanager
from time import time

from sqlalchemy import select, func
from sqlalchemy.exc import SQLAlchemyError

from analyzer.sales_source import SalesSource
from analyzer.sales_store import SalesJob, SalesProject
from analyzer.source import SourceError
from analyzer.store import digest


class Connection:
    def __init__(self, conn):
        self.conn=conn

    def execute(self, sql, params=()):
        return self.conn.exec_driver_sql(sql,params).mappings()


class SharedSource(SalesSource):
    def __init__(self,cfg,store):
        super().__init__(cfg)
        self.store=store
        self.upstream_only=True

    @contextmanager
    def _connection(self):
        try:
            with self.store.engine.connect() as conn:
                conn.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                yield Connection(conn)
        except SQLAlchemyError:
            raise SourceError('同库只读查询失败，请检查 public.notices、mv_project、attachments 的字段与读取权限',retryable=True) from None

    def list_projects(self,after_key='',limit=20,allow_later=False):
        prefix=f'"{self.store.schema}".'
        sql=f'''SELECT m.project_key,m.channel FROM public.mv_project m
            WHERE m.project_key>%s AND m.channel=ANY(%s)
            AND NOT EXISTS (SELECT 1 FROM {prefix}sales_projects_v1 p WHERE p.project_key=m.project_key)
            AND EXISTS (SELECT 1 FROM public.notices n WHERE n.project_key_name=m.project_key
                AND n.revoke_status='active' AND n.channel=ANY(%s)
                AND n.has_procurement_content=true AND length(trim(n.procurement_content))>0)
            ORDER BY m.project_key LIMIT %s'''
        early=['采购公告','采购意向','更正公告']
        stages=early+['结果公告','合同公告','验收公告','废标终止','其他'] if allow_later else early
        with self._connection() as conn:
            return [dict(r) for r in conn.execute(sql,(after_key,stages,early,limit)).fetchall()]


async def scan_shared(service,cfg):
    import asyncio
    state=service.state('shared_scan',{})
    if state.get('next_scan',0)>time():
        return
    with service.store.session() as session:
        queued=session.scalar(select(func.count()).select_from(SalesJob).where(
            SalesJob.kind.in_(['analyze','review_existing']),SalesJob.status.in_(['queued','running','retry'])))
    if queued>=200:
        return
    source=service.source_factory(cfg)
    rows=await asyncio.to_thread(source.list_projects,state.get('cursor',''),min(cfg['project_batch_size'],200-queued),cfg['allow_later_stages'])
    for row in rows:
        key=row['project_key']
        with service.store.session() as session:
            previous=session.scalar(select(SalesJob.id).where(SalesJob.kind=='analyze',
                SalesJob.payload['project_key'].as_string()==key,SalesJob.status.in_(['queued','running','retry','failed'])).limit(1))
        if previous:
            continue
        material=await asyncio.to_thread(source.project_material,key,cfg['allow_later_stages'])
        if material['status']=='ready':
            service.enqueue('analyze',{'project_key':key,'preserve_existing':True},digest(['analyze',key]))
    service.set_state('shared_scan',{'cursor':rows[-1]['project_key'] if rows else '',
        'checked_at':time(),'next_scan':0 if rows else time()+cfg['poll_seconds'],
        'processed':state.get('processed',0)+len(rows),'phase':'discovering' if rows else 'caught_up'})


def initialize_shared(service):
    """No source DDL or destructive cleanup. Existing results and legacy tables remain."""
    prefix=f'"{service.store.schema}".'
    with service.store.engine.begin() as conn:
        conn.exec_driver_sql(f'''ALTER TABLE {prefix}sales_matches_v1
            ADD COLUMN IF NOT EXISTS updated_at double precision NOT NULL DEFAULT extract(epoch FROM clock_timestamp())''')
        conn.exec_driver_sql(f'''CREATE OR REPLACE VIEW {prefix}v_project_sales_tags AS
            SELECT p.project_key,m.sales_item_id,i.name,i.description,i.categories,i.products,
                m.score,m.method,m.reason,m.evidence,to_timestamp(m.updated_at) AS updated_at
            FROM {prefix}sales_projects_v1 p JOIN {prefix}sales_matches_v1 m
              ON m.project_key=p.project_key AND m.revision=p.revision
            JOIN {prefix}sales_items_v1 i ON i.id=m.sales_item_id AND i.version=m.item_version
            WHERE m.status='confirmed' AND i.enabled=true''')
