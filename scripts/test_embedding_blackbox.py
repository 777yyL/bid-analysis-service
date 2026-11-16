"""Read-only, small-sample embedding discrimination test using saved model settings."""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import diagnose_embeddings as diagnostic
import httpx

PREFIX = ('Instruct: Given a software capability, retrieve procurement passages that '
          'express a purchasing need for that capability.\nQuery: ')
SAMPLES = [
    {'name':'车辆预约审核', 'description':'托运单位、承运单位、接收单位、访客等可以通过微信小程序或PC客户端上提交入园区的普通车辆、普通货车、危化品车辆、危废车辆四类车辆的入园申请。由管委会、园内企业审核通过后下发车辆通行权限。'},
    {'name':'访客管理', 'description':'访客管理提供访客预约、访客审批、访客权限管理、黑名单、访客登记、访客签离、保安核验、访客通知、通行记录与访客轨迹查看等功能。支持微信公众号、企业微信、钉钉等三方集成。支持针对多场景预约自定义表单控制。'},
    {'name':'交易中心基础管理', 'description':'提供交易中心项目活动管理、招标代理基础管理、活动调控等基础管理能力'},
]
DOCUMENTS = [
    {'id':'vehicle_direct','kind':'purchase','expected_sales':['车辆预约审核'],
     'text':'采购需求：建设园区车辆预约审核系统，支持普通车辆、货车及危化品车辆通过小程序提交入园申请，由园区企业审核后下发车辆通行权限。'},
    {'id':'vehicle_paraphrase','kind':'purchase','expected_sales':['车辆预约审核'],
     'text':'本次采购园区运输车辆入场审批软件，承运单位提前报送车牌和到访时间，管理人员批准后允许车辆进场，未经批准的车辆不得进入。'},
    {'id':'visitor_direct','kind':'purchase','expected_sales':['访客管理'],
     'text':'采购需求：建设访客管理系统，支持访客预约、审批、登记、签离、黑名单管理、保安核验、人员通行权限下发和通行记录查询。'},
    {'id':'visitor_paraphrase','kind':'purchase','expected_sales':['访客管理'],
     'text':'本次采购来访人员接待软件，外来人员在线填写来访信息，被访人确认后由门岗核验身份，离开时登记签出，并保留人员出入记录。'},
    {'id':'trading_platform_purchase','kind':'purchase','expected_sales':['交易中心基础管理'],
     'text':'本次采购公共资源交易中心业务管理平台，建设交易项目活动管理、招标代理机构基础信息维护及交易活动调控功能。'},
    {'id':'relay_parts','kind':'unrelated','expected_sales':[],
     'text':'采购范围：本项目主要采购地铁电客车继电器备件，包括包装、运输、装卸、保险、售后服务及质量保证期服务。'},
    {'id':'supplier_registration','kind':'bidding_process','expected_sales':[],
     'text':'采购范围：采购电客车继电器备件。采购文件获取方式：供应商应登录阳光采购平台完成注册和审核，获取账号密码，下载操作手册后参与报价。'},
    {'id':'bid_submission','kind':'bidding_process','expected_sales':[],
     'text':'本项目采购电客车继电器备件。投标人须在公共资源交易中心平台上传投标文件，按规定时间参加开标评标，并联系招标代理机构办理保证金手续。'},
]


def evaluate_scores(names, documents, scores):
    metrics = []
    for name, values in zip(names, scores):
        positives = [v for d,v in zip(documents, values) if name in d['expected_sales']]
        negatives = [v for d,v in zip(documents, values) if name not in d['expected_sales']]
        if not positives or not negatives:
            raise ValueError('每个销售项必须同时有正样本和负样本')
        wins = sum(1 if p>n else .5 if p==n else 0 for p in positives for n in negatives)
        ranking = sorted([{'document_id':d['id'], 'score':v,
            'expected_match':name in d['expected_sales'], 'kind':d['kind']}
            for d,v in zip(documents, values)], key=lambda r:-r['score'])
        metrics.append({'name':name, 'positive_min':min(positives), 'negative_max':max(negatives),
            'separation_gap':min(positives)-max(negatives),
            'pairwise_positive_win_rate':wins/(len(positives)*len(negatives)),
            'negative_above_055':sum(v>=.55 for v in negatives),
            'positive_count':len(positives), 'negative_count':len(negatives), 'ranking':ranking})
    by_document = [{'id':d['id'], 'kind':d['kind'], 'expected_sales':d['expected_sales'],
        'ranking':sorted([{'name':name, 'score':scores[i][j]} for i,name in enumerate(names)],
                         key=lambda r:-r['score'])} for j,d in enumerate(documents)]
    return {'per_sales_item':metrics, 'per_document':by_document}


