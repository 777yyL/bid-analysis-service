"""Live full-corpus tender search. Selection snapshots are bounded, ephemeral and local."""
from collections import defaultdict
from datetime import datetime, timedelta
import secrets
from threading import Lock
from time import monotonic
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from sqlalchemy import text
from analyzer.schemas import StrictModel
from analyzer.source import SourceError

TZ=ZoneInfo('Asia/Shanghai')
EARLY="('采购公告','采购意向','更正公告')"
Key=Annotated[str,Field(min_length=1,max_length=180)]
Page=Annotated[int,Field(ge=1,le=200000)]
Size=Annotated[int,Field(ge=1,le=50)]

class Window(StrictModel):
    start:datetime=Field(description='北京时间，含起点；例如 2026-08-19T00:00:00+08:00')
    end:datetime=Field(description='不含终点；包含11月19日全天时填写11月20日零点')
    regions:list[Annotated[str,Field(pattern=r'^[0-9]{6}$')]]=Field(default_factory=list,max_length=30)
    clock:Literal['publish_at','first_seen_at']='publish_at'
    events:Literal['tenders','awards','all']='tenders'
    active_only:bool=True
    @model_validator(mode='after')
    def valid(self):
        for k in ('start','end'):
            d=getattr(self,k)
            setattr(self,k,d.replace(tzinfo=TZ) if d.tzinfo is None else d.astimezone(TZ))
        if not timedelta(0)<self.end-self.start<=timedelta(days=366):
            raise ValueError('时间窗口需大于0且不超过366天')
        return self

class Search(Window):
    terms:list[Annotated[str,Field(min_length=1,max_length=60)]]=Field(default_factory=list,max_length=30,description='任一词匹配；可填写生态环境等主题及同义词')
    business_scope:Literal['all']='all'

def predicate(q):
    params=dict(start=q.start,end=q.end)
    parts=[f'n.{q.clock}>=:start',f'n.{q.clock}<:end']
    if q.active_only: parts+=["n.revoke_status='active'"]
    if q.events=='tenders': parts+=[f'n.channel IN {EARLY}']
    if q.events=='awards': parts+=["n.channel='结果公告'"]
    if q.regions:
        params.update(reg=q.regions,provs=[x[:2] for x in q.regions if x.endswith('0000')])
        parts+=["(n.region_code=ANY(CAST(:reg AS text[])) OR left(coalesce(nullif(n.province_code,''),n.region_code),2)=ANY(CAST(:provs AS text[])))"]
    return ' AND '.join(parts),params

def procurement(demand,summary):
    return ("procurement_content",demand) if demand and demand.strip() else (("scope_summary",summary) if summary and summary.strip() else ("title",""))

