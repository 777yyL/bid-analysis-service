"""Queries over presales snapshots only; regional scope is a business filter, not an ACL."""
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from analyzer.schemas import StrictModel
from analyzer.source import SourceError

class TerritoryQuery(StrictModel):
    start_date: date
    end_date: date
    regions: list[Annotated[str, Field(pattern=r'^\d{6}$')]] = Field(default_factory=list,max_length=30,description='省或市六位编码的并集；空数组为全国，不表示个人权限')
    scope: Literal['business','software','hardware','both','demand','untagged_demand']='business'
    stage: Literal['all','early']='all'
    l1: str=Field(default='',max_length=200)
    l2: str=Field(default='',max_length=200)
    model_confirmed_only: bool=False
    sales_item: str=Field(default='',max_length=200,description='销售项名称精确匹配')
    buyer: str=Field(default='',max_length=200)
    winner: str=Field(default='',max_length=200)
    project_key: str=Field(default='',max_length=200)
    @model_validator(mode='after')
    def valid(self):
        if not 0 <= (self.end_date-self.start_date).days < 366 or self.end_date==date.max:
            raise ValueError('日期两端包含，最多366天')
        return self

Mode=Literal['projects','buyers','winners','hardware_types','sales_items','regions','notices','awards']

def query_snapshot(engine, q, mode, page, size):
    params=q.model_dump() | dict(end=q.end_date+timedelta(days=1),lim=size,off=(page-1)*size)
    where=["n.channel IN ('采购意向','采购公告','更正公告')","n.publish_at>=:start_date","n.publish_at<:end"]
    if q.regions:
        where+=['(n.city=ANY(CAST(:regions AS text[])) OR n.province=ANY(CAST(:provs AS text[])))']
        params['provs']=[r for r in q.regions if r.endswith('0000')]
    if q.stage=='early':
        where+=["p.stage IN ('采购意向','采购公告','更正公告')"]
    hw='SELECT h.* FROM presales.hardware h WHERE h.notice_id=n.notice_id'
    if q.l1: hw+=' AND h.l1=:l1'
    if q.l2: hw+=' AND h.l2=:l2'
    if q.scope in ('hardware','both') or q.l1 or q.l2: where+=[f'EXISTS ({hw})']
    if q.scope in ('software','both'): where+=['p.has_software']
    if q.scope=='business': where+=['(p.has_software OR EXISTS ('+hw+'))']
    if q.scope in ('demand','untagged_demand'): where+=['n.demand IS NOT NULL']
    if q.scope=='untagged_demand': where+=['NOT p.has_hardware AND NOT p.has_software']
    software_filter=(" AND s.name=:sales_item" if q.sales_item else "")+(" AND s.method IN ('model','model_auto','model_backfill')" if q.model_confirmed_only else "")
    if software_filter: where+=['EXISTS (SELECT FROM presales.software s WHERE s.project_key=p.project_key'+software_filter+')']
    if q.buyer: where+=['n.buyer=:buyer']
    if q.winner: where+=['EXISTS (SELECT FROM presales.winner w WHERE w.project_key=p.project_key AND w.name=:winner)']
    if q.project_key: where+=['p.project_key=:project_key']
    base="""WITH e AS (SELECT n.notice_id,n.project_key,n.publish_at,n.city,n.province,n.buyer
        FROM presales.notice n JOIN presales.project p USING(project_key) WHERE """+' AND '.join(where)+"""),
        keys AS (SELECT DISTINCT project_key FROM e),
        hw AS (SELECT DISTINCT e.project_key,h.l1,h.l2 FROM e JOIN presales.hardware h USING(notice_id)
                 WHERE true """+(' AND h.l1=:l1' if q.l1 else '')+(' AND h.l2=:l2' if q.l2 else '')+""")"""
    if mode in ('projects','notices'):
        pick='DISTINCT ON (project_key)' if mode=='projects' else ''
        order='ORDER BY project_key,publish_at DESC,notice_id DESC' if pick else ''
        base+=f', selected AS (SELECT * FROM (SELECT {pick} * FROM e {order}) d ORDER BY publish_at DESC,notice_id DESC LIMIT :lim OFFSET :off)'
        rows=f"""SELECT a.project_key,a.notice_id,a.publish_at,n.title,n.channel,p.stage,n.buyer,n.city,n.province,n.source_url,left(n.demand,500) AS demand_excerpt,
            ARRAY(SELECT DISTINCT s.name FROM presales.software s WHERE s.project_key=a.project_key {software_filter} ORDER BY s.name LIMIT 30) AS project_sales_items,
            ARRAY(SELECT DISTINCT l1||'/'||l2 FROM presales.hardware h WHERE h.notice_id=a.notice_id ORDER BY 1 LIMIT 30) AS notice_hardware_types,
            (SELECT json_agg(z) FROM (SELECT l1,l2,hits,left(evidence,250) AS excerpt,tagged_at FROM presales.hardware h WHERE h.notice_id=a.notice_id ORDER BY l1,l2 LIMIT 10) z) AS hardware_evidence
            FROM selected a JOIN presales.notice n USING(notice_id)
            JOIN presales.project p ON p.project_key=a.project_key"""
        order='publish_at DESC,notice_id DESC'
    elif mode=='awards':
        rows='SELECT k.project_key,w.notice_id,w.lot_id,w.lot_no,w.name AS winner,n.title,n.publish_at FROM keys k JOIN presales.winner w USING(project_key) JOIN presales.notice n USING(notice_id)'
        if q.winner: rows+=' WHERE w.name=:winner'
        order='project_key,notice_id,lot_id,winner'
    else:
        groups={
          'buyers':"SELECT project_key,buyer AS value FROM e WHERE buyer IS NOT NULL",
          'winners':"SELECT k.project_key,w.name AS value FROM keys k JOIN presales.winner w USING(project_key)"+(" WHERE w.name=:winner" if q.winner else ''),
          'hardware_types':"SELECT project_key,l1||'/'||l2 AS value FROM hw",
          'sales_items':"SELECT k.project_key,s.name AS value FROM keys k JOIN presales.software s USING(project_key) WHERE true"+software_filter,
          'regions':"SELECT project_key,coalesce(city,province,'未知') AS value FROM e"}
        base+=', pairs AS (SELECT DISTINCT * FROM ('+groups[mode]+') facts)'
        rows="SELECT value,count(*) AS project_count,min(project_key) AS example_project_key FROM pairs GROUP BY value"
        order='project_count DESC,value'
    paging='' if mode in ('projects','notices') else ' LIMIT :lim OFFSET :off'
    total_source={'projects':'keys','notices':'e'}.get(mode,'grouped')
    base+=', grouped AS ('+rows+'), pg AS (SELECT * FROM grouped ORDER BY '+order+paging+')'
    if mode=='winners':
        base+=""", enriched AS (SELECT pg.*, (SELECT min(w.notice_id) FROM presales.winner w WHERE w.project_key=pg.example_project_key AND w.name=pg.value) AS example_award_notice_id, (SELECT count(*) FROM (SELECT DISTINCT h.l1,h.l2 FROM pairs t JOIN hw h USING(project_key) WHERE t.value=pg.value) a) AS hardware_type_count,
            (SELECT coalesce(json_agg(z),'[]') FROM (SELECT h.l1,h.l2,count(*) AS project_count FROM pairs t JOIN hw h USING(project_key)
             WHERE t.value=pg.value GROUP BY h.l1,h.l2 ORDER BY count(*) DESC,h.l1,h.l2 LIMIT 50) z) AS hardware_types FROM pg)"""
    table='enriched' if mode=='winners' else 'pg'
    sql=base+f""" SELECT (SELECT refreshed_at FROM presales.snapshot) AS refreshed_at,
        (SELECT count(*) FROM keys) AS total_projects,(SELECT count(*) FROM {total_source}) AS total_rows,
        (SELECT count(DISTINCT project_key) FROM {'e' if mode in ('projects','notices') else 'grouped' if mode=='awards' else 'pairs'}) AS represented_projects,
        (SELECT coalesce(json_agg(x ORDER BY {order}),'[]') FROM {table} x) AS items"""
    try:
        with engine.connect() as c:
            result=dict(c.execute(text(sql),params).mappings().one())
    except DBAPIError as ex:
        if getattr(ex.orig,'sqlstate','') in ('42P01','3F000','42501'):
            raise SourceError('售前数据集未安装或无读权限；请管理员先刷新 presales 数据集。') from None
        raise
    if not result['refreshed_at']: raise SourceError('售前数据集尚无成功刷新记录')
    return result | {'mode':mode,'filters':q.model_dump(mode='json'),'page':page,'page_size':size,
        'has_more':page*size<result['total_rows'],'unrepresented_projects':result['total_projects']-result['represented_projects'],
        'basis':'按有效采购意向/采购公告/更正公告的发布日期和地区筛选；硬件来自同一公告，软件为项目标签，中标人取快照时有效结果。类型为相关项目的类型，不能据此认定供应了具体设备。'}

def register(server,queries,invoke,annotations):
    @server.tool(annotations=annotations)
    async def analyze_presales(filters: TerritoryQuery, mode: Mode='projects',
            page: Annotated[int,Field(ge=1,le=5000)]=1,page_size: Annotated[int,Field(ge=1,le=50)]=20) -> dict[str, Any]:
        """按省市、招标发布时间分析售前数据集。projects查线索、notices查公告依据；awards核查结果公告和标段（输入日期仍选招标公告，不是中标日期）；buyers/winners客户/中标人排行；hardware_types/sales_items/regions统计。湖北近三个月硬件项目中标人及设备类型：regions=['420000'],scope=hardware,mode=winners。scope=demand含未打标；untagged_demand专查未命中。排名为去重项目数，不用单页估算。"""
        return await invoke('analyze_presales',query_snapshot,queries.store.engine,filters,mode,page,page_size)
