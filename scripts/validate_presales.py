"""100 field questions: same contractual parameters over live MCP; no model or DB writes."""
import argparse
import asyncio
from datetime import date,timedelta
import json
import os
from pathlib import Path
from time import perf_counter
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

# Ten territories/windows with independent expectations for eligible notice IDs in fixture.
AREAS=[
 ('全国',[],89,[11,12,31,41,62,71,72,81]),
 ('湖北全省',['420000'],89,[11,12,31,41,71,72,81]),
 ('武汉',['420100'],89,[11,12,41,71]),
 ('黄石',['420200'],89,[31,72]),
 ('武汉及黄石',['420100','420200'],89,[11,12,31,41,71,72]),
 ('湖南省',['430000'],89,[62]),
 ('无样本城市鄂州',['420700'],89,[]),
 ('湖北本日',['420000'],0,[12,31,41,71,72,81]),
 ('湖北上一个公告日',['420000'],-1,[11]),
 ('省市重叠辖区',['420000','420100'],89,[11,12,31,41,71,72,81])]
INTENTS=[
 ('哪些有公司业务标签的项目值得看？','projects','business',{}),
 ('哪些项目所在阶段还可以早期跟进？','projects','business',{'stage':'early'}),
 ('有哪些有实际需求但未打任何标签的线索？','projects','untagged_demand',{}),
 ('哪些采购单位发布了实际需求，涉及多少个项目？','buyers','demand',{}),
 ('按项目数列出招标提及硬件的项目中标单位，以及各自涉及的硬件类型。','winners','hardware',{}),
 ('哪些硬件类型出现最多？同项目不要重复计数。','hardware_types','hardware',{}),
 ('哪些软件销售项在这些项目中命中？','sales_items','software',{}),
 ('哪些公告同时涉及硬件、且所属项目已有软件命中？列出公告依据。','notices','both',{}),
 ('有实际采购需求的项目分布在哪些地区？','regions','demand',{}),
 ('查摄像机相关招标项目，列出公告定位信息和标签。','projects','hardware',{'l2':'摄像机'})]

def cases(end):
    for area,regions,days,ids in AREAS:
        a=end-timedelta(days=abs(days)); b=a if days<0 else end
        for question,mode,scope,extra in INTENTS:
            yield (f'{area}，{a}至{b}：{question}',dict(mode=mode,filters=dict(start_date=str(a),end_date=str(b),regions=regions,scope=scope,**extra)),ids)

async def run(url,key,end,output,oracle=None):
    records=[]
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+key},trust_env=False,timeout=45) as http:
        async with Client(streamable_http_client(url,http_client=http),read_timeout_seconds=45) as client:
            names={t.name for t in (await client.list_tools()).tools}
            if names!={'analyze_presales'}: raise RuntimeError('未启用 presales 模式')
            for question,args,ids in cases(end):
                start=perf_counter()
                row={'id':len(records)+1,'question':question,'arguments':args}
                try:
                    response=await client.call_tool('analyze_presales',args)
                    assert not response.is_error, str(response.content)[:250]
                    d=response.structured_content
                    assert d['total_projects']>=d['represented_projects']>=0 and len(d['items'])<=20
                    if oracle: oracle(args,ids,d)
                    row.update(status='fixture_pass' if oracle else 'live_observed',projects=d['total_projects'],rows=d['total_rows'])
                except Exception as e:
                    row.update(status='error',error=(str(e)[:420] if oracle else type(e).__name__))
                row['ms']=round((perf_counter()-start)*1000,2)
                records.append(row)
                if len(records)%10==0:
                    print(f"{len(records)}/100, errors={sum(r['status']=='error' for r in records)}, last_ms={row['ms']}",flush=True)
    output.parent.mkdir(exist_ok=True,parents=True)
    output.write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf8')
    times=sorted(r['ms'] for r in records)
    print('cases',len(records),'errors',sum(r['status']=='error' for r in records),'p50_ms',times[49],'p95_ms',times[94],flush=True)
    return records

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--url',default='http://127.0.0.1:8100/mcp/')
    p.add_argument('--end',type=date.fromisoformat,default=date.today());p.add_argument('--output',type=Path,default=Path('presales-live.json'))
    a=p.parse_args()
    results=asyncio.run(run(a.url,os.environ['MCP_API_KEY'],a.end,a.output))
    raise SystemExit(int(any(r['status']=='error' for r in results)))
