"""Run a small, explicit online embedding comparison; read API key from stdin."""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL = 'Qwen/Qwen3-Embedding-0.6B'
PREFIX = ('Instruct: Given a software capability, retrieve procurement passages that '
          'express a purchasing need for that capability.\nQuery: ')
ITEMS = [
    ('车辆预约审核', '托运单位、承运单位、接收单位、访客等可以通过微信小程序或PC客户端上提交入园区的普通车辆、普通货车、危化品车辆、危废车辆四类车辆的入园申请。由管委会、园内企业审核通过后下发车辆通行权限。'),
    ('35114C级标准适配', '支持对接海康、一所、三所安全认证网关，实现35114A\\C级标准视频基础应用，按项目评估，按定制提供成果物 注意：35114的项目才需要申请，非35114项目申请后会导致无法正常登陆，默认不放开，需要定制放开'),
    ('（废弃，请勿下单）国土违建图片（限定型号）', '按照接入路数收费（房屋建设、施工车辆、施工人员、挖掘机、推土机、堆沙、堆砖头、活动板房检测） 注意：尚未加入全分析基线，ZF、ZT、GT设备可申请'),
    ('企业管理数量', '系统可支撑管理重点用车企业的数量授权，需配合【移动源政府监管端基础包】使用'),
    ('交易中心基础管理', '提供交易中心项目活动管理、招标代理基础管理、活动调控等基础管理能力'),
    ('访客管理', '访客管理提供访客预约、访客审批、访客权限管理、黑名单、访客登记、访客签离、保安核验、访客通知、通行记录与访客轨迹查看等功能。支持微信公众号、企业微信、钉钉等三方集成。支持针对多场景预约自定义表单控制。'),
    ('电客车继电器备件供货【人工对照项】', '提供地铁电客车继电器备件供货，包含包装、运输、装卸、保险、售后服务及质量保证期服务。'),
]


def main():
    key = sys.stdin.readline().strip()
    if not key:
        raise SystemExit('Missing API key on stdin')
    fixture = json.loads((ROOT / 'dist/zhengzhou-relay-extraction-v2-result.json').read_text(encoding='utf-8'))
    original = fixture['inputs']['original']
    segments = [s['text'] for s in fixture['after']['original']['segments']]
    core = '本项目主要采购电客车继电器备件，此项目包括采购、包装、运输、装卸、保险、售后服务、质量保证期服务等。'
    positive = '采购需求：建设园区访客管理系统，支持访客预约、审批、登记、签离、黑名单管理、保安核验、访客通行权限下发和通行记录查询，支持企业微信集成。'
    plain = [f'销售项：{name}\n能力描述：{description}' for name, description in ITEMS]
    documents = [original, *segments, core, positive, fixture['inputs']['flattened_truncated']]
    inputs = plain + [PREFIX + value for value in plain] + documents
    started = time.monotonic()
    try:
        response = httpx.post('https://api.siliconflow.cn/v1/embeddings',
            headers={'Authorization': f'Bearer {key}'},
            json={'model': MODEL, 'input': inputs, 'encoding_format': 'float'}, timeout=90)
    except httpx.HTTPError as error:
        raise SystemExit(f'Embedding request failed: {type(error).__name__}') from None
    if response.status_code != 200:
        raise SystemExit(f'Embedding HTTP {response.status_code}: {response.text[:800]}')
    payload = response.json()
    data = sorted(payload['data'], key=lambda row: row['index'])
    vectors = np.asarray([row['embedding'] for row in data], dtype=float)
    if vectors.shape != (len(inputs), 1024) or not np.isfinite(vectors).all():
        raise SystemExit(f'Invalid embedding shape or values: {vectors.shape}')
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise SystemExit('Zero vector returned')
    vectors /= norms
    n = len(ITEMS)
    vector_target = ROOT / 'dist/zhengzhou-relay-regenerated-vectors.json'
    snapshot = {
        'model': MODEL, 'dimensions': 1024,
        'endpoint': 'https://api.siliconflow.cn/v1/embeddings',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'normalized': True, 'query_prefix': PREFIX, 'document_prefix': '',
        'records': [
            {'kind': 'sales_plain' if i < n else 'sales_instructed' if i < 2*n else 'document',
             'name': ITEMS[i % n][0] if i < 2*n else f'document_{i-2*n}',
             'input': value, 'vector': vectors[i].tolist()}
            for i, value in enumerate(inputs)
        ],
    }
    vector_target.write_text(json.dumps(snapshot, ensure_ascii=False), encoding='utf-8')
    # Match using the saved vectors, so the artifact can reproduce the scores offline.
    saved = json.loads(vector_target.read_text(encoding='utf-8'))
    vectors = np.asarray([row['vector'] for row in saved['records']], dtype=float)
    without, instructed, docs = vectors[:n], vectors[n:2*n], vectors[2*n:]
    conditions = {
        '原文_无指令': without @ docs[0],
        '提取片段最大值_无指令': np.max(without @ docs[1:4].T, axis=1),
        '提取片段最大值_加指令': np.max(instructed @ docs[1:4].T, axis=1),
        '仅采购核心句_加指令': instructed @ docs[4],
        '访客采购正例_加指令': instructed @ docs[5],
        '去换行截断原文_无指令': without @ docs[6],
    }
    result = {'model': MODEL, 'dimensions': vectors.shape[1],
        'endpoint': 'https://api.siliconflow.cn/v1/embeddings',
        'elapsed_seconds': round(time.monotonic()-started, 2), 'usage': payload.get('usage'),
        'query_prefix': PREFIX, 'document_prefix': '',
        'vector_file': vector_target.name,
        'supplied_five_match': {
            'threshold': 0.55,
            'scores': [{'name': ITEMS[i][0], 'score': round(float(score), 6),
                        'candidate': bool(score >= 0.55)}
                       for i, score in enumerate(conditions['提取片段最大值_加指令'][:5])],
        },
        'catalog_scope': '用户提供的五个误召回销售项、访客管理，以及一个人工编写的继电器供货对照项；不是全量销售项目录',
        'extraction_chars': fixture['after']['original']['extracted_chars'],
        'cases': {condition: sorted([{'name': name, 'score': round(float(scores[i]), 6)}
             for i, (name, _) in enumerate(ITEMS)], key=lambda row: -row['score'])
             for condition, scores in conditions.items()}}
    target = ROOT / 'dist/zhengzhou-relay-online-embedding-result.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
