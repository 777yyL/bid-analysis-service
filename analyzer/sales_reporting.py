"""Read-only arrival cohorts. No task creation, replication, or inferred zeroes."""
from collections import defaultdict
from datetime import datetime, time as daytime, timedelta
from threading import Lock
from time import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from analyzer.project_selection import ANALYSIS_STAGES, select_project
from analyzer.sales_source import SalesSource
from analyzer.sales_store import SalesItem, SalesJob, SalesMatch, SalesProject, vector_profile
from analyzer.schemas import NoticeInput
from analyzer.source import SourceError
from analyzer.store import digest


def day_bounds(day, column_type, timezone):
    start = datetime.combine(day, daytime.min, ZoneInfo(timezone))
    end = start + timedelta(days=1)
    if column_type == 'timestamp without time zone':
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
    return start, end


def classify_demand(key, rows):
    notices = [NoticeInput(notice_id=r['notice_id'], title=r['title'] or key,
        project_key=key, notice_type=r['channel'] or '其他',
        revoke_status=r['revoke_status'], publish_at=str(r['publish_at']) if r.get('publish_at') else None,
        body=r.get('procurement_content') or '',
        **{k:r[k] for k in ('round_key','lot_key','related_notice_id') if r.get(k) is not None}) for r in rows]
    plans = select_project(notices)
    chosen = {n.notice_id for p in plans if p['main'] for n in [p['main'], *p['corrections']]}
    mains = [p['main'].notice_id for p in plans if p['main']]
    result = {'main_notice_ids':mains, 'main_notice_id':mains[0] if mains else None,
              'correction_notice_ids':sorted(chosen-set(mains))}
    if not mains:
        return {**result, 'demand':'missing_primary'}
    values = [r for r in rows if r['notice_id'] in chosen]
    if any(r.get('has_procurement_content') is None or (
            r.get('has_procurement_content') is True and not r.get('has_content')) for r in values):
        return {**result, 'demand':'pending'}
    return {**result, 'demand':'yes' if any(r['has_procurement_content'] for r in values) else 'no'}


def classify_progress(demand, status, job):
    if job == 'running':
        return 'running'
    if job in ('queued','retry'):
        return 'pending'
    if job == 'failed':
        return 'failed'
    if demand in ('out','no'):
        return 'skipped'
    if demand == 'pending':
        return 'awaiting'
    if demand == 'missing_primary':
        return 'missing_primary'
    if status in ('matched','no_match'):
        return 'completed'
    if status == 'needs_review':
        return 'review_pending'
    if status == 'missing_material':
        return 'missing_material'
    return 'pending'


SLICES = ('all','new','existing','eligible','out','demand','unextracted','no_demand',
          'missing_primary','analyzed','matched','no_match','pending','running','failed','review_pending','missing_material')


def in_slice(p, name):
    return {'all':True, 'new':p['is_new'], 'existing':not p['is_new'],
        'eligible':p['demand']!='out', 'out':p['demand']=='out', 'demand':p['demand']=='yes',
        'unextracted':p['demand']=='pending', 'no_demand':p['demand']=='no',
        'missing_primary':p['demand']=='missing_primary', 'analyzed':p['progress']=='completed',
        'matched':p['hit'], 'no_match':p['progress']=='completed' and not p['hit'],
        **{v:p['progress']==v for v in ('pending','running','failed','review_pending','missing_material')}}[name]


def summarize(rows):
    return {'notices':sum(p['notices'] for p in rows),
            **{name:sum(in_slice(p,name) for p in rows) for name in SLICES}}