class FullCorpus:
    def __init__(self,engine,cfg):
        self.engine,self.cfg=engine,cfg
        self.cache={};self.guard=Lock()

    def coverage(self,q):
        where,p=predicate(q)
        with self.engine.connect() as c:
            c.exec_driver_sql("SET LOCAL TIME ZONE 'Asia/Shanghai'")
            row=c.execute(text(f"""SELECT count(*) notices,count(distinct nullif(n.project_key_name,'')) projects,
                count(*) FILTER(WHERE nullif(n.project_key_name,'') IS NULL) unlinked_notices,
                count(*) FILTER(WHERE nullif(btrim(procurement_content),'') IS NOT NULL) with_demand,
                count(*) FILTER(WHERE nullif(btrim(procurement_content),'') IS NULL AND nullif(btrim(scope_summary),'') IS NOT NULL) summary_only,
                count(*) FILTER(WHERE nullif(content_path,'') IS NOT NULL) with_body_path FROM public.notices n WHERE {where}"""),p).mappings().one()
        return dict(row)|{'filters':q.model_dump(mode='json'),'note':'有文件路径不等于文件可读；本工具不做主题或信息化筛选。'}

    def search(self,q):
        where,p=predicate(q)
        terms=list(dict.fromkeys(t.strip() for t in q.terms if t.strip()))
        if q.terms and not terms: raise SourceError('检索词不能为空白')
        if terms:
            p['words']=terms
            where+=""" AND EXISTS(SELECT FROM unnest(CAST(:words AS text[])) t(word)
                WHERE strpos(lower(n.title),lower(t.word))>0 OR strpos(lower(coalesce(nullif(btrim(n.procurement_content),''),n.scope_summary,'')),lower(t.word))>0)"""
        records=[]; award=defaultdict(set); budget=0
        with self.engine.connect() as c:
            c.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            c.exec_driver_sql("SET LOCAL TIME ZONE 'Asia/Shanghai'")
            as_of=c.exec_driver_sql('SELECT transaction_timestamp()').scalar().isoformat()
            stmt=f"""SELECT n.notice_id,n.project_key_name,n.title,n.buyer,n.procurement_content,n.scope_summary,n.content_path
                FROM public.notices n WHERE {where} ORDER BY n.{q.clock} DESC,n.notice_id DESC LIMIT 50001"""
            for nid,key,title,buyer,demand,summary,path in c.execution_options(stream_results=True).execute(text(stmt),p):
                if len(records)>=50000: raise SourceError('候选公告超过50000，请缩小地区、时间或主题；未返回截断统计')
                src,body=procurement(demand,summary)
                hits={field:[t for t in terms if t.lower() in (value or '').lower()] for field,value in [('title',title),(src,body)] if value}
                body_hits=hits.get(src,[]) if body else []
                field=src if body_hits or (body and not terms) else 'title'
                value=(body if field!='title' else title) or ''
                pos=min((value.lower().find(t.lower()) for t in hits.get(field,[])),default=0)
                row=dict(notice_id=nid,project_key=key,title=(title or '')[:300],buyer=(buyer or '').strip() or None,
                         matched_terms={k:v for k,v in hits.items() if v},status='included',source=field,excerpt=value[max(0,pos-100):pos+300])
                budget+=sum(len(str(v)) for v in row.values())
                if budget>8_000_000: raise SourceError('结果集超过容量上限，请缩小范围')
                records.append(row)
            keys=sorted({r['project_key'] for r in records if r['status']=='included' and r['project_key']})
            if keys:
                # Award-window questions must not pick winners from another result notice.
                extra=" AND n.notice_id=ANY(CAST(:ids AS bigint[]))" if q.events=='awards' else ''
                for k,name,nid in c.execute(text("""SELECT DISTINCT n.project_key_name,coalesce(l.name,btrim(n.winner)),n.notice_id
                    FROM public.notices n LEFT JOIN LATERAL (SELECT DISTINCT btrim(winner) AS name FROM public.lots
                    WHERE notice_id=n.notice_id AND btrim(winner) NOT IN ('','-','/','暂无','无','详见附件','详见公告')) l ON true
                    WHERE n.project_key_name=ANY(CAST(:keys AS text[])) AND n.revoke_status='active' AND n.channel='结果公告'
                    AND concat_ws(' ',n.title,n.notice_type) !~ '候选|废标|流标|终止'
                    AND coalesce(l.name,btrim(n.winner)) NOT IN ('','-','/','暂无','无','详见附件','详见公告')"""+extra),
                    dict(keys=keys,ids=[r['notice_id'] for r in records if r['status']=='included'])):
                    budget+=len(k)+len(name)+32
                    if budget>8_000_000: raise SourceError('中标结果超过容量上限，请缩小检索范围')
                    award[name].add((k,nid))
        token=secrets.token_urlsafe(24)
        with self.guard:
            now=monotonic()
            self.cache={k:v for k,v in self.cache.items() if now-v[0]<900}
            if len(self.cache)>=8: del self.cache[next(iter(self.cache))]
            self.cache[token]=(now,records,award,keys)
        return dict(selection_id=token,as_of=as_of,terms=terms,business_scope=q.business_scope,filters=q.model_dump(mode='json'),
            candidate_notices=len(records),included_projects=len(keys),included_notices=sum(r['status']=='included' for r in records),
            unverified_notices=sum(r['status']=='unverified' for r in records),
            items=[r for r in records if r['status']=='included'][:10],
            basis='标题及需求文本（为空时用概要）的词面召回；仅返回检索事实，不代表信息化商机。未检索全库原件。')

    def selection(self,token):
        with self.guard:
            v=self.cache.get(token)
            if not v or monotonic()-v[0]>=900: raise SourceError('结果集过期、被替换或不存在，请重新查询')
        return v

    def aggregate(self,token,group,page,size,project_keys=None):
        _,rows,award,keys=self.selection(token);start=(page-1)*size
        if project_keys is not None:
            subset=set(project_keys)
            if not subset.issubset(keys): raise SourceError('指定项目不属于该冻结结果集')
            keys=subset
            rows=[r for r in rows if r['project_key'] in subset]
            award={n:{p for p in ps if p[0] in subset} for n,ps in award.items()}
            award={n:ps for n,ps in award.items() if ps}
        if group in ('included','unverified'):
            items=[r for r in rows if r['status']==group]
            return dict(selection_id=token,total_rows=len(items),items=items[start:start+size],has_more=start+size<len(items))
        groups=award
        if group=='buyers':
            groups=defaultdict(set)
            for r in rows:
                if r['status']=='included' and r['buyer'] and r['project_key']:
                    groups[r['buyer']].add((r['project_key'],r['notice_id']))
        represented=set();items=[]
        for name,pairs in groups.items():
            ks={k for k,n in pairs};represented.update(ks);k,n=min(pairs)
            items.append(dict(name=name,project_count=len(ks),example_project_key=k,example_notice_id=n))
        items.sort(key=lambda x:(-x['project_count'],x['name']))
        return dict(selection_id=token,total_projects=len(keys),represented_projects=len(represented),
            undisclosed_projects=len(keys)-len(represented),total_rows=len(items),items=items[start:start+size],has_more=start+size<len(items))


    def timeline(self,key,page,size):
        with self.engine.connect() as c:
            rows=list(c.execute(text("""SELECT notice_id,title,channel,buyer,publish_at,first_seen_at,revoke_status
                FROM public.notices WHERE project_key_name=:k ORDER BY publish_at NULLS LAST,notice_id LIMIT :n OFFSET :offset"""),
                dict(k=key,n=size+1,offset=(page-1)*size)).mappings())
        return dict(project_key=key,items=[dict(r) for r in rows[:size]],has_more=len(rows)>size)

    def evidence(self,nid,aid,phrase,offset):
        with self.engine.connect() as c:
            row=c.execute(text("""SELECT title,content_path FROM public.notices WHERE notice_id=:n"""),{'n':nid}).first()
            if not row: raise SourceError('公告不存在')
            attachments=list(c.execute(text("""SELECT att_id,filename FROM public.attachments WHERE notice_id=:n
                ORDER BY att_id LIMIT 51"""),{'n':nid}).mappings())
            path=row.content_path
            if aid is not None:
                att=c.execute(text('SELECT text_path FROM public.attachments WHERE notice_id=:n AND att_id=:a'),dict(n=nid,a=aid)).first()
                if not att: raise SourceError('附件不属于指定公告或不存在')
                path=att.text_path
        raw,reason=read_material(self.cfg,path)
        pos=raw.find(phrase,offset) if phrase else min(offset,len(raw))
        start=max(offset,pos-120) if phrase and pos>=0 else offset
        snippet=raw[start:start+2400] if pos>=0 else ''
        return dict(notice_id=nid,attachment_id=aid,title=row.title,source='attachment' if aid else 'notice_body',
            status=reason or ('已定位原文' if snippet else '未定位到原文'),excerpt=snippet,char_start=start,
            next_offset=start+len(snippet) if snippet and start+len(snippet)<len(raw) else None,
            attachments=[dict(a) for a in attachments[:50]],attachments_truncated=len(attachments)>50)

