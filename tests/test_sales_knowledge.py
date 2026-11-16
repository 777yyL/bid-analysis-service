import httpx
import pytest

from analyzer.sales_knowledge import KnowledgeClient, identify
from analyzer.sales_models import ModelError, RuntimeSettings


@pytest.mark.asyncio
async def test_configured_knowledge_concurrency_is_enforced():
    import asyncio
    active=0
    maximum=0
    async def respond(request):
        nonlocal active,maximum
        active+=1
        maximum=max(maximum,active)
        await asyncio.sleep(.01)
        active-=1
        return httpx.Response(200,json={'code':'0','data':{'chunks':[]}})
    cfg=RuntimeSettings(knowledge_url='https://maas/retrieve',knowledge_id='kb',knowledge_concurrency=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client=KnowledgeClient(cfg.model_dump(),http)
        await asyncio.gather(*(client.retrieve('query') for _ in range(8)))
    assert maximum==2


@pytest.mark.asyncio
async def test_request_trace_preserves_platform_ids_without_credentials_or_text():
    from analyzer.task_trace import trace_callback
    events=[]
    token=trace_callback.set(lambda phase,details:events.append({'phase':phase,**details}))
    cfg=RuntimeSettings(knowledge_url='https://maas/retrieve',knowledge_id='kb',knowledge_api_key='private-key')
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200,json={'code':'0','data':{'chunks':[],'trialId':'trial1'}},
                           headers={'X-TraceId':'trace1'}))) as http:
            await KnowledgeClient(cfg.model_dump(),http).retrieve('private-query-text')
    finally:
        trace_callback.reset(token)
    assert [r['phase'] for r in events]==['knowledge_waiting','knowledge_request','knowledge_response']
    assert events[-1]['trace_id']=='trace1' and events[-1]['trial_id']=='trial1'
    assert events[-1]['request_seconds']>=0
    assert 'private-key' not in str(events) and 'private-query-text' not in str(events)


