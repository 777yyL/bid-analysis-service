"""Resumable cache warmup over local synchronized projects only."""
import asyncio
import time

from sqlalchemy import select

from analyzer.sales_store import SalesProject
from analyzer.sales_sync import SourceSync
from analyzer.source import SourceError
from analyzer.source_tables import SourceProject
from analyzer.store import digest


async def download_batch(service, job, cfg):
    payload = dict(job['payload'])
    snapshot = await asyncio.to_thread(SourceSync(service.store, cfg).status)
    if (not snapshot['ready'] or (not payload.get('batch_id') and snapshot.get('synced_at') != payload['snapshot'])
            or cfg['source_download_url'] != payload['url']):
        raise ValueError('同步快照或分享服务地址已变化，请重新启动材料下载')
    if not cfg['source_download_url']:
        raise ValueError('请先配置文件分享服务地址')
    if payload.get('batch_id'):
        def batch_projects():
            with service.store.session() as session:
                return [{'project_key':key} for key in session.scalars(select(SourceProject.project_key).where(
                    SourceProject.source_scope==digest(cfg['source_dsn']), SourceProject.batch_id==payload['batch_id'],
                    SourceProject.in_scope.is_(True),SourceProject.project_key>payload.get('cursor',''),
                    ~select(SalesProject.project_key).where(SalesProject.project_key==SourceProject.project_key).exists()
                ).order_by(SourceProject.project_key).limit(cfg['source_download_concurrency']))]
        rows = await asyncio.to_thread(batch_projects)
    else:
        rows = await asyncio.to_thread(service.source_factory(cfg).list_projects,
                                      payload.get('cursor', ''), cfg['source_download_concurrency'], True)
    if not rows:
        service.update_job(job, progress={'phase':'download_completed',
            'processed':payload.get('processed', 0), 'errors':payload.get('errors', 0)})
        return True

    def fetch(row):
        source = service.source_factory({**cfg, '_refresh_downloads':True})
        try:
            material = source.project_material(row['project_key'], cfg['allow_later_stages'])
            return material.get('warnings', [])
        except SourceError as error:
            return [str(error)]

    # One bounded wave per lease, then yield so ordinary analysis is not starved.
    results = await asyncio.gather(*(asyncio.to_thread(fetch, row) for row in rows))
    for row, warnings in zip(rows, results, strict=True):
        if warnings:
            payload['errors'] = payload.get('errors', 0) + 1
            payload['recent_errors'] = [*payload.get('recent_errors', []),
                {'project_key':row['project_key'], 'warnings':warnings}][-30:]
            service.update_job(job, progress={'phase':'download_project_warning',
                'project_key':row['project_key'], 'warnings':warnings})
    payload.update(cursor=rows[-1]['project_key'], processed=payload.get('processed', 0)+len(rows))
    service.update_job(job, payload=payload, progress={'phase':'download_batch_completed',
        'processed':payload['processed'], 'errors':payload.get('errors', 0), 'cursor':payload['cursor']},
        status='queued', owner=None, lease_until=None, attempts=max(0, job['attempts']-1),
        available_at=time.time()+1)
    return False