def read_material(cfg,path,limit=None):
    from pathlib import Path,PureWindowsPath
    if not path or not cfg.source_file_root: return '', '未提供文本路径或未配置挂载'
    try:
        root=Path(cfg.source_file_root).resolve(strict=True)
        rel=Path(path)
        if rel.is_absolute() or PureWindowsPath(path).anchor: raise ValueError()
        f=(root/rel).resolve(strict=True)
        if not f.is_relative_to(root) or not f.is_file(): raise ValueError()
        cap=cfg.source_max_response_bytes if limit is None else min(cfg.source_max_response_bytes,limit)
        if f.stat().st_size>cap: return '', '材料超过单文件读取上限'
        with f.open('rb') as h: raw=h.read(cap+1)
        if len(raw)>cap: return '', '材料超过单文件读取上限'
        return raw.decode('utf-8-sig'),''
    except (ValueError,OSError): return '', '文本不可读或不在挂载目录内'

class Coverage(Window):
    events:Literal['tenders','awards','all']='all'

def register(server,queries,cfg,invoke,annotations):
    from analyzer.structured_facts import register as register_facts
    svc=FullCorpus(queries.store.engine,cfg)
    register_facts(server,queries,invoke,annotations,svc.selection)
    @server.tool(annotations=annotations)
    async def get_data_coverage(window:Coverage)->dict[str,Any]:
        """主库全量计数。采集班次用first_seen_at、events=all、active_only=false。时间左闭右开。"""
        return await invoke('get_data_coverage',svc.coverage,window)
    @server.tool(annotations=annotations)
    async def search_tenders(filters:Search)->dict[str,Any]:
        """检索招标主体和需求，缺失回退概要，无业务标签依赖。按显式词查询，不做软件业务判断。河南=410000。返回词面命中的字段、片段和结果集。"""
        return await invoke('search_tenders',svc.search,filters)
    @server.tool(annotations=annotations)
    async def aggregate_tenders(selection_id:Key,group:Literal['buyers','winners','included','unverified'],page:Page=1,page_size:Size=20,project_keys:Annotated[list[Key],Field(max_length=2000)]|None=None)->dict[str,Any]:
        """对同一冻结结果集统计主体；传project_keys仅统计Agent核实的子集。included翻阅词面命中，不代表软件商机。"""
        return await invoke('aggregate_tenders',svc.aggregate,selection_id,group,page,page_size,project_keys)
    @server.tool(annotations=annotations)
    async def get_project_timeline(project_key:Key,page:Page=1,page_size:Size=30)->dict[str,Any]:
        """读取项目各阶段公告，含撤销状态。"""
        return await invoke('get_project_timeline',svc.timeline,project_key,page,page_size)
    @server.tool(annotations=annotations)
    async def get_notice_evidence(notice_id:Annotated[int,Field(gt=0)],attachment_id:Annotated[int,Field(gt=0)]|None=None,
            phrase:Annotated[str,Field(max_length=200)]='',offset:Annotated[int,Field(ge=0)]=0)->dict[str,Any]:
        """按公告或所属附件ID从挂载目录读取原文，phrase定位片段。未读到文件不以概要冒充原文。"""
        return await invoke('get_notice_evidence',svc.evidence,notice_id,attachment_id,phrase,offset)
