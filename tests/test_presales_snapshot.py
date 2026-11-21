import asyncio
from collections import defaultdict
from datetime import date
from pathlib import Path
import socket
import uvicorn
import pytest
from tests.test_agent_mcp import pg_app
from analyzer.app import create_app
from analyzer.presales_mcp import query_snapshot,TerritoryQuery
from scripts.refresh_presales import refresh
from scripts.validate_presales import run

# Independent hand-labeled business facts; do not compute expected answers from the SQL.
# project, city, buyer, hardware on this notice, extracted demand
FACT={11:('p1','420100','甲方A',[],True),12:('p1','420100','甲方A',['摄像机'],True),
 31:('p3','420200','甲方B',[],True),41:('p4','420100','甲方A',[],True),
 62:('p4','430100','甲方C',['交换机'],True),71:('p7','420100','甲方D',['交换机'],False),
 72:('p7','420200','甲方D',['摄像机'],False),81:('p8','420000','甲方E',['摄像机'],False)}
SW={'p1':['访客管理','车辆管理'],'p4':['访客管理']}
WIN={'p1':['外省供应商','联合体乙'],'p7':['外省供应商']}
def oracle(args,ids,d):
    scope=args['filters']['scope'];mode=args['mode']
    selected=[]
    for i in ids:
        k,city,buyer,hw,demand=FACT[i]
        sw=SW.get(k,[])
        if not {'business':bool(sw or hw),'hardware':bool(hw),'software':bool(sw),'both':bool(hw and sw),
            'demand':demand,'untagged_demand':k=='p3'}[scope]: continue
        if args['filters'].get('stage') and k in ('p1','p7'): continue
        if args['filters'].get('l2') and '摄像机' not in hw: continue
        selected.append(i)
    keys={FACT[i][0] for i in selected};assert d['total_projects']==len(keys)
    if mode in ('notices','projects'):
        assert {r['project_key'] for r in d['items']}==keys
        assert {r['notice_id'] for r in d['items']}==(set(selected) if mode=='notices' else {max((i for i in selected if FACT[i][0]==k),key=lambda i: (i!=11, {12:9,31:10,41:11}.get(i,0),i)) for k in keys})
    else:
        groups=defaultdict(set);types=defaultdict(lambda:defaultdict(set))
        for i in selected:
            k,reg,b,hw,_=FACT[i]
            for v in {'winners':WIN.get(k,[]),'buyers':[b],'regions':[reg],
                   'sales_items':SW.get(k,[]),'hardware_types':['设备/'+x for x in hw]}[mode]:
                groups[v].add(k)
                for x in hw: types[v][x].add(k)
        assert {r['value']:r['project_count'] for r in d['items']}=={g:len(v) for g,v in groups.items()}
        assert d['represented_projects']==len(set().union(*groups.values()))
        if mode=='winners':
            for r in d['items']:
                assert r['example_award_notice_id'] in (100,101)
                assert {h['l2']:h['project_count'] for h in r['hardware_types']}=={x:len(v) for x,v in types[r['value']].items()}