@pytest.mark.asyncio
async def test_knowledge_contract_and_business_errors():
    cfg = RuntimeSettings(knowledge_url='https://maas/retrieve', knowledge_id='kb1',
                          knowledge_api_key='secret', knowledge_strategy=3, knowledge_rerank=False)
    def respond(request):
        import json
        body=json.loads(request.content)
        assert request.headers['api-key']=='secret'
        assert 'Authorization' not in request.headers
        assert body['knowledgeId']=='kb1' and body['enableRerankModel']
        return httpx.Response(200,json={'code':'0','data':{'chunks':[
            {'chunkId':'c1','chunkContent':'销售项ID：item1\n能力描述：预约','similarity':.7}]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result=await KnowledgeClient(cfg.model_dump(),http).retrieve('采购需求')
        assert result[0]['chunkId']=='c1'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:
            httpx.Response(200,json={'code':'403','message':'secret'}))) as http:
        with pytest.raises(ModelError,match='业务状态'):
            await KnowledgeClient(cfg.model_dump(),http).retrieve('采购需求')


def test_mapping_never_uses_fuzzy_names_or_unknown_ids():
    entries=[{'id':'item1','name':'访客管理'},{'id':'item2','name':'访客管理'}]
    assert identify('销售项ID：item1\n销售项名称：访客管理',entries)=='item1'
    assert identify('销售项名称：访客管理',entries) is None
    assert identify('销售项ID：unknown\n销售项名称：访客管理',entries) is None


@pytest.mark.parametrize('sid,name',[
    ('Infovision OIM_LANES_OFF','动态称重车道数'),
    ('Infovision OIM_OSUR','超限基础包'),
    ('Infovision OIM_OSA','超限非现场证据审核'),
    ('Infovision_iFar_IVOL','超员检测【预售】'),
    ('Infovision OIM_SS','源头治超应用'),
])
def test_actual_excel_markdown_slices_keep_ids_with_spaces(sid,name):
    content = ('| 销售项ID | 销售项名称 | 能力描述 | 分类 | 产品 |\n'
        '| --- | --- | --- | --- | --- |\n'
        f'| {sid} | {name} | 对接称重子系统，实现数据接入和管理。 | 车辆管控 | 交通平台 |')
    assert identify(content,[{'id':sid,'name':name}])==sid
    assert identify(content,[{'id':'other','name':name}]) is None


def test_explicit_ids_with_spaces_and_multi_item_slices():
    entries=[{'id':'Infovision OIM_SS','name':'源头治超应用'},{'id':'b','name':'B'}]
    assert identify('销售项ID：Infovision OIM_SS\n能力描述：监管',entries)=='Infovision OIM_SS'
    assert identify('| 销售项ID | 销售项名称 |\n| --- | --- |\n| Infovision OIM_SS | 源头 |\n| b | B |',entries) is None


@pytest.mark.asyncio
async def test_real_request_options_and_nullable_response_fields():
    cfg=RuntimeSettings(knowledge_url='https://maas/retrieve',knowledge_id='f9c5ac92-34bb-4d25-a5a3-cf741d91253d',
        knowledge_threshold=.2,knowledge_rerank_model='Qwen3-Reranker-0.6B',knowledge_result_max_length=2)
    def respond(request):
        import json
        body=json.loads(request.content)
        assert body['resultMaxLength']==2 and body['workspaceId']==''
        assert body['knowledgeGraph'] is False
        assert body['metadataFilteringConditions']=={'logicalOperator':'and','conditions':[]}
        assert 'metadataFilterResultDto' not in body
        return httpx.Response(200,json={'code':'0','data':{'total':1,'chunks':[
            {'chunkId':'5f07fe01-7d5d-49af-b3a9-884bb7236ccb','chunkContent':'称重',
             'similarity':.9858503,'chunkTitle':None,'docToken':None,'downloadUrl':None}]},'message':'success'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert len(await KnowledgeClient(cfg.model_dump(),http).retrieve('动态称重'))==1


@pytest.mark.asyncio
async def test_real_five_hit_response_maps_to_sales_without_confirming():
    from analyzer.sales_knowledge import retrieve_candidates

    records=[
        ('Infovision OIM_LANES_OFF','动态称重车道数',.9858503,'5f07fe01-7d5d-49af-b3a9-884bb7236ccb'),
        ('Infovision OIM_OSUR','超限基础包',.73516846,'5d0493b6-f856-492e-804e-8d6a0f0dba77'),
        ('Infovision OIM_OSA','超限非现场证据审核',.66002357,'0f5de86b-0906-412b-9b94-6b8a57105b48'),
        ('Infovision_iFar_IVOL','超员检测【预售】',.34728935,'b10754b8-c5dd-4ff6-baa9-43ebe49126ed'),
        ('Infovision OIM_SS','源头治超应用',.20089743,'9807d647-39d5-476e-a9a7-6a04fa1e5df0'),
    ]
    hits=[{'chunkId':cid,'docId':'063a2d43-52dc-4207-858a-771feeb2cebb',
           'similarity':score,'chunkTitle':None,
           'chunkContent':'| 销售项ID | 销售项名称 | 能力描述 | 分类 | 产品 |\n'
                          '| --- | --- | --- | --- | --- |\n'
                          f'| {sid} | {name} | 能力描述 | 分类 | 产品 |'}
          for sid,name,score,cid in records]
    cfg=RuntimeSettings(knowledge_url='https://maas/retrieve',knowledge_id='kb').model_dump()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200,json={'code':'0','data':{'total':5,'chunks':hits}}))) as http:
        candidates,unresolved=await retrieve_candidates(KnowledgeClient(cfg,http),
            [{'chunk_id':'notice1','created_at':'test','notice_id':1,
              'content':'采购内容包括动态称重、高清抓拍、超限检测以及省级治超联网平台对接。'}],
            [{'id':sid,'name':name,'description':'能力描述','version':'v1'} for sid,name,_,_ in records],[])
    assert unresolved==[]
    assert len(candidates)==5
    assert candidates[0]['sales_item_id']=='Infovision OIM_LANES_OFF'
    assert candidates[0]['score']==.9858503
    assert all(c['status']=='candidate' for c in candidates)
    assert candidates[0]['evidence'][0]['knowledge_sources'][0]['chunkId']==records[0][3]
