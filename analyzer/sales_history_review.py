"""Review persisted low-score matches without reading source files or re-retrieving."""
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from analyzer.sales_store import SalesAudit, SalesItem, SalesJob, SalesMatch, SalesProject, record
from analyzer.store import digest

REVIEWED = ('manual','manual_stale','model','model_auto','model_backfill')


def eligible(cfg,threshold):
    return select(SalesProject.project_key,SalesProject.revision).join(
        SalesMatch,SalesMatch.project_key==SalesProject.project_key).join(
        SalesItem,SalesItem.id==SalesMatch.sales_item_id).where(
        SalesProject.material['_source_revision'].as_string()==digest(cfg['source_dsn']),
        SalesProject.status!='stage_filtered',SalesMatch.revision==SalesProject.revision,
        SalesItem.enabled.is_(True),SalesMatch.item_version==SalesItem.version,
        SalesMatch.status.in_(['confirmed','candidate']),SalesMatch.method.not_in(REVIEWED),
        SalesMatch.score<threshold)


def preview(service,cfg):
    with service.store.session() as session:
        query=eligible(cfg,cfg['review_score_threshold'])
        return {'projects':session.scalar(select(func.count()).select_from(query.distinct().subquery())),
                'matches':session.scalar(select(func.count()).select_from(query.subquery())),
                'threshold':cfg['review_score_threshold']}


def scan(service,job,cfg):
    payload=dict(job['payload'])
    if payload['source_scope']!=digest(cfg['source_dsn']):
        raise ValueError('数据源已变化，请重新启动历史低分复核')
    with service.store.session() as session:
        pending=session.scalar(select(func.count()).select_from(SalesJob).where(SalesJob.status.in_(['queued','retry','running'])))
        rows=[] if pending>=200 else session.execute(eligible(cfg,payload['threshold']).where(
            SalesProject.project_key>payload.get('cursor','')).distinct().order_by(SalesProject.project_key).limit(20)).all()
    cursor=payload.get('cursor','')
    enqueued=payload.get('enqueued',0)
    blocked=pending>=200
    for key,revision in rows:
        active=digest(['analyze',key])
        try:
            child=service.enqueue('review_existing',{'project_key':key,'revision':revision,
                'threshold':payload['threshold'],'batch_id':job['id'],'source_scope':payload['source_scope']},active)
        except IntegrityError:
            blocked=True
            break
        if child['kind']!='review_existing' or child['payload'].get('batch_id')!=job['id']:
            blocked=True
            break
        cursor=key
        enqueued+=1
    if not rows and not blocked:
        service.update_job(job,progress={'phase':'review_scan_completed','enqueued':enqueued})
        return True
    payload.update(cursor=cursor,enqueued=enqueued)
    service.update_job(job,payload=payload,progress={'phase':'review_scan_waiting' if blocked else 'review_batch_queued',
        'enqueued':enqueued,'cursor':cursor},status='queued',owner=None,lease_until=None,
        attempts=max(0,job['attempts']-1),available_at=time.time()+2)
    return False


async def review_project(service,job,cfg):
    if not cfg['review_enabled'] or not cfg['review_base_url'] or not cfg['review_model']:
        raise ValueError('历史低分复核需要开启并配置模型复核')
    payload=job['payload']
    if payload['source_scope']!=digest(cfg['source_dsn']):
        raise ValueError('数据源已变化，请重新启动历史低分复核')
    with service.store.session() as session:
        project=session.get(SalesProject,payload['project_key'])
        if not project or project.revision!=payload['revision']:
            raise ValueError('项目版本已变化，旧历史复核任务未采纳，请重新启动复核')
        if project.material.get('_source_revision')!=payload['source_scope']:
            raise ValueError('项目数据源已变化，复核未采纳')
        material=dict(project.material)
        rows=session.execute(select(SalesMatch,SalesItem).join(SalesItem,SalesItem.id==SalesMatch.sales_item_id).where(
            SalesMatch.project_key==project.project_key,SalesMatch.revision==project.revision,
            SalesMatch.score<payload['threshold'],SalesMatch.status.in_(['confirmed','candidate']),
            SalesMatch.method.not_in(REVIEWED),SalesItem.enabled.is_(True),SalesMatch.item_version==SalesItem.version)).all()
        targets=[(record(match),{'sales_item_id':item.id,'name':item.name,'description':item.description,
            'status':'candidate','method':'model','reason':'历史低分模型复核','evidence':match.evidence}) for match,item in rows]
    for index,(old,candidate) in enumerate(targets):
        service.update_job(job,progress={'phase':'historical_model_review','sales_item':candidate['name'],
            'processed':index,'total':len(targets)})
        if not candidate['evidence']:
            service.update_job(job,progress={'phase':'historical_missing_evidence','sales_item':candidate['name']})
            continue
        await service.review_candidate(candidate,cfg,payload['revision'],material.get('correction_notice_ids',[]),material.get('units'))
        with service.store.session.begin() as session:
            owned=session.scalar(select(SalesJob.id).where(SalesJob.id==job['id'],SalesJob.owner==job['owner'],
                SalesJob.status=='running').with_for_update())
            current=session.get(SalesProject,payload['project_key'],with_for_update=True)
            match=session.get(SalesMatch,old['id'],with_for_update=True)
            item=session.get(SalesItem,old['sales_item_id'])
            if not owned or not current or current.revision!=payload['revision']:
                raise ValueError('项目版本或任务执行权变化，历史复核结果未保存')
            if not match or not item or not item.enabled or item.version!=old['item_version'] or any(
                getattr(match,k)!=old[k] for k in ('revision','item_version','method','status','reason','score')):
                continue
            session.add(SalesAudit(id=str(uuid.uuid4()),project_key=current.project_key,
                payload={'event':'historical_low_score_review','previous':old,'result':candidate},created_at=time.time()))
            match.status=candidate['status']
            match.reason=candidate['reason']
            match.evidence=candidate['evidence']
            match.method='model_backfill'
            if cfg['manual_confirmation_enabled'] and match.status=='confirmed':
                match.status='candidate'
                match.reason+='；等待人工确认'
            current.updated_at=time.time()
            session.flush()
            service.update_project_status(session,current)
    service.update_job(job,progress={'phase':'historical_review_completed','processed':len(targets),'total':len(targets)})
