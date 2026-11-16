"""Read-only bid-analysis embedding diagnostic. Run --help for options."""
import argparse
import hashlib
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.rows import dict_row

NAMES = ['车辆预约审核', '35114C级标准适配', '（废弃，请勿下单）国土违建图片（限定型号）',
         '企业管理数量', '交易中心基础管理']
FIELDS = ('embedding_base_url', 'embedding_model', 'embedding_dimensions',
          'embedding_query_prefix', 'embedding_document_prefix')
DEFAULTS = dict(zip(FIELDS, ('', '', 0, '', '')))
DEMO = [
    '郑州市轨道交通2026年车辆备件采购项目02标比选公告\n\n郑州市轨道交通2026年车辆备件采购项目02标，已具备采购条件，现公开邀请供应商参加比选采购活动。\n\n### **1.采购项目简介**',
    '1.4 采购项目概况：为保障郑州地铁生产维护、正常运营，现采购2026年车辆备件等物资。',
    '### **2.采购范围及相关要求**\n\n2.1 采购范围：本项目主要采购电客车继电器备件，此项目包括采购、包装、运输、装卸、保险、售后服务、质量保证期服务等，详见《第五章 采购需求》。\n\n2.2 交货期：自接到买方分批下达采购订单之日起，满足采购需求。\n\n2.3 交货地点：根据采购订单要求，送至各线路段场等指定地点。\n\n2.4 质量标准：满足国家标准、行业标准及采购需求。\n\n2.5 质保期：不少于1年。\n\n2.6供货服务期限：自合同签订之日起至2028年3月31日。',
]


def profile(cfg):
    value = json.dumps([cfg[k] for k in FIELDS], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def validate_vector(value, dimensions=0):
    if not isinstance(value, list) or not value or any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError('向量不是非空有限数值数组')
    if dimensions and len(value) != dimensions:
        raise ValueError(f'向量维度不符：实际 {len(value)}，期望 {dimensions}')
    if not math.isfinite(math.hypot(*value)) or math.hypot(*value) == 0:
        raise ValueError('向量范数为零或无效')
    return [float(v) for v in value]


def parse_vectors(payload, count, dimensions):
    data = payload.get('data', [])
    if not isinstance(data, list) or len(data) != count:
        raise ValueError('接口返回的向量数量不符')
    ordered = {}
    for row in data:
        index = row.get('index')
        if type(index) is not int or not 0 <= index < count or index in ordered:
            raise ValueError('接口返回的 index 重复或越界')
        ordered[index] = validate_vector(row.get('embedding'), dimensions)
    if len({len(v) for v in ordered.values()}) != 1:
        raise ValueError('接口返回的向量维度不一致')
    return [ordered[i] for i in range(count)]


def compare(left, right):
    try:
        left, right = validate_vector(left), validate_vector(right)
        if len(left) != len(right):
            raise ValueError(f'维度不同：{len(left)} vs {len(right)}')
        ln, rn = math.hypot(*left), math.hypot(*right)
        a, b = [v/ln for v in left], [v/rn for v in right]
        return {'cosine': max(-1.0, min(1.0, math.fsum(x*y for x, y in zip(a, b)))),
                'left_norm': ln, 'right_norm': rn,
                'raw_max_abs_diff': max(abs(x-y) for x, y in zip(left, right)),
                'normalized_max_abs_diff': max(abs(x-y) for x, y in zip(a, b))}
    except (ValueError, TypeError) as error:
        return {'error': str(error)}


def reference_comparison(records, text, vector):
    for record in records:
        if record.get('input') == text:
            return compare(vector, record.get('vector'))
    return {'skipped': '在线参考文件没有完全相同的编码文本，不跨文本比较'}


def read_database(args):
    dsn = args.dsn.replace('postgresql+psycopg://', 'postgresql://', 1)
    def table(name):
        return sql.Identifier(args.schema, name)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10,
                         options='-c default_transaction_read_only=on -c statement_timeout=30000') as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        saved = conn.execute(sql.SQL('SELECT value FROM {} WHERE key=%s').format(
            table('sales_runtime_v1')), ('settings',)).fetchone()
        cfg = DEFAULTS | ((saved or {}).get('value') or {})
        names = args.name or NAMES
        rows = conn.execute(sql.SQL(
            'SELECT id,name,description,enabled,version,vector,vector_profile,vector_version '
            'FROM {} WHERE name = ANY(%s) ORDER BY name,id LIMIT 50').format(
                table('sales_items_v1')), (names,)).fetchall()
        chunks = []
        if args.notice_id is not None:
            chunks = conn.execute(sql.SQL(
                'SELECT id,notice_id,content,embedding,embed_model,profile,source_type '
                'FROM {} WHERE notice_id=%s ORDER BY created_at DESC,id LIMIT %s').format(
                    table('sales_document_chunks_v1')), (args.notice_id, args.chunk_limit)).fetchall()
    return cfg, rows, chunks, names