def seed(engine):
    with engine.begin() as c:
        run=c.exec_driver_sql
        run("ALTER TABLE notices ADD buyer text, ADD province_code text, ADD winner text, ADD notice_type text")
        run('CREATE TABLE lots(notice_id bigint,winner text,lot_id bigserial,lot_no text)')
        run('CREATE TABLE notice_product_tag(notice_id bigint, tag_l1 text,tag_l2 text,word_weak boolean,hits smallint default 1,evidence text,tagged_at timestamp default now())')
        run('CREATE TABLE project_product_tag(project_key text,tag_l1 text,tag_l2 text)')
        run("UPDATE source_projects SET channel='结果公告' WHERE project_key='p1'")
        run("INSERT INTO source_projects VALUES ('p7','结果公告'),('p8','采购公告')")
        for i,(k,reg,b,hw,demand) in FACT.items():
            if i>=62:
                c.exec_driver_sql("INSERT INTO notices(notice_id,project_key_name,title,channel,publish_at,revoke_status) VALUES (%s,%s,'测试采购','采购公告','2026-11-16','active')",(i,k))
            c.exec_driver_sql("UPDATE notices SET buyer=%s,region_code=%s,province_code=%s,has_procurement_content=%s,procurement_content='实际测试需求' WHERE notice_id=%s",(b,reg if i!=81 else None,reg[:2]+'0000',demand,i))
            for x in hw:
                c.exec_driver_sql("INSERT INTO notice_product_tag(notice_id,tag_l1,tag_l2,word_weak,evidence) VALUES (%s,'设备',%s,false,'设备原文依据')",(i,x))
                c.exec_driver_sql("INSERT INTO project_product_tag VALUES (%s,'设备',%s)",(k,x))
        run("INSERT INTO notice_product_tag(notice_id,tag_l1,tag_l2,word_weak) VALUES(11,'设备','摄像机',true)")
        for i,k,title,status in [(100,'p1','中标结果','active'),(101,'p7','成交公告','active'),(102,'p8','中标候选公示','active'),(103,'p8','中标结果','revoked')]:
            c.exec_driver_sql("INSERT INTO notices(notice_id,project_key_name,title,channel,publish_at,revoke_status,winner) VALUES(%s,%s,%s,'结果公告','2026-11-17',%s,'外省供应商')",(i,k,title,status))
        run("INSERT INTO lots(notice_id,winner) VALUES (100,'外省供应商'),(100,'外省供应商'),(100,'联合体乙')")

@pytest.mark.asyncio
async def test_100_questions(pg_app,tmp_path):
    old,cfg=pg_app
    e=old.state.store.engine
    seed(e)
    refresh(e,cfg.analysis_schema)
    cfg.mcp_dataset_mode='presales'
    app=create_app(cfg)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        task=asyncio.create_task(server.serve(sockets=[sock]))
        try:
            while not server.started:
                if task.done(): await task; raise RuntimeError('server did not start')
                await asyncio.sleep(.05)
            records=await run(f'http://127.0.0.1:{port}/mcp/',cfg.mcp_api_key.get_secret_value(),date(2026,11,16),
                Path('reports/presales-snapshot-100.json'),oracle)
            assert len(records)==100 and not [r['id'] for r in records if r['status']=='error'], [r['id'] for r in records if r['status']=='error']
        finally:
            server.should_exit=True
            await task
    f=TerritoryQuery(start_date='2026-11-15',end_date='2026-11-16',regions=['420000'],scope='hardware')
    assert query_snapshot(e,f,'projects',1,1)['has_more']
    all_lots=query_snapshot(e,f,'awards',1,50)
    assert all_lots['total_rows']==4 and all_lots['represented_projects']==2
    only_model=f.model_copy(update={'scope':'software','model_confirmed_only':True})
    assert [r['value'] for r in query_snapshot(e,only_model,'sales_items',1,50)['items']]==['访客管理']
    with e.begin() as c:
        c.exec_driver_sql('UPDATE notice_product_tag SET word_weak=true WHERE notice_id=12')
    refresh(e,cfg.analysis_schema)
    assert 'p1' not in {r['project_key'] for r in query_snapshot(e,f,'projects',1,50)['items']}


    # Refresh failure is not an empty dataset; readers must still see the last good build.
    with e.begin() as c:
        c.exec_driver_sql('ALTER TABLE public.lots RENAME TO unavailable_lots')
    before=query_snapshot(e,f,'awards',1,50)
    from sqlalchemy.exc import DBAPIError
    with pytest.raises(DBAPIError):
        refresh(e,cfg.analysis_schema)
    after=query_snapshot(e,f,'awards',1,50)
    assert before==after and before['total_rows']==1 and before['items'][0]['notice_id']==101
