"""MaaS retrieval with explicit sales identities and original procurement evidence."""
import asyncio
import math
import re
import time

import httpx

from analyzer.procurement_extraction import extract_document_requirements, requirement_chunks
from analyzer.sales_models import ModelError
from analyzer.store import digest
from analyzer.task_trace import emit, emit_async


def identify(content, entries):
    found = re.findall(r'^[ \t]*(?:销售项ID|sales_item_id)[ \t]*[:：][ \t]*([^\r\n]+)', content, re.M | re.I)
    found = [value.strip() for value in found]
    columns = None
    for line in content.splitlines():
        if not line.strip().startswith('|'):
            columns = None
            continue
        cells = [cell.strip().replace(r'\|','|') for cell in re.split(r'(?<!\\)\|',line.strip().strip('|'))]
        if '销售项ID' in cells or 'sales_item_id' in cells:
            columns = cells.index('销售项ID') if '销售项ID' in cells else cells.index('sales_item_id')
            continue
        if columns is not None and columns<len(cells) and not re.fullmatch(r':?-+:?',cells[columns]):
            found.append(cells[columns])
    if found:
        return found[0] if len(set(found)) == 1 and any(e['id']==found[0] for e in entries) else None
    names = re.findall(r'^\s*销售项名称\s*[:：]\s*(.+?)\s*$',content,re.M)
    matched = [e['id'] for e in entries if len(names)==1 and e['name']==names[0]]
    return matched[0] if len(matched)==1 else None


class KnowledgeClient:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.owned = client is None
        self.http = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self.slots = asyncio.Semaphore(cfg['knowledge_concurrency'])

    async def aclose(self):
        if self.owned:
            await self.http.aclose()

    async def retrieve(self, query):
        cfg = self.cfg
        if not cfg['knowledge_url'] or not cfg['knowledge_id']:
            raise ValueError('请先保存知识库检索完整地址及知识库 ID')
        payload = {'knowledgeId':cfg['knowledge_id'], 'query':query,
            'topK':cfg['knowledge_top_k'], 'similarityThreshold':cfg['knowledge_threshold'],
            'retrieveStrategy':cfg['knowledge_strategy'],
            'enableRerankModel':cfg['knowledge_rerank'] or cfg['knowledge_strategy']==3,
            'rerankModel':cfg['knowledge_rerank_model'],
            'workspaceId':cfg['knowledge_workspace_id'], 'knowledgeGraph':False,
            'metadataFilteringMode':'disabled',
            'metadataFilteringConditions':{'logicalOperator':'and','conditions':[]}}
        if cfg['knowledge_result_max_length']:
            payload['resultMaxLength'] = cfg['knowledge_result_max_length']
        headers = {'api-key':cfg['knowledge_api_key']} if cfg['knowledge_api_key'] else {}
        started=time.monotonic()
        await emit_async('knowledge_waiting',query_chars=len(query))
        try:
            async with self.slots:
                await emit_async('knowledge_request',wait_seconds=round(time.monotonic()-started,3),query_chars=len(query))
                request_started=time.monotonic()
                response = await self.http.post(cfg['knowledge_url'],json=payload,headers=headers,
                                               timeout=cfg['knowledge_timeout'])
                request_seconds=round(time.monotonic()-request_started,3)
        except httpx.HTTPError as error:
            await emit_async('knowledge_error',error_type=type(error).__name__,elapsed_seconds=round(time.monotonic()-started,3))
            raise ModelError(f'知识库连接失败或超时（{type(error).__name__}）',retryable=True) from None
        if response.status_code!=200:
            await emit_async('knowledge_error',http_status=response.status_code,elapsed_seconds=round(time.monotonic()-started,3),
                 trace_id=str(response.headers.get('X-TraceId') or '')[:128])
            raise ModelError(f'知识库返回 HTTP {response.status_code}',
                             retryable=response.status_code in (408,429) or response.status_code>=500)
        try:
            body = response.json()
            if type(body.get('code')) not in (str,int) or str(body['code'])!='0':
                raise ModelError('知识库返回非成功业务状态，请检查认证、知识库 ID 和权限')
            chunks = body['data']['chunks']
            if not isinstance(chunks,list):
                raise ValueError()
            for row in chunks:
                if (not isinstance(row,dict) or not isinstance(row.get('chunkContent'),str)
                    or not isinstance(row.get('chunkId'),str)
                    or type(row.get('similarity')) not in (int,float)
                    or not math.isfinite(row['similarity'])):
                    raise ValueError()
            await emit_async('knowledge_response',http_status=response.status_code,hits=len(chunks),
                 elapsed_seconds=round(time.monotonic()-started,3),
                 request_seconds=request_seconds,
                 trace_id=str(body.get('X-TraceId') or response.headers.get('X-TraceId') or '')[:128],
                 trial_id=str(body['data'].get('trialId') or '')[:128])
            return chunks
        except (ValueError,KeyError,TypeError,AttributeError):
            raise ModelError('知识库响应格式不符合 code/data/chunks 约定') from None


