"""Bounded PostgreSQL fact queries at a declared grain; no arbitrary SQL from clients."""
import re
from typing import Annotated, Any, Literal
from pydantic import Field
from sqlalchemy import text
from analyzer.schemas import StrictModel
from analyzer.source import SourceError

Entity=Literal['notice','project','lot','attachment']
TABLES={'notice':'notices','project':'mv_project','lot':'lots','attachment':'attachments'}
KEYS={'notice':'notice_id','project':'project_key','lot':'lot_id','attachment':'att_id'}
SOURCES={'project':['project'],'notice':['notice','project'],'lot':['lot','notice','project'],
         'attachment':['attachment','notice','project']}
# Only introspect these public business relations. No config/credentials, binary or
# free-form JSON payloads, raw documents, or filesystem locators in this interface.
BLOCK=re.compile(r'(?:^|_)(?:path|hash|headers?|cookie|token|secret|password|passwd|private|credential|dsn|api_key|access_key|sign|signature|phone|mobile|email|contact|embedding|vector|payload|blob)(?:_|$)')
HIDDEN={'content','raw_html','raw_text','sha256','procurement_content','scope_summary','summary','remote_url','vision_scope','body','html'}
CAST={'int2':'smallint','int4':'integer','int8':'bigint','numeric':'numeric','float4':'real','float8':'double precision',
      'timestamp':'timestamp','timestamptz':'timestamptz','date':'date','time':'time','timetz':'timetz','bool':'boolean',
      'text':'text','bpchar':'text','varchar':'text','uuid':'uuid'}
NUM={'int2','int4','int8','numeric','float4','float8'}
TIME={'date','timestamp','timestamptz'}
Name=Annotated[str,Field(min_length=1,max_length=100)]
Scalar=Annotated[str,Field(max_length=500)]|int|float|bool
class Condition(StrictModel):
    field:Name
    op:Literal['eq','ne','in','contains','gt','gte','lt','lte','is_null','not_null','has_any']='eq'
    value:Scalar|list[Scalar]|None=None

class Dimension(StrictModel):
    field:Name
    period:Literal['day','week','month','quarter','year']|None=None

class Measure(StrictModel):
    op:Literal['count','distinct','sum','avg','min','max']
    field:Name|None=None

class Order(StrictModel):
    field:Name
    desc:bool=False

class FactQuery(StrictModel):
    entity:Entity='notice'
    selection_id:Name|None=None
    selection_scope:Literal['notice','project']='notice'
    filters:list[Condition]=Field(default_factory=list,max_length=24)
    fields:list[Name]=Field(default_factory=list,max_length=20)
    group_by:list[Dimension]=Field(default_factory=list,max_length=5)
    measures:list[Measure]=Field(default_factory=list,max_length=8)
    order_by:list[Order]=Field(default_factory=list,max_length=5)
    page:int=Field(default=1,ge=1,le=20000)
    page_size:int=Field(default=30,ge=1,le=100)

def inventory(conn,entity):
    out={}
    rows=conn.execute(text("""SELECT c.relname,a.attname,t.typname,t.typelem<>0 AND t.typcategory='A' AS arr,
        et.typname AS element,pg_catalog.col_description(c.oid,a.attnum) AS label
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace ns ON ns.oid=c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid JOIN pg_catalog.pg_type t ON a.atttypid=t.oid
        LEFT JOIN pg_catalog.pg_type et ON et.oid=t.typelem
        WHERE ns.nspname='public' AND c.relname=ANY(CAST(:tabs AS text[])) AND a.attnum>0 AND NOT a.attisdropped
        AND has_column_privilege(c.oid,a.attnum,'SELECT') ORDER BY c.relname,a.attnum"""),
        dict(tabs=[TABLES[s] for s in SOURCES[entity]]))
    rev={v:k for k,v in TABLES.items()}
    for tab,col,typ,arr,elem,label in rows:
        if col in HIDDEN or BLOCK.search(col) or not re.fullmatch('[a-z][a-z0-9_]*',col): continue
        if (elem if arr else typ) not in CAST: continue
        src=rev[tab];kind=elem if arr else typ
        ops=['has_any'] if arr else ['eq','ne','in']+(['contains'] if kind in ('text','bpchar','varchar') else [])+(['gt','gte','lt','lte'] if kind in NUM|TIME|{'time','timetz'} else [])
        out[f'{src}.{col}']=dict(type=kind,array=arr,operators=ops+['is_null','not_null'],description=(label or '')[:250])
    return out

