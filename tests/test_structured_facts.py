from decimal import Decimal
import pytest
from tests.test_agent_mcp import pg_app
from analyzer.structured_facts import catalog,FactQuery,run
from analyzer.source import SourceError

def test_discovery_multigrain_filters_and_nulls(pg_app):
    app,_=pg_app
    eng=app.state.store.engine
    with eng.begin() as c:
        c.exec_driver_sql("ALTER TABLE notices ADD budget_value numeric, ADD content_path text, ADD http_token text, ADD secret_bundle jsonb")
        c.exec_driver_sql("UPDATE notices SET budget_value=1000000 WHERE notice_id=12")
        c.exec_driver_sql("CREATE TABLE lots(lot_id bigint primary key,notice_id bigint,winner text,amount_value numeric, flags text[])")
        c.exec_driver_sql("INSERT INTO lots VALUES(1,12,'A',12,ARRAY['a','b']),(2,12,'A',13,ARRAY['b']),(3,21,NULL,NULL,NULL)")
        c.exec_driver_sql("UPDATE source_projects SET channel='结果公告' WHERE project_key='p1'")
    cols=catalog(eng,'lot')['fields']
    assert 'project.channel' in cols and 'notice.budget_value' in cols and 'lot.flags' in cols
    assert not any(n in cols for n in ['notice.content_path','notice.http_token','notice.secret_bundle'])
    def query(**kwargs):return run(eng,FactQuery(entity='lot',**kwargs))
    criteria=[dict(field='project.channel',value='结果公告'),dict(field='notice.budget_value',op='gte',value=1000000),
              dict(field='lot.flags',op='has_any',value=['b'])]
    result=query(filters=criteria,group_by=[dict(field='lot.winner'),dict(field='notice.publish_at',period='month')],
        measures=[dict(op='count'),dict(op='distinct',field='notice.project_key_name'),dict(op='sum',field='lot.amount_value')],order_by=[dict(field='count',desc=True)])
    r=result['items'][0]
    assert (r['count'],r['distinct:notice.project_key_name'],r['sum:lot.amount_value'])==(2,1,Decimal('25'))
    assert len(result['items'])==1 and r['notice.publish_at:month'].month==11
    rows=query(fields=['lot.lot_id','project.channel'],order_by=[dict(field='lot.amount_value',desc=True)],page_size=1)
    assert rows['has_more'] and rows['items'][0]['lot.lot_id']==2
    assert query(filters=[dict(field='lot.amount_value',op='is_null')],measures=[dict(op='count')])['items']==[{'count':1}]
    assert query(filters=[dict(field='lot.winner',op='contains',value="' OR 1=1 --")],measures=[dict(op='count')])['items']==[{'count':0}]
    with pytest.raises(SourceError): query(measures=[dict(op='sum',field='notice.budget_value')])
    with pytest.raises(SourceError): query(fields=['notice.content_path'])
    with pytest.raises(SourceError): query(order_by=[dict(field='lot.winner; DROP TABLE')])
