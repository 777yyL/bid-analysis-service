from pathlib import Path
import asyncio
import json
import os
import sys
import socket
import uvicorn
import pytest
from tests.test_agent_mcp import pg_app
from analyzer.app import create_app
from analyzer.full_tenders import Coverage,FullCorpus,Search

DATES=dict(start='2026-08-19T00:00:00+08:00',end='2026-11-20T00:00:00+08:00',regions=['410000'])
def setup(app,cfg,tmp):
    cfg.source_file_root=str(tmp)
    (tmp/'body.txt').write_text('实际采购：建设水质监测平台。','utf8')
    (tmp/'attachment.txt').write_text('附件原文：开发数据分析平台','utf8')
    with app.state.store.engine.begin() as c:
        run=c.exec_driver_sql
        run('ALTER TABLE public.notices ADD buyer text, ADD winner text, ADD province_code text, ADD notice_type text, ADD scope_summary text, ADD content_path text')
        run('CREATE TABLE lots(notice_id bigint,winner text)')
        for n,k,t,d,s,channel,reg in [
            (61,'alpha','水质项目',None,'建设水质监测平台','采购公告','410100'),
            (62,'beta','河南生态环境局办公家具','采购桌椅',None,'采购公告','410100'),
            (63,'gamma','水质检测','检测机构须将报告上传到已有平台',None,'采购公告','410200'),
            (64,'alpha','成交结果',None,None,'结果公告','410100'),
            (65,'outside','污染源项目','建设污染源监管平台',None,'采购公告','420100'),
            (66,'delta','生态环境服务',None,'详见公告正文','采购意向','410200'),
            (67,None,'其他',None,None,'其他','410100'),
            (68,'zeta','采购公告','开发污染源管理系统','无关概要','采购公告','410100')]:
            run("""INSERT INTO notices(notice_id,project_key_name,title,buyer,winner,province_code,region_code,channel,
                publish_at,first_seen_at,revoke_status,procurement_content,scope_summary,content_path)
                VALUES(%s,%s,%s,'甲方A',NULL,%s,%s,%s,'2026-11-18','2026-11-18','active',%s,%s,%s)""",
                (n,k,t,reg[:2]+'0000',reg,channel,d,s,'body.txt' if n==66 else None))
        run("INSERT INTO lots VALUES(64,'上海供应商'),(64,'上海供应商')")
        run("INSERT INTO attachments(att_id,notice_id,filename,text_path) VALUES(55,61,'清单','attachment.txt'),(56,61,'坏路径','../outside.txt')")
    return FullCorpus(app.state.agent_queries.store.engine,cfg)

@pytest.mark.asyncio
async def test_full_corpus_real_cli_and_source_fallback(pg_app,tmp_path):
    old,cfg=pg_app;svc=setup(old,cfg,tmp_path)
    q=Search(**DATES,terms=['生态环境','水质','污染源'])
    r=svc.search(q)
    assert r['included_projects']==5 and r['unverified_notices']==0
    assert {(i['project_key'],i['source']) for i in r['items']}=={('alpha','scope_summary'),('delta','title'),('beta','title'),('gamma','title'),('zeta','procurement_content')}
    assert svc.coverage(Coverage(**DATES))['unlinked_notices']==1
    assert svc.search(Search(**DATES,events='awards',business_scope='all'))['included_projects']==1
    assert svc.evidence(61,55,'开发',0)['status']=='已定位原文'
    assert svc.evidence(61,56,'',0)['excerpt']==''
    from analyzer.source import SourceError
    with pytest.raises(SourceError): svc.evidence(64,55,'',0)
    cfg.mcp_dataset_mode='full'; app=create_app(cfg)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        job=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if job.done(): await job; raise RuntimeError('startup failed')
                await asyncio.sleep(.05)
            env=os.environ|dict(MCP_URL=f'http://127.0.0.1:{port}/mcp/',MCP_API_KEY=cfg.mcp_api_key.get_secret_value(),PYTHONIOENCODING='utf-8')
            async def cli(*args):
                proc=await asyncio.create_subprocess_exec(sys.executable,'skills/tender-presales/scripts/tender_cli.py',*args,env=env,
                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                out,err=await proc.communicate();assert proc.returncode==0,err.decode('utf8')
                return json.loads(out)
            assert set(await cli('tools'))=={'get_data_coverage','search_tenders','aggregate_tenders','get_project_timeline','get_notice_evidence','list_fact_fields','query_facts'}
            factfile=tmp_path/'facts.json'
            factfile.write_text(json.dumps({'query':{'entity':'notice','filters':[{'field':'notice.province_code','value':'410000'}],'measures':[{'op':'distinct','field':'notice.project_key_name'}]}}),'utf8')
            assert (await cli('call','query_facts',str(factfile)))['items'][0]['distinct:notice.project_key_name']==5
            r=await cli('report','--start',DATES['start'],'--end',DATES['end'],'--region','410000','--term','生态环境','--term','水质','--term','污染源','--csv',str(tmp_path/'report.csv'))
            assert r['buyers']['items'][0]['project_count']==5
            assert r['winners']['items'][0]['name']=='上海供应商' and r['winners']['items'][0]['project_count']==1
            assert r['winners']['undisclosed_projects']==4 and r['buyers']['selection_id']==r['winners']['selection_id']
            assert (tmp_path/'report.csv').read_text('utf-8-sig').count('上海供应商')==1
        finally:
            server.should_exit=True
            await job


def test_factual_selection_is_literal_and_subset_is_verified(pg_app,tmp_path):
    from analyzer.source import SourceError
    app,cfg=pg_app
    svc=setup(app,cfg,tmp_path)
    res=svc.search(Search(**DATES,terms=['生态环境']))
    assert {r['project_key'] for r in res['items']}=={'beta','delta'}
    assert all(r['matched_terms']=={'title':['生态环境']} for r in res['items'])
    assert svc.aggregate(res['selection_id'],'buyers',1,20,['delta'])['total_projects']==1
    from analyzer.structured_facts import FactQuery,run
    f=FactQuery(selection_id=res['selection_id'],fields=['notice.notice_id'])
    assert {x['notice.notice_id'] for x in run(svc.engine,f,svc.selection)['items']}=={62,66}
    with pytest.raises(SourceError):
        run(svc.engine,f.model_copy(update={'selection_id':'not-a-valid-selection'}),svc.selection)

    with pytest.raises(SourceError): svc.aggregate(res['selection_id'],'buyers',1,20,['outside'])