def catalog(engine,entity):
    with engine.connect() as c:
        return dict(entity=entity,fields=inventory(c,entity),grain_key=entity+'.'+KEYS[entity],joins=SOURCES[entity],
            note='字段名前缀区分公告阶段 notice.channel 和当前项目阶段 project.channel；字段和权限以当前连接为准。')

def run(engine,q,lookup=None):
    def fail(msg): raise SourceError(msg)
    params={}; base=q.entity; needed={base}
    with engine.connect() as c:
        c.exec_driver_sql("SET LOCAL TIME ZONE 'Asia/Shanghai'")
        cols=inventory(c,base)
        def col(name):
            if not name or name not in cols: fail(f'未开放或不存在的字段：{name}，请先查看字段目录')
            s,k=name.split('.');needed.add(s);return f'{s}."{k}"'
        wh=[]
        if q.selection_id:
            if lookup is None: fail('此入口不支持冻结结果集')
            _,records,_,keys=lookup(q.selection_id)
            if q.selection_scope=='notice':
                if base=='project': fail('项目粒度请指定selection_scope=project')
                expr=col('notice.notice_id')
                params['sid']=[r['notice_id'] for r in records]
                wh.append(expr+'=ANY(CAST(:sid AS bigint[]))')
            else:
                expr=col('project.project_key' if base=='project' else 'notice.project_key_name')
                params['sid']=keys
                wh.append(expr+'=ANY(CAST(:sid AS text[]))')
        for i,cond in enumerate(q.filters):
            expr=col(cond.field);meta=cols[cond.field];op=cond.op
            if op not in meta['operators']: fail(f'{cond.field} 不支持 {op}')
            if op in ('is_null','not_null'):
                if cond.value is not None: fail('NULL条件不能带value')
                wh.append(expr+(' IS NULL' if op=='is_null' else ' IS NOT NULL'));continue
            many=op in ('in','has_any')
            if cond.value is None or isinstance(cond.value,list)!=many: fail('筛选值形状与操作符不符')
            vs=cond.value if many else [cond.value]
            if not 1<=len(vs)<=100: fail('列表长度应为1～100')
            vals=[]
            for j,v in enumerate(vs):
                key=f'v{i}_{j}';params[key]=str(v);vals.append(f'CAST(:{key} AS {CAST[meta["type"]]})')
            if op=='has_any': wh.append(f'EXISTS (SELECT FROM unnest({expr}) AS a(v) WHERE a.v IN ({",".join(vals)}))')
            elif op=='in': wh.append(f'{expr} IN ({",".join(vals)})')
            elif op=='contains': wh.append(f'strpos(lower({expr}),lower({vals[0]}))>0')
            else:
                sign={'eq':'=','ne':'<>','gt':'>','gte':'>=','lt':'<','lte':'<='}[op]
                wh.append(f'{expr} {sign} {vals[0]}')
        aggregate=bool(q.group_by or q.measures)
        if aggregate and q.fields: fail('分组统计不能与明细fields混用')
        selected={};group=[];order=[]
        def add(alias,expr):
            if alias in selected: fail('字段或指标重复')
            selected[alias]=expr
        if aggregate:
            for d in q.group_by:
                expr=col(d.field)
                if d.period:
                    if cols[d.field]['array'] or cols[d.field]['type'] not in TIME: fail('时间分桶只接受日期或时间戳')
                    expr=f"date_trunc('{d.period}',{expr})"
                alias=d.field+(':'+d.period if d.period else '')
                add(alias,expr);group.append(str(len(selected)))
            for m in q.measures or [Measure(op='count')]:
                if m.op=='count':
                    if m.field: fail('count 是行数，不接收字段；去重请用 distinct')
                    add('count','count(*)');continue
                expr=col(m.field);meta=cols[m.field]
                if m.op not in ('distinct',) and (meta['array'] or meta['type'] not in NUM|TIME): fail('此指标不支持该类型')
                if m.op in ('sum','avg') and (meta['type'] not in NUM or not m.field.startswith(base+'.')): fail('只能对当前粒度的数值求和/平均，避免跨粒度重复')
                add(m.op+':'+m.field,'count(DISTINCT '+expr+')' if m.op=='distinct' else f'{m.op}({expr})')
        else:
            for name in q.fields or [base+'.'+KEYS[base]]: add(name,col(name))
        for o in q.order_by:
            if o.field in selected: expr='"'+o.field+'"'
            elif not aggregate: expr=col(o.field)
            else: fail('统计排序需使用返回的维度/指标名称，如 count')
            order.append(expr+(' DESC' if o.desc else ' ASC')+' NULLS LAST')
        if aggregate: order+=group or ['1']
        else: order.append(col(base+'.'+KEYS[base]))
        joins=f'public.{TABLES[base]} {base}'
        if base in ('lot','attachment') and needed&{'notice','project'}:
            joins+=f' LEFT JOIN public.notices notice ON notice.notice_id={base}.notice_id'
        if 'project' in needed and base!='project':
            joins+=' LEFT JOIN public.mv_project project ON project.project_key=notice.project_key_name'
        selects=', '.join(f'{expr} AS "{alias}"' for alias,expr in selected.items())
        sql=f'SELECT {selects} FROM {joins}'+(' WHERE '+' AND '.join(wh) if wh else '')+(' GROUP BY '+','.join(group) if group else '')
        rows=list(c.execute(text(sql+' ORDER BY '+','.join(order)+' LIMIT :lim OFFSET :off'),
                  params|{'lim':q.page_size+1,'off':(q.page-1)*q.page_size}).mappings())
        def bounded(v):
            if isinstance(v,str) and len(v)>2048:return v[:2048]+'…'
            if isinstance(v,list):return [bounded(x) for x in v[:100]]
            return v
        return dict(selection_id=q.selection_id,selection_scope=q.selection_scope if q.selection_id else None,entity=base,mode='groups' if aggregate else 'rows',items=[{k:bounded(v) for k,v in r.items()} for r in rows[:q.page_size]],
                    page=q.page,page_size=q.page_size,has_more=len(rows)>q.page_size,timezone='Asia/Shanghai',
                    basis='所有显式条件AND；不隐式剔除撤销、NULL或无标签行。count按所选粒度，distinct忽略NULL；时间按北京时间。文本最多2048字，数组最多100项。')

def register(server,queries,invoke,annotations,lookup):
    @server.tool(annotations=annotations)
    async def list_fact_fields(entity:Entity='notice')->dict[str,Any]:
        """发现公告、项目、标段或附件的结构化字段和可用操作符。含当前项目阶段、采购人、中标人、采购方式、金额、时间和地区。"""
        return await invoke('list_fact_fields',catalog,queries.store.engine,entity)
    @server.tool(annotations=annotations)
    async def query_facts(query:FactQuery)->dict[str,Any]:
        """多维事实检索，不判断商机。先list_fact_fields；选entity粒度、filters、fields，或group_by与measures。分组排序用count或sum:lot.amount_value等指标名。"""
        return await invoke('query_facts',run,queries.store.engine,query,lookup)
