"""Replay curated presales questions over MCP HTTP. Stdlib + project MCP/httpx2 dependencies."""
import argparse
import asyncio
import csv
import json
import logging
import math
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from time import perf_counter, sleep

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

TOOLS = {'O':'search_opportunities','S':'search_projects','C':'list_sales_items','A':'aggregate_projects',
         'P':'get_analysis_progress','D':'get_project_detail','E':'get_match_evidence'}
ROOT = Path(__file__).resolve().parents[1]


def progress(message):
    print(f'{datetime.now():%H:%M:%S} {message}',file=sys.stderr,flush=True)


async def timed(label, action):
    start=perf_counter()
    progress(label+' 开始')
    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            progress(f'{label} 等待返回 {perf_counter()-start:.0f}s')
    monitor=asyncio.create_task(heartbeat())
    try:
        return await action
    finally:
        monitor.cancel()
        await asyncio.gather(monitor,return_exceptions=True)
        progress(f'{label} 结束，耗时 {perf_counter()-start:.2f}s')


def save_report(path,summary,rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps({'summary':summary,'results':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if os.name!='nt' or attempt==5:
                raise
            sleep(.1*(attempt+1))


def literal(value):
    try:
        return json.loads(value)
    except ValueError:
        return value


def arguments(kind, raw, args):
    params = dict(pair.split('=', 1) for pair in raw.split())
    if kind == 'C':
        return params
    if kind in ('D','E'):
        key = args.second_project if params.get('key')=='p4' else args.project
        if kind=='D':
            return {'project_key':key}
        return {'project_key':key,'sales_item_id':args.second_item if params.get('item')=='parking' else args.item}
    end = args.end - timedelta(days=int(params.pop('back',0)))
    days = int(params.pop('days',7))
    result = {}
    for key, dest in [('group','group_by'),('limit','limit'),('page','page'),('size','page_size')]:
        if key in params:
            result[dest] = literal(params.pop(key))
    filters = {k:literal(v) if k in {'hit','model_confirmed_only','min_score','deadline_days','open_for_bids'} else v for k,v in params.items()}
    if filters.get('sales_item_id')=='visitor':
        filters['sales_item_id']=args.item
    filters.update(start_date=str(end-timedelta(days=days-1)),end_date=str(end))
    return dict(filters=filters,**result)


def find_field(obj, name):
    if isinstance(obj, dict):
        if obj.get(name) is not None and obj.get(name)!='':
            return True
        return any(find_field(v,name) for v in obj.values())
    return isinstance(obj,list) and any(find_field(v,name) for v in obj)


def assess(data, checks, fixture):
    results, notes = [], []
    for check in checks.split(';;'):
        if check.startswith('!'):
            return '需人工核对','服务接受了能力探测参数，请核对返回语义'
        if check.startswith('@'):
            ok = find_field(data,check[1:])
            notes.append(check[1:]+(' 有值' if ok else ' 未返回结构化值'))
        else:
            op='~' if '~' in check else '='
            path,want=check.split(op,1)
            value=data
            try:
                for key in path.split('.'):
                    value=value[int(key)] if isinstance(value,list) else value[key]
            except (KeyError,IndexError,TypeError):
                notes.append(path+' 无值')
                results.append(False)
                continue
            if not fixture:
                notes.append(f'{path}={str(value)[:160]}')
                results.append(True)
                continue
            expected = want if isinstance(value,str) else literal(want)
            ok = want in str(value) if op=='~' else value==expected
            notes.append(f'{path}={str(value)[:100]}')
        results.append(ok)
    if not fixture:
        return '现场观测','；'.join(notes)
    return ('可回答' if all(results) else '部分可回答' if any(results) else '未满足'), '；'.join(notes)


async def select_samples(client,args):
    """Pick actual project/item pairs from the requested period; preserve supplied overrides."""
    if args.project and args.second_project and args.item and args.second_item:
        return
    period={'start_date':str(args.end-timedelta(days=6)), 'end_date':str(args.end)}
    result=await timed('自动选样 search_opportunities',client.call_tool('search_opportunities',{'filters':period,'page_size':20}))
    if result.is_error:
        raise RuntimeError('商机样本查询失败，请检查服务是否为 0.8.21 或更高版本')
    rows=result.structured_content['items']
    if not args.project and rows:
        args.project=rows[0]['project_key']
    if not args.second_project:
        args.second_project=next((r['project_key'] for r in rows if r['project_key']!=args.project),None)
    if args.project:
        detail=await timed('自动选样 get_project_detail',client.call_tool('get_project_detail',{'project_key':args.project}))
        if detail.is_error:
            raise RuntimeError('读取样本项目失败，请检查项目键或服务日志')
        items=[m['sales_item_id'] for m in detail.structured_content['matches'] if m['status']=='confirmed']
        args.item=args.item or next(iter(items),None)
        args.second_item=args.second_item or next((i for i in items if i!=args.item),None)


async def run(args, token):
    with args.cases.open(encoding='utf-8-sig') as f:
        rows=list(csv.DictReader(f,delimiter='\t'))
    if len(rows)!=100 or len({r['售前问题'] for r in rows})!=100:
        raise ValueError('题库应包含100道不同的问题')
    out=[]
    only=getattr(args,'only',None)
    selected=[(i,row) for i,row in enumerate(rows,1) if only is None or i in only]
    if not selected:
        raise ValueError('未选择任何题目')
    progress(f'准备连接 MCP，计划执行 {len(selected)} 题')
    def record(row):
        out.append(row)
        progress(f"第 {row['id']} 题 {row['status']}，已完成 {len(out)}/{len(selected)}")
        save_report(args.output,{'state':'running','completed':len(out),'planned':len(selected),'end_date':str(args.end)},out)
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False,timeout=45) as http:
        async with Client(streamable_http_client(args.url,http_client=http),read_timeout_seconds=45) as client:
            discovered={t.name for t in (await timed('发现工具',client.list_tools())).tools}
            if not args.fixture:
                if 'search_opportunities' not in discovered:
                    raise RuntimeError('未发现 search_opportunities，请先部署新版 MCP')
                if any(r['调用'] in ('D','E') or 'sales_item_id=visitor' in r['参数'] for _,r in selected):
                    await select_samples(client,args)
            for i,row in selected:
                kind,raw,checks=row['调用'],row['参数'],row['验收依据']
                trace=[]
                reject=False
                async def call(k, params):
                    name=TOOLS[k]
                    if name not in discovered:
                        raise ValueError('工具清单中缺少 '+name)
                    start=perf_counter()
                    step={'tool':name,'args':params}
                    trace.append(step)
                    try:
                        response=await timed(f'第 {i} 题 {name}',client.call_tool(name,params))
                        step['error']=bool(response.is_error)
                        if response.is_error:
                            msg=' '.join(getattr(b,'text','') for b in response.content)
                            sqlstate=re.search(r'SQLSTATE=([0-9A-Z]{5})\b',msg)
                            if sqlstate:
                                step['sqlstate']=sqlstate[1]
                                progress(f'第 {i} 题 {name} SQLSTATE={sqlstate[1]}')
                            step['validation_error']=any(s in msg.lower() for s in ['validation','invalid','unexpected','extra inputs','not permitted','1～7','input should'])
                            return None
                        return response.structured_content
                    finally:
                        step['ms']=round(1000*(perf_counter()-start),2)
                try:
                    if kind=='CS':
                        name,_,rest=raw.partition(' ')
                        catalog=await call('C',{'q':name.removeprefix('name=')})
                        candidates=[r for r in (catalog or {}).get('items',[]) if r['name']==name[5:]]
                        if len(candidates)!=1:
                            raise ValueError('目录名称未唯一解析')
                        params=arguments('S',rest,args)
                        params['filters']['sales_item_id']=candidates[0]['id']
                        data=await call('S',params)
                    else:
                        params=arguments(kind,raw,args)
                        if (kind in ('D','E') and not all(params.values())) or (
                                'sales_item_id' in params.get('filters',{}) and not params['filters']['sales_item_id']):
                            record(dict(id=i,category=row['场景'],question=row['售前问题'],status='缺少现场样本',
                                            basis=checks,observation='可指定项目及销售项后复测',ms=0,calls=[]))
                            continue
                        data=await call(kind,params)
                    if data is None:
                        reject=checks.startswith('!') and trace[-1].get('validation_error')
                        status,note=('接口限制',checks[1:]) if reject else ('调用异常','工具未返回结构化结果')
                    else:
                        status,note=assess(data,checks,args.fixture)
                except Exception as exc:
                    status,note='调用异常',type(exc).__name__
                record(dict(id=i,category=row['场景'],question=row['售前问题'],status=status,
                                basis=checks,observation=note,ms=round(sum(t['ms'] for t in trace),2),calls=trace))
    latencies=sorted(r['ms'] for r in out if r['calls'])
    summary={'mode':'本地合成数据' if args.fixture else '现场服务','end_date':str(args.end),
             'questions':len(out),'calls':sum(len(r['calls']) for r in out),'statuses':dict(Counter(r['status'] for r in out)),
             'state':'finished','p50_ms':latencies[math.ceil(.5*len(latencies))-1] if latencies else None,'p95_ms':latencies[math.ceil(.95*len(latencies))-1] if latencies else None,
             'sample_project':args.project,'sample_items':[args.item,args.second_item]}
    save_report(args.output,summary,out)
    print(json.dumps(summary,ensure_ascii=False))
    return out


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default=os.getenv('MCP_URL','http://127.0.0.1:8100/mcp/'))
    parser.add_argument('--cases',type=Path,default=ROOT/'examples/presales-mcp-100.tsv')
    parser.add_argument('--output',type=Path,default=Path('presales-mcp-results.json'))
    parser.add_argument('--only',type=int,nargs='+',choices=range(1,101),metavar='题号',help='只执行指定题，例如 --only 3 41 91')
    parser.add_argument('--end',type=date.fromisoformat,default=datetime.now(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=1))
    for key in ('project','second-project','item','second-item'):
        parser.add_argument('--'+key)
    args=parser.parse_args()
    args.fixture=False
    logging.basicConfig(level=logging.CRITICAL)
    asyncio.run(run(args,os.environ['MCP_API_KEY']))