def prepare_material(material, cfg):
    chunks, summaries = [], []
    totals = {key:0 for key in ('original_chars','extracted_chars','removed_chars','uncertain_chars')}
    preview_budget = 24000
    for document in material.pop('documents',[]):
        if document['source_type']=='attachment' and not cfg['collect_attachments']:
            continue
        emit('extracting_requirements',notice_id=document['notice_id'],attachment_id=document.get('attachment_id'))
        result = extract_document_requirements(document,
            correction=document['notice_id'] in material.get('correction_notice_ids',[]))
        for key in totals:
            totals[key] += result[key]
        preview = '\n'.join(s['text'] for s in result['segments'])
        if len(summaries)<50:
            limit = min(3000,preview_budget)
            summaries.append({k:v for k,v in result.items() if k!='segments'} | {
            'notice_id':document['notice_id'],'attachment_id':document.get('attachment_id'),
            'source_type':document['source_type'],'title':document.get('chunk_title',''),
            'preview':preview[:limit],'preview_truncated':len(preview)>limit})
            preview_budget -= min(len(preview),limit)
        for part in requirement_chunks(result,cfg['source_chunk_chars']):
            content_hash = digest(part['content'])
            chunks.append({**part,'chunk_id':digest([document['notice_id'],document.get('attachment_id'),part]),
                'created_at':'knowledge-text-v1','notice_id':document['notice_id'],
                'attachment_id':document.get('attachment_id'),'source_type':document['source_type'],
                'content_hash':content_hash,'procurement_extracted':True})
    material['chunks'] = chunks
    material['material_storage'] = 'maas_retrieval'
    material['procurement_extraction'] = {'version':'knowledge-demand-v1','documents':summaries,**totals}
    material['status'] = 'ready' if chunks else 'no_procurement_demand'
    return material


async def retrieve_candidates(client, chunks, entries, corrections):
    candidates, unresolved = {}, []
    by_id = {e['id']:e for e in entries}
    for index,chunk in enumerate(chunks):
        await emit_async('knowledge_chunk',notice_id=chunk['notice_id'],chunk_index=index+1,chunk_total=len(chunks))
        hits = await client.retrieve(chunk['content'])
        for hit in hits:
            sid = identify(hit['chunkContent'],entries)
            if sid is None:
                unresolved.append({'chunkId':hit['chunkId'],'content':hit['chunkContent'][:300]})
                continue
            item = by_id[sid]
            candidate = candidates.setdefault(sid,{'sales_item_id':sid,'name':item['name'],
                'description':item['description'],'item_version':item['version'],
                'score':hit['similarity'],'status':'candidate','method':'knowledge',
                'reason':'知识库召回候选，采购关系待确认','evidence':[],'knowledge_hits':[]})
            candidate['score'] = max(candidate['score'],hit['similarity'])
            key = f"{chunk['chunk_id']}:{chunk['created_at']}"
            if not any(e['key']==key for e in candidate['evidence']):
                candidate['evidence'].append({k:v for k,v in chunk.items() if k!='embedding'} | {
                    'key':key,'notice_type':'更正公告' if chunk['notice_id'] in corrections else '采购主公告'})
            candidate['knowledge_hits'].append(hit)
            evidence = next(e for e in candidate['evidence'] if e['key']==key)
            evidence.setdefault('knowledge_sources',[]).append({k:hit.get(k) for k in ('chunkId','docId','similarity')})
    for candidate in candidates.values():
        for chunk in chunks:
            key = f"{chunk['chunk_id']}:{chunk['created_at']}"
            if chunk['notice_id'] in corrections and not any(e['key']==key for e in candidate['evidence']):
                candidate['evidence'].append({**chunk,'key':key,'notice_type':'更正公告'})
        candidate['evidence'].sort(key=lambda e:e['notice_type']!='更正公告')
    return list(candidates.values()), unresolved