class DailyReports:
    def __init__(self, service):
        self.service = service
        self.source_factory = SalesSource
        self.cache = {}
        self.lock = Lock()

    def load(self, day, refresh=False, time_basis='first_seen_at'):
        if time_basis not in ('first_seen_at', 'publish_at'):
            raise SourceError('仅支持首次入库时间或公告发布时间')
        cfg = self.service.settings()
        key = (digest([cfg['source_dsn'],cfg['allow_later_stages'],cfg['source_timezone']]), str(day), time_basis)
        with self.lock:
            cached = self.cache.get(key)
            if not refresh and cached and time()-cached['generated_at']<30:
                return cached
            result = self._read(day, cfg, time_basis)
            self.cache[key] = result
            while len(self.cache)>8:
                del self.cache[next(iter(self.cache))]
            return result

    def _read(self, day, cfg, time_basis='first_seen_at'):
        by_project, arrivals, first, channels = defaultdict(list), {}, {}, {}
        source = self.source_factory(cfg)
        with source._connection() as conn:
            columns = {r['column_name']:r['data_type'] for r in conn.execute("""SELECT column_name,data_type
                FROM information_schema.columns WHERE table_schema='public' AND table_name='notices'""").fetchall()}
            required = {'first_seen_at','notice_id','project_key_name','channel','title','revoke_status',
                        'has_procurement_content','procurement_content','publish_at'}
            if not required <= columns.keys():
                raise SourceError('每日统计需要主库 notices 字段：'+', '.join(sorted(required-columns.keys())))
            if any(columns[k] not in ('timestamp with time zone','timestamp without time zone')
                   for k in ('first_seen_at', time_basis)):
                raise SourceError('所选时间字段必须是 timestamp 类型，不能推测日期')
            start, end = day_bounds(day, columns[time_basis], cfg['source_timezone'])
            first_start, first_end = day_bounds(day, columns['first_seen_at'], cfg['source_timezone'])
            # time_basis is an enum checked at load(), never caller-supplied SQL.
            if time_basis not in ('first_seen_at', 'publish_at'):
                raise SourceError('时间字段无效')
            rows = conn.execute(f'''SELECT project_key_name, count(*) AS notices
                FROM public.notices WHERE {time_basis} >= %s AND {time_basis} < %s
                GROUP BY project_key_name''',(start,end)).fetchall()
            unlinked = sum(r['notices'] for r in rows if not r['project_key_name'])
            arrivals = {r['project_key_name']:r['notices'] for r in rows if r['project_key_name']}
            if len(arrivals)>20000:
                raise SourceError('当日涉及项目超过 20000，未截断统计；请检查主库项目关联或改用离线统计')
            keys=list(arrivals)
            for offset in range(0,len(keys),1000):
                batch=keys[offset:offset+1000]
                optional=', '.join(k if k in columns else 'NULL AS '+k for k in ('round_key','lot_key','related_notice_id'))
                region='region_code' if 'region_code' in columns else 'NULL AS region_code'
                records=conn.execute(f'''SELECT notice_id, project_key_name, title, channel, revoke_status, {region},
                    publish_at, has_procurement_content, length(trim(coalesce(procurement_content,'')))>0 AS has_content,
                    CASE WHEN channel='更正公告' THEN procurement_content ELSE '' END AS procurement_content,
                    {optional} FROM public.notices WHERE project_key_name=ANY(%s)
                    ORDER BY notice_id LIMIT 100001''',(batch,)).fetchall()
                if len(records)>100000:
                    raise SourceError('项目关联公告量异常，未截断统计')
                for r in records:
                    by_project[r['project_key_name']].append(dict(r))
                for r in conn.execute('''SELECT project_key_name, min(first_seen_at) AS first_at
                    FROM public.notices WHERE project_key_name=ANY(%s) GROUP BY project_key_name''',(batch,)).fetchall():
                    first[r['project_key_name']]=r['first_at']
                for r in conn.execute('SELECT project_key,channel FROM public.mv_project WHERE project_key=ANY(%s)',(batch,)).fetchall():
                    channels[r['project_key']]=r['channel'] or ''
        result=[]
        for key in arrivals:
            notices=by_project[key]
            info=classify_demand(key,notices)
            channel=channels.get(key,'')
            if not channel or not cfg['allow_later_stages'] and channel not in ANALYSIS_STAGES:
                info['demand']='out'
            chosen=next((n for n in notices if n['notice_id']==info['main_notice_id']),notices[-1] if notices else {})
            result.append({'project_key':key,'title':chosen.get('title') or key, 'channel':channel,
                'publish_at':str(chosen.get('publish_at') or ''),'date':str(day),'notices':arrivals[key],
                'is_new':bool(first.get(key) and first_start<=first[key]<first_end),
                'region_code':chosen.get('region_code') or '', 'matches':[], 'hit':False,
                'status':None, 'updated_at':None, 'recorded':False, **info})
        self._results(result,cfg)
        summary=summarize(result)
        summary['notices']+=unlinked
        summary['unlinked_notices']=unlinked
        return {'date':str(day),'generated_at':time(),'timezone':cfg['source_timezone'],
                'summary':summary,'items':result,'source':'public.notices.'+time_basis,
                'region_supported':'region_code' in columns}

    def _results(self, rows, cfg):
        lookup={p['project_key']:p for p in rows}
        keys=list(lookup)
        with self.service.store.session() as session:
            for offset in range(0,len(keys),500):
                batch=keys[offset:offset+500]
                projects=session.execute(select(SalesProject.project_key,SalesProject.status,SalesProject.revision,
                    SalesProject.updated_at).where(SalesProject.project_key.in_(batch),
                    SalesProject.material['_source_revision'].as_string()==digest(cfg['source_dsn']))).all()
                revisions={p.project_key:p.revision for p in projects}
                for p in projects:
                    lookup[p.project_key].update(status=p.status,updated_at=p.updated_at,recorded=True)
                stmt=select(SalesMatch.project_key,SalesMatch.revision,SalesMatch.sales_item_id,SalesMatch.score,
                    SalesMatch.method,SalesMatch.updated_at,
                    SalesItem.name,SalesItem.categories,SalesItem.products).join(SalesItem,SalesItem.id==SalesMatch.sales_item_id).where(
                    SalesMatch.project_key.in_(batch),SalesMatch.status=='confirmed',SalesItem.enabled.is_(True),
                    SalesMatch.item_version==SalesItem.version)
                if cfg['retrieval_backend']=='local':
                    stmt=stmt.where(SalesItem.vector_profile==vector_profile(cfg),SalesItem.vector_version==SalesItem.version)
                for m in session.execute(stmt).mappings():
                    if m['revision']==revisions.get(m['project_key']):
                        lookup[m['project_key']]['matches'].append(dict(m))
                        lookup[m['project_key']]['hit']=True
                jobs=session.execute(select(SalesJob.payload['project_key'].as_string().label('project_key'),SalesJob.status)
                    .where(SalesJob.kind.in_(['analyze','review_existing']),SalesJob.payload['project_key'].as_string().in_(batch))
                    .order_by(SalesJob.created_at.desc(),SalesJob.id.desc())).all()
                latest={}
                for job in jobs:
                    latest.setdefault(job.project_key,job.status)
                for key in batch:
                    p=lookup[key]
                    p['progress']=classify_progress(p['demand'],p['status'],latest.get(key))

    def projects(self, day, slice='matched', q='', page=1, page_size=30, category='', product='', channel='', min_score=None):
        from analyzer.sales_categories import category_matches
        snapshot=self.load(day)
        rows=[]
        for p in snapshot['items']:
            if not in_slice(p,slice) or q and q.casefold() not in (p['title']+' '+p['project_key']).casefold() or channel and p['channel']!=channel:
                continue
            tags=[m for m in p['matches'] if (not category or category_matches(m['categories'],category))
                and (not product or product in m['products']) and (min_score is None or m['score']>min_score)]
            if (category or product or min_score is not None) and not tags:
                continue
            rows.append({**p,'matches':tags})
        rows.sort(key=lambda p:(p['publish_at'],p['project_key']),reverse=True)
        return {'total':len(rows),'items':rows[(page-1)*page_size:page*page_size],
                'generated_at':snapshot['generated_at'],'date':str(day)}