def run(args):
    cfg, _, _, _ = diagnostic.read_database(args)
    endpoint = cfg['embedding_base_url'].rstrip('/')
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('页面保存的接口地址无效')
    if not cfg['embedding_model']:
        raise ValueError('页面尚未配置向量模型')
    key = diagnostic.get_key(cfg, args)
    texts = [f"销售项：{item['name']}\n能力描述：{item['description']}" for item in SAMPLES]
    # Controlled test inputs: both variants use identical documents without a prefix.
    inputs = texts + [PREFIX+t for t in texts] + [d['text'] for d in DOCUMENTS]
    vectors = []
    headers = {'Authorization':f'Bearer {key}'} if key else {}
    batch_size = max(1,min(16,int(cfg.get('embedding_batch_size',16))))
    with httpx.Client(trust_env=args.trust_env, follow_redirects=False,
                      timeout=float(cfg.get('embedding_timeout',60))) as client:
        for start in range(0,len(inputs),batch_size):
            batch = inputs[start:start+batch_size]
            response = client.post(endpoint if endpoint.endswith('/embeddings') else endpoint+'/embeddings',
                headers=headers, json={'model':cfg['embedding_model'],'input':batch})
            if response.status_code!=200:
                raise ValueError(f'向量接口 HTTP {response.status_code}；未输出密钥和响应正文')
            vectors.extend(diagnostic.parse_vectors(response.json(),len(batch),cfg['embedding_dimensions']))
            print(f'已生成测试向量 {len(vectors)}/{len(inputs)}',flush=True)
    if len({len(v) for v in vectors})!=1:
        raise ValueError('不同批次向量维度不一致')
    n = len(SAMPLES)
    documents = vectors[2*n:]
    variants = {}
    for mode,start in [('empty_prefix',0),('query_instruction',n)]:
        scores = [[diagnostic.compare(vector,doc)['cosine'] for doc in documents]
                  for vector in vectors[start:start+n]]
        variants[mode] = evaluate_scores([r['name'] for r in SAMPLES],DOCUMENTS,scores)
    reference = json.loads(args.reference.read_text(encoding='utf-8')) if args.reference.is_file() else {}
    ref_records = {r['input']:r['vector'] for r in reference.get('records',[])}
    reference_variants = {}
    if all(value in ref_records for value in inputs):
        ref_vectors = [ref_records[value] for value in inputs]
        for mode,start in [('empty_prefix',0),('query_instruction',n)]:
            scores = [[diagnostic.compare(vector,doc)['cosine'] for doc in ref_vectors[2*n:]]
                      for vector in ref_vectors[start:start+n]]
            reference_variants[mode] = evaluate_scores([r['name'] for r in SAMPLES],DOCUMENTS,scores)
    result = {'created_at':datetime.now(timezone.utc).isoformat(),'read_only':True,
        'model':cfg['embedding_model'],'dimensions':len(vectors[0]),
        'saved_prefixes':{'query':cfg['embedding_query_prefix'],'document':cfg['embedding_document_prefix']},
        'tested_prefixes':{'empty_prefix':'','query_instruction':PREFIX,'document':''},
        'samples':SAMPLES,'documents':DOCUMENTS,'variants':variants,
        'online_reference':{'loaded_for_identical_inputs':bool(reference_variants),
                            'model':reference.get('model'),'variants':reference_variants},
        'notes':['固定的3个销售项、8条人工样本，仅用于诊断，不代表全量准确率。',
                 '正负标签是本次测试预设的业务判断，不是模型自动生成。',
                 '空前缀/查询指令只是本次请求的临时对照，不修改页面配置。',
                 '流程文本词义上可能接近交易系统；高分不单独证明模型故障，最终仍需采购上下文判断。',
                 '分差为最弱正样本减最强负样本；正负配对胜率把同分计0.5。',
                 '不能根据这8条样本直接设定生产阈值。']}
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.save_reference:
        args.save_reference.write_text(json.dumps({'model':cfg['embedding_model'],'dimensions':len(vectors[0]),
            'records':[{'input':value,'vector':vector} for value,vector in zip(inputs,vectors)]},
            ensure_ascii=False),encoding='utf-8')
    for mode,evaluation in variants.items():
        print('\n模式：'+mode)
        for metric in evaluation['per_sales_item']:
            print(f"{metric['name']}：正样本最低 {metric['positive_min']:.4f}，"
                  f"负样本最高 {metric['negative_max']:.4f}，分差 {metric['separation_gap']:.4f}，"
                  f"正负配对胜率 {metric['pairwise_positive_win_rate']:.1%}")
    print(f'报告已保存到 {args.output}；未修改数据库或页面配置')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn',default=os.environ.get('DATABASE_URL',''))
    parser.add_argument('--schema',default=os.environ.get('ANALYSIS_SCHEMA') or 'public')
    parser.add_argument('--key-file',default=os.environ.get('SALES_SECRET_KEY_FILE','./runtime/settings.key'))
    parser.add_argument('--output',type=Path,default=Path('/tmp/embedding-blackbox-result.json'))
    parser.add_argument('--reference',type=Path,default=Path(__file__).with_name('embedding-blackbox-online-reference.json'))
    parser.add_argument('--save-reference',type=Path,help='保存本次向量作为对照文件；不包含密钥')
    parser.add_argument('--trust-env',action='store_true',help='仅本机访问公网需要系统代理时使用')
    parser.set_defaults(name=[s['name'] for s in SAMPLES],notice_id=None,chunk_limit=3)
    args = parser.parse_args()
    if not args.dsn:
        parser.error('请在 analyzer 容器内执行，以读取 DATABASE_URL')
    try:
        run(args)
    except Exception as error:
        message = str(error) if type(error) is ValueError else '请检查数据库连接、持久化配置密钥、接口及文件路径'
        print(f'测试失败 [{type(error).__name__}]：{message}',file=sys.stderr)
        sys.exit(1)


if __name__=='__main__':
    main()