def get_key(cfg, args):
    override = os.environ.get('DIAG_EMBEDDING_API_KEY')
    if override is not None:
        return override
    encrypted = cfg.get('embedding_api_key')
    if not encrypted:
        return ''
    try:
        return Fernet(Path(args.key_file).read_bytes().strip()).decrypt(encrypted.encode()).decode()
    except Exception:
        raise ValueError('无法解密页面保存的 API Key。请在原 analyzer 容器内运行，或设置 DIAG_EMBEDDING_API_KEY') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn', default=os.environ.get('DATABASE_URL', ''), help='分析库 DSN，默认 DATABASE_URL')
    parser.add_argument('--schema', default=os.environ.get('ANALYSIS_SCHEMA') or 'public')
    parser.add_argument('--key-file', default=os.environ.get('SALES_SECRET_KEY_FILE', './runtime/settings.key'))
    parser.add_argument('--name', action='append', help='精确销售项名称，可重复；默认检查已讨论的五项')
    parser.add_argument('--notice-id', type=int, help='额外检查该公告已有的本地正文/附件分片向量')
    parser.add_argument('--chunk-limit', type=int, default=3, choices=range(1, 21))
    parser.add_argument('--text-file', type=Path, help='替代内置376字需求；文件按全文编码，不额外提取')
    parser.add_argument('--reference', type=Path, default=Path(__file__).with_name('zhengzhou-relay-regenerated-vectors.json'))
    parser.add_argument('--output', type=Path, default=Path('/tmp/bid-vector-diagnostic.json') if os.name!='nt' else Path('bid-vector-diagnostic.json'))
    args = parser.parse_args()
    if not args.dsn:
        parser.error('请在 analyzer 容器内运行以自动读取 DATABASE_URL，或设置 DATABASE_URL')
    cfg, rows, chunks, names = read_database(args)
    if not rows:
        raise ValueError('未找到指定销售项，请通过 --name 传入库中精确名称')
    endpoint = cfg['embedding_base_url'].rstrip('/')
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('页面保存的向量接口地址无效')
    if not cfg['embedding_model']:
        raise ValueError('页面未配置向量模型名称')
    key = get_key(cfg, args)
    query_prefix, doc_prefix = cfg['embedding_query_prefix'], cfg['embedding_document_prefix']
    documents = [args.text_file.read_text(encoding='utf-8-sig')] if args.text_file else DEMO
    texts = [query_prefix + f"销售项：{r['name']}\n能力描述：{r['description']}" for r in rows]
    texts += [doc_prefix + text for text in documents]
    texts += [doc_prefix + row['content'] for row in chunks]
    reference = json.loads(args.reference.read_text(encoding='utf-8')) if args.reference.is_file() else {}
    reference_records = reference.get('records', [])
    vectors = []
    batch_size = max(1, min(32, int(cfg.get('embedding_batch_size', 16))))
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=float(cfg.get('embedding_timeout', 60))) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start+batch_size]
            response = client.post(endpoint if endpoint.endswith('/embeddings') else endpoint+'/embeddings',
                                   headers=headers, json={'model': cfg['embedding_model'], 'input': batch})
            if response.status_code != 200:
                raise ValueError(f'内网向量接口 HTTP {response.status_code}（未输出响应正文或密钥）')
            vectors.extend(parse_vectors(response.json(), len(batch), cfg['embedding_dimensions']))
            print(f'已重新编码 {len(vectors)}/{len(texts)} 条', flush=True)
    if len({len(v) for v in vectors}) != 1:
        raise ValueError('不同批次向量维度不一致')
    n = len(rows)
    demo_vectors = vectors[n:n+len(documents)]
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
        'model': cfg['embedding_model'], 'dimensions': len(vectors[0]),
        'query_prefix': query_prefix, 'document_prefix': doc_prefix,
        'current_vector_profile': profile(cfg),
        'candidate_threshold': cfg.get('candidate_threshold', .55),
        'missing_names': [name for name in names if name not in {r['name'] for r in rows}],
        'sales_items': [], 'stored_chunks': [],
        'reference': {'loaded': bool(reference_records), 'model': reference.get('model'),
                      'dimensions': reference.get('dimensions')},
        'notes': ['库中新旧向量比较：配置或文本版本不同，不能归因于存储损坏。',
                  '差异阈值仅用于排查，不作为精度验收标准。',
                  '只读取数据库，没有回写测试向量，不能验证写入前后是否一致。',
                  '在线参考仅在编码文本完全相同时比较；模型部署不同仍可能存在数值差异。'],
    }
    for i, row in enumerate(rows):
        old_new = compare(row['vector'], vectors[i])
        flags = []
        if row['vector_profile'] != profile(cfg):
            flags.append('库中向量配置指纹与页面当前配置不同')
        if row['vector_version'] != row['version']:
            flags.append('销售项内容已变化，库中向量版本过期')
        if old_new.get('cosine', 1) < .99:
            flags.append('新旧向量方向明显不同：核查编码服务/历史配置/数据关联')
        result = {k: row[k] for k in ('id', 'name', 'enabled', 'version', 'vector_version', 'vector_profile')}
        result.update(old_dimensions=len(row['vector']) if isinstance(row['vector'], list) else None,
                      old_vs_new=old_new, flags=flags,
                      input_sha256=hashlib.sha256(texts[i].encode()).hexdigest(),
                      fresh_vs_online_reference=reference_comparison(reference_records, texts[i], vectors[i]))
        result['demo_scores'] = [
            {'segment': j, 'stored_sales_vs_fresh_document': compare(row['vector'], doc).get('cosine'),
             'fresh_sales_vs_fresh_document': compare(vectors[i], doc).get('cosine')}
            for j, doc in enumerate(demo_vectors)]
        for side in ('stored_sales_vs_fresh_document', 'fresh_sales_vs_fresh_document'):
            valid_scores = [r[side] for r in result['demo_scores'] if r[side] is not None]
            result[side + '_max'] = max(valid_scores) if valid_scores else None
        report['sales_items'].append(result)
    for i, row in enumerate(chunks):
        new = vectors[n+len(documents)+i]
        report['stored_chunks'].append({
            'id': row['id'], 'notice_id': row['notice_id'], 'source_type': row['source_type'],
            'embed_model': row['embed_model'], 'profile': row['profile'],
            'old_vs_new': compare(row['embedding'], new),
            'sales_scores': [{'id': item['id'], 'name': item['name'],
                'stored_sales_vs_stored_chunk': compare(item['vector'], row['embedding']).get('cosine'),
                'fresh_sales_vs_fresh_chunk': compare(vectors[j], new).get('cosine')}
                for j, item in enumerate(rows)]})
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    for row in report['sales_items']:
        metric = row['old_vs_new']
        online = row['fresh_vs_online_reference']
        print(f"{row['name']} | 库中vs新生成 cosine={metric.get('cosine', metric.get('error'))}"
              f" | 内网新生成vs在线参考={online.get('cosine', online.get('skipped', online.get('error')))}")
        for flag in row['flags']:
            print('  提示：' + flag)
    if report['missing_names']:
        print('未找到：' + '、'.join(report['missing_names']))
    print(f'报告已保存：{args.output}；数据库未修改')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Connection/HTTP exception strings may contain credentials; do not print them.
        message = str(error) if type(error) is ValueError else '检查连接、表结构、文件路径或接口返回格式'
        print(f'诊断失败 [{type(error).__name__}]：{message}', file=sys.stderr)
        sys.exit(1)
