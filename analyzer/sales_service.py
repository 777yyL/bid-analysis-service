"""Read existing chunk vectors, match capabilities, preserve revision-bound evidence."""
import asyncio
import contextlib
import json
import logging
import math
import time
import traceback
import uuid
from pathlib import Path

import numpy as np
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from analyzer.procurement import procurement_sections
from analyzer.procurement_extraction import extract_requirements, requirement_chunks
from analyzer.sales_categories import category_matches
from analyzer.sales_documents import LocalDocuments
from analyzer.sales_knowledge import KnowledgeClient, prepare_material, retrieve_candidates
from analyzer.sales_models import SalesModelClient
from analyzer.sales_store import (
    SalesAudit,
    SalesData,
    SalesItem,
    SalesJob,
    SalesMatch,
    SalesProject,
    SalesStore,
    record,
    vector_profile,
)
from analyzer.sales_sync import SnapshotSource, SourceSync, sync_signature
from analyzer.source import SourceError
from analyzer.source_tables import SourceProject
from analyzer.store import digest
from analyzer.tagging import BACKGROUND, EXPERIENCE, NEGATION
from analyzer.task_trace import trace_callback

logger = logging.getLogger(__name__)


def normalized(vector):
    data = np.asarray(vector, dtype=np.float32)
    if data.ndim != 1 or not data.size or not np.isfinite(data).all():
        raise ValueError('向量包含非法值')
    norm = np.linalg.norm(data)
    if not math.isfinite(float(norm)) or norm <= 0:
        raise ValueError('向量不能为零')
    return data / norm


class SalesService(SalesStore):
    def __init__(self, store, bootstrap):
        super().__init__(store, bootstrap)
        self.clients = {}
        self.source_factory = lambda cfg: SnapshotSource(cfg, self.store)
        self.model_factory = SalesModelClient
        self.catalog_cache = None

    def save_settings(self, incoming):
        current = self.settings()
        changed = any(k in current and current[k] != v and k not in (
            'paused', 'history_enabled', 'incremental_enabled', 'engine_enabled')
            and not (k in ('source_dsn', 'embedding_api_key', 'review_api_key', 'knowledge_api_key', 'source_download_key') and v == '')
            for k, v in incoming.items())
        changed |= any(incoming.get(k + '_clear') for k in ('source_dsn', 'embedding_api_key', 'review_api_key', 'knowledge_api_key', 'source_download_key'))
        if changed:
            with self.store.session() as s:
                running = s.scalar(select(func.count()).select_from(SalesJob).where(SalesJob.status == 'running'))
            if running:
                raise ValueError('有任务正在执行；请先暂停调度并等待任务结束，再修改模型或匹配配置')
        result = super().save_settings(incoming)
        cfg = self.settings()
        self.set_state('active_vector_profile', {'value': vector_profile(cfg)})
        self.set_state('active_source_revision', {'value': digest(cfg['source_dsn'])})
        return result

    def refresh_view(self):
        if self.store.engine.dialect.name != 'postgresql':
            raise ValueError('物化视图仅支持 PostgreSQL；本地 SQLite 请使用项目查询接口')
        schema = self.store.schema or 'public'
        # schema was validated by Store; all other identifiers are constants.
        prefix = f'"{schema}".'
        sql = f'''CREATE MATERIALIZED VIEW IF NOT EXISTS {prefix}mv_sales_project_matches AS
            SELECT p.project_key, p.title, p.channel,
                CASE p.status WHEN 'matched' THEN '匹配完成' WHEN 'no_match' THEN '未发现匹配能力'
                    WHEN 'no_procurement_demand' THEN '未提取到采购需求'
                    WHEN 'needs_review' THEN '需确认' WHEN 'stage_filtered' THEN '后续阶段已跳过'
                    WHEN 'missing_primary' THEN '无采购主公告' WHEN 'missing_material' THEN '无可用采购材料'
                    ELSE '待处理' END AS analysis_status,
                coalesce(jsonb_agg(jsonb_build_object('sales_item_id', i.id, 'name', i.name,
                    'categories', i.categories, 'products', i.products, 'status',
                    CASE m.status WHEN 'confirmed' THEN '已确认' WHEN 'candidate' THEN '待确认' ELSE '已排除' END,
                    'method', m.method, 'score', m.score, 'reason', m.reason, 'evidence', m.evidence)
                    ORDER BY i.name) FILTER (WHERE i.id IS NOT NULL AND m.status <> 'excluded'), '[]'::jsonb) AS sales_items,
                p.material -> 'notices' AS notice_list, to_timestamp(p.updated_at) AS updated_at
            FROM {prefix}sales_projects_v1 p
            LEFT JOIN {prefix}sales_matches_v1 m ON m.project_key = p.project_key AND m.revision = p.revision
            LEFT JOIN {prefix}sales_items_v1 i ON i.id = m.sales_item_id AND i.enabled = true
                AND i.version = m.item_version AND (p.material ->> '_retrieval_backend' = 'maas' OR (
                i.vector_version = i.version AND i.vector_profile = (SELECT value ->> 'value' FROM {prefix}sales_runtime_v1 WHERE key = 'active_vector_profile')))
            WHERE p.material ->> '_source_revision' = (SELECT value ->> 'value' FROM {prefix}sales_runtime_v1 WHERE key = 'active_source_revision')
            GROUP BY p.project_key, p.title, p.channel, p.status, p.material, p.updated_at WITH DATA'''
        # JSON is not equality-comparable in GROUP BY; cast only the small snapshot, not source bodies.
        sql = sql.replace("p.material -> 'notices' AS notice_list", "p.material::jsonb -> 'notices' AS notice_list")
        sql = sql.replace('p.material, p.updated_at WITH DATA', 'p.material::jsonb, p.updated_at WITH DATA')
        with self.store.engine.begin() as connection:
            definition = connection.exec_driver_sql('''SELECT definition FROM pg_matviews
                WHERE schemaname=%s AND matviewname='mv_sales_project_matches' ''', (schema,)).scalar()
            if definition and ("'no_procurement_demand'" not in definition or '_retrieval_backend' not in definition):
                # This is a regenerable snapshot. Never cascade into user-owned dependent objects.
                connection.exec_driver_sql(f'DROP MATERIALIZED VIEW {prefix}mv_sales_project_matches')
            connection.exec_driver_sql(sql)
            connection.exec_driver_sql(f'CREATE UNIQUE INDEX IF NOT EXISTS mv_sales_project_matches_key_uq ON {prefix}mv_sales_project_matches(project_key)')
            connection.exec_driver_sql(f'REFRESH MATERIALIZED VIEW CONCURRENTLY {prefix}mv_sales_project_matches')
        self.set_state('view_status', {'name': f'{schema}.mv_sales_project_matches', 'refreshed_at': time.time()})

    def client(self, cfg=None):
        cfg = cfg or self.settings()
        # Pause/scheduler changes must not create another rate-limit budget.
        key = digest({k: v for k, v in cfg.items() if k.startswith(('embedding_', 'review_'))})
        if key not in self.clients:
            self.clients[key] = self.model_factory(cfg)
        return self.clients[key]

    def knowledge_client(self, cfg):
        key = 'knowledge:' + digest({k:v for k,v in cfg.items() if k.startswith('knowledge_')})
        if key not in self.clients:
            self.clients[key] = KnowledgeClient(cfg)
        return self.clients[key]

    async def close(self):
        for client in self.clients.values():
            await client.aclose()

    def require_snapshot(self, cfg=None):
        cfg = cfg or self.settings()
        if isinstance(self.source_factory(cfg), SnapshotSource):
            sync = SourceSync(self.store, cfg)
            if self.store.engine.dialect.name != 'postgresql':
                raise ValueError('本地来源表需要 PostgreSQL 分析库，请先完成时间批次同步')
            if not sync.status()['ready']:
                raise ValueError('请先同步项目与公告；数据源、阶段范围或附件开关变化后需要重新同步')

    def enqueue_download(self, cfg, snapshot, batch_id=None):
        job = self.enqueue('download_source', {'snapshot':snapshot['synced_at'],
            'url':cfg['source_download_url'], 'cursor':'', 'processed':0, 'errors':0, 'batch_id':batch_id},
            digest(['download_source', snapshot['synced_at'], cfg['source_download_url']]))
        self.set_state('download_source_job', {'id':job['id']})
        return job

    def catalog(self, cfg):
        profile = vector_profile(cfg)
        with self.store.session() as s:
            rows = list(s.scalars(select(SalesItem).where(SalesItem.enabled.is_(True)).options(defer(SalesItem.vector)).order_by(SalesItem.id)))
            if not rows:
                raise ValueError('请先导入并启用销售项')
            missing = [r.name for r in rows if r.vector_version != r.version or r.vector_profile != profile] if cfg['retrieval_backend']=='local' else []
            if missing:
                raise ValueError(f'仍有 {len(missing)} 个启用销售项待向量化')
            entries = [{c.name: getattr(r, c.name) for c in SalesItem.__table__.columns if c.name != 'vector'} for r in rows]
        signature = digest([[r['id'], r['version']] for r in entries] + [profile])
        if cfg['retrieval_backend']=='maas':
            return signature, entries, None
        if not self.catalog_cache or self.catalog_cache[0] != signature:
            with self.store.session() as s:
                vectors = dict(s.execute(select(SalesItem.id, SalesItem.vector).where(SalesItem.id.in_([r['id'] for r in entries]))).all())
            self.catalog_cache = signature, entries, np.stack([normalized(vectors[r['id']]) for r in entries])
        else:
            self.catalog_cache = signature, entries, self.catalog_cache[2]
        return self.catalog_cache

    def candidates(self, material, cfg, catalog):
        _, entries, matrix = catalog
        candidates = {}
        for chunk in material.get('chunks', []):
            vector = normalized(chunk['embedding'])
            if chunk['embed_model'] != cfg['embedding_model'] or len(vector) != matrix.shape[1]:
                raise ValueError('公告与销售项向量模型或维度不兼容；未生成匹配结果')
            if chunk.get('procurement_extracted'):
                role = 'demand' if chunk.get('section_role') == 'demand' else 'unknown'
                sections = {'spans':[{'text':chunk['content'], 'role':role}], 'has_demand':role=='demand'}
            else:
                sections = procurement_sections(chunk['content'])
            usable = [p for p in sections['spans'] if p['role'] != 'process' and p['text'].strip()]
            if not usable:
                continue
            scores = matrix @ vector
            for index in np.argsort(-scores)[:cfg['candidate_limit']]:
                score, item = float(scores[index]), entries[int(index)]
                if score < cfg['candidate_threshold']:
                    continue
                key = f"{chunk['chunk_id']}:{chunk['created_at']}"
                evidence = {k: chunk.get(k) for k in ('chunk_id', 'created_at', 'notice_id', 'attachment_id',
                    'source_type', 'content', 'page_start', 'page_end', 'char_start', 'char_end', 'content_hash')}
                evidence.update(key=key, section_role='demand' if sections['has_demand'] else 'unknown',
                    notice_type='更正公告' if chunk['notice_id'] in material.get('correction_notice_ids', []) else '采购主公告')
                previous = candidates.get(item['id'])
                if previous is None:
                    previous = candidates[item['id']] = {'sales_item_id': item['id'], 'name': item['name'],
                        'description': item['description'], 'item_version': item['version'], 'score': score,
                        'status': 'candidate', 'method': 'vector', 'reason': '能力语义相近，采购关系待确认', 'evidence': []}
                previous['score'] = max(previous['score'], score)
                if not any(e['key'] == key for e in previous['evidence']):
                    previous['evidence'].append(evidence)
                exact = any(item['name'] in p['text'] and p['role'] == 'demand'
                    and not (EXPERIENCE.search(p['text']) or BACKGROUND.search(p['text']) or NEGATION.search(p['text']))
                    for p in usable)
                if exact and sections['has_demand'] and score >= cfg['confirm_threshold'] and not material.get('correction_notice_ids'):
                    previous.update(status='confirmed', reason='明确采购上下文命中销售项名称，能力相似度通过阈值')
        for candidate in candidates.values():
            for chunk in material.get('chunks', []):
                if chunk['notice_id'] not in material.get('correction_notice_ids', []):
                    continue
                key = f"{chunk['chunk_id']}:{chunk['created_at']}"
                if not any(e['key'] == key for e in candidate['evidence']):
                    candidate['evidence'].append({k: v for k, v in chunk.items() if k != 'embedding'} | {
                        'key': key, 'notice_type': '更正公告', 'section_role': 'unknown'})
            candidate['evidence'].sort(key=lambda e: e['notice_type'] != '更正公告')
        return list(candidates.values())

    async def review_candidate(self, candidate, cfg, revision, corrections, units=None):
        if units and len(units) > 1:
            children = []
            for unit in units:
                notice_ids = {unit['main_notice_id'], *unit.get('correction_notice_ids', [])}
                sources = [e for e in candidate['evidence'] if e.get('notice_id') in notice_ids]
                if not sources:
                    continue
                child = {**candidate, 'evidence': sources}
                await self.review_candidate(child, cfg, revision, unit.get('correction_notice_ids', []))
                children.append(child)
            states = {c['status'] for c in children}
            status = 'confirmed' if 'confirmed' in states else 'excluded' if states == {'excluded'} else 'candidate'
            candidate.update(status=status, method='model', reason='分别核验各采购单元后汇总，任一单元存在明确能力需求即可命中')
            candidate['evidence'] = list({e['key']: e for child in children if child['status'] == status for e in child['evidence']}.values())
            return
        base = {k: candidate[k] for k in ('sales_item_id', 'name', 'description')}
        rank = {nid: index + 1 for index, nid in enumerate(corrections)}
        evidence = candidate['evidence']
        groups = {}
        for source in evidence:
            groups.setdefault(rank.get(source.get('notice_id'), 0), []).append(source)
        chosen, chosen_rank, states, citations = None, -1, set(), set()
        oversized = False
        # Each evidence group belongs to one effective correction rank; later ranks take precedence.
        for priority, sources in sorted(groups.items()):
            context = []
            current_base = dict(base)
            if chosen is not None and priority > chosen_rank:
                current_base['previous_judgment'] = {'status': chosen['status'], 'reason': chosen['reason'],
                    'instruction': '结合原采购证据确认本次更正是否修改该能力；未涉及则保留原需求'}
                context = [e for e in evidence if e['key'] in citations][:2]
            overhead = len(json.dumps(current_base, ensure_ascii=False)) + len(json.dumps(context, ensure_ascii=False)) + 2000
            batches, batch, size = [], [], overhead
            for source in sources:
                cost = len(json.dumps(source, ensure_ascii=False)) + 2
                if cost + overhead > cfg['review_max_input_chars']:
                    oversized = True
                    continue
                if batch and (len(batch) >= 15 or size + cost > cfg['review_max_input_chars']):
                    batches.append(batch)
                    batch, size = [], overhead
                batch.append(source)
                size += cost
            if batch:
                batches.append(batch)
            for batch in batches:
                combined = list({e['key']: e for e in [*context, *batch]}.values())
                cache_key = 'review:' + digest([revision, current_base, combined, cfg['review_model'], cfg['review_base_url']])
                result = self.state(cache_key)
                if result is None:
                    result = (await self.client(cfg).review([current_base], combined))[0]
                    self.set_state(cache_key, result)
                if priority > chosen_rank:
                    chosen, chosen_rank, states, citations = result, priority, set(), set()
                if priority == chosen_rank:
                    states.add(result['status'])
                    citations.update(result['evidence_keys'])
                    if result['status'] == 'confirmed':
                        chosen = result
        if not chosen or oversized or len(states) > 1:
            candidate.update(status='candidate', method='model', reason='证据存在冲突或有片段超过模型预算，保留待人工核验')
            return
        candidate.update(status=chosen['status'], method='model', reason=chosen['reason'])
        candidate['evidence'] = [e for e in evidence if e['key'] in citations]

    async def vectorize(self, job, cfg):
        ids = job['payload'].get('ids') or []
        with self.store.session() as s:
            stmt = select(SalesItem).where(SalesItem.enabled.is_(True))
            if ids:
                stmt = stmt.where(SalesItem.id.in_(ids))
            items = [record(r) for r in s.scalars(stmt.order_by(SalesItem.id))]
        if not ids:
            items = [r for r in items if r['vector'] is None or r['vector_version'] != r['version']
                     or r['vector_profile'] != vector_profile(cfg)]
        if not items:
            self.update_job(job, progress={'processed': 0, 'total': 0, 'message': '没有待向量化销售项'})
            return
        for offset in range(0, len(items), cfg['embedding_batch_size']):
            batch = items[offset:offset + cfg['embedding_batch_size']]
            vectors = await self.client(cfg).embed_sales(batch)
            with self.store.session.begin() as s:
                if vector_profile(self.settings()) != vector_profile(cfg):
                    raise ValueError('向量配置已变化，旧任务结果未采纳')
                for item, vector in zip(batch, vectors, strict=True):
                    normalized(vector)
                    row = s.get(SalesItem, item['id'], with_for_update=True)
                    if row and row.enabled and row.version == item['version']:
                        row.vector, row.vector_profile, row.vector_version = vector, vector_profile(cfg), row.version
                        row.vector_error, row.updated_at = None, time.time()
            self.update_job(job, progress={'processed': min(offset + len(batch), len(items)), 'total': len(items)})
        self.catalog_cache = None

    async def analyze(self, job, cfg):
        key = job['payload']['project_key']
        if not job['payload'].get('force') and (
                isinstance(self.source_factory(cfg), SnapshotSource) or job['payload'].get('preserve_existing')):
            with self.store.session() as session:
                existing = session.get(SalesProject, key)
                if existing and existing.material.get('_analysis_job_id')!=job['id']:
                    self.update_job(job, progress={'phase':'existing_result_retained','project_key':key})
                    return
        self.update_job(job, progress={'phase': 'reading_project', 'project_key': key})
        source = self.source_factory(cfg)
        material = await asyncio.to_thread(source.project_material, key, cfg['allow_later_stages'])
        material['_analysis_job_id'] = job['id']
        if 'documents' in material and material.get('status') == 'ready':
            def progress(value):
                if not self.update_job(job, progress=value):
                    raise asyncio.CancelledError()
            if cfg['retrieval_backend']=='maas':
                material = prepare_material(material,cfg)
            else:
                catalog = await asyncio.to_thread(self.catalog, cfg)
                material = await LocalDocuments(self.store, cfg).prepare(material, self.client(cfg), progress, catalog[2].shape[1])
        material['_source_revision'] = digest(cfg['source_dsn'])
        material['_retrieval_backend'] = cfg['retrieval_backend']
        status = material.get('status', 'ready')
        if status == 'stage_filtered':
            with self.store.session.begin() as s:
                self.check_snapshot(s, material)
                previous = s.get(SalesProject, key)
                if previous:
                    previous.channel, previous.status = material.get('channel') or '未知', 'stage_filtered'
                    previous.updated_at = time.time()
                    return
        if status not in ('selected', 'ready'):
            self.save_project(job, material, digest([status, material.get('channel')]), status, [])
            return
        catalog = await asyncio.to_thread(self.catalog, cfg)
        matching_config = {k: cfg[k] for k in ('source_dsn', 'candidate_limit', 'candidate_threshold', 'confirm_threshold',
            'review_enabled', 'review_base_url', 'review_model', 'review_max_input_chars', 'review_max_output_tokens', 'no_hit_sample_percent', 'collect_attachments')}
        matching_config.update({k:v for k,v in cfg.items() if k.startswith('knowledge_') and k!='knowledge_api_key'})
        matching_config['retrieval_backend'] = cfg['retrieval_backend']
        matching_config['manual_confirmation_enabled'] = cfg['manual_confirmation_enabled']
        if cfg['low_score_review_enabled']:
            matching_config.update({k:cfg[k] for k in ('low_score_review_enabled','review_score_threshold')})
        revision = digest([catalog[0], matching_config,
            material.get('main_notice_id'), material.get('correction_notice_ids'), material.get('units'),
            [[c['chunk_id'], c['created_at'], digest(c['content']), c.get('embed_model'), digest(c.get('embedding'))] for c in material.get('chunks', [])]])
        with self.store.session() as s:
            previous = s.get(SalesProject, key)
            if previous and previous.status != 'stage_filtered' and previous.revision == revision and not job['payload'].get('force') and not (
                    job['attempts'] > 1 and previous.status == 'needs_review'):
                # Refresh metadata from the new snapshot without repeating matching or model calls.
                with self.store.session.begin() as write:
                    owned = write.scalar(select(SalesJob.id).where(SalesJob.id == job['id'],
                        SalesJob.owner == job['owner'], SalesJob.status == 'running').with_for_update())
                    if not owned:
                        raise ValueError('任务执行权已失效，元数据未更新')
                    self.check_snapshot(write, material)
                    row = write.get(SalesProject, key, with_for_update=True)
                    if row and row.revision == revision:
                        row.channel, row.title = material.get('channel') or '未知', material.get('title') or key
                        row.material = {k:v for k,v in material.items() if k not in ('chunks', 'documents')}
                return
        self.update_job(job, progress={'phase': 'matching', 'chunks': len(material.get('chunks', []))})
        if cfg['retrieval_backend']=='maas':
            candidates, unresolved = await retrieve_candidates(self.knowledge_client(cfg),material.get('chunks',[]),catalog[1],material.get('correction_notice_ids',[]))
            if unresolved:
                raise ValueError('知识库返回切片无法关联启用的销售项；请检查切片中的销售项ID与本地目录，未采纳本次结果')
        else:
            candidates = await asyncio.to_thread(self.candidates, material, cfg, catalog)
        if cfg['retrieval_backend']=='local' and not candidates and cfg['review_enabled'] and int(digest(key)[:8], 16) % 100 < cfg['no_hit_sample_percent']:
            sample_cfg = {**cfg, 'candidate_threshold': -1.0, 'confirm_threshold': 1.0, 'candidate_limit': min(3, cfg['candidate_limit'])}
            candidates = await asyncio.to_thread(self.candidates, material, sample_cfg, catalog)
            for candidate in candidates:
                candidate.update(status='candidate', reason='无命中低比例抽检',sample_only=True)
        if cfg['low_score_review_enabled']:
            for candidate in candidates:
                if candidate['score']<cfg['review_score_threshold'] and candidate['status']=='confirmed':
                    candidate['status']='candidate'
            low = [c for c in candidates if c['status']=='candidate' and c['score']<cfg['review_score_threshold']]
            if low and (not cfg['review_enabled'] or not cfg['review_base_url'] or not cfg['review_model']):
                self.save_project(job, material, revision, 'needs_review', candidates, catalog[0], cfg)
                raise ValueError('低分候选需模型复核，请在系统配置开启复核并配置接口及模型；本次低分候选未自动打标签')
        if cfg['review_enabled']:
            self.save_project(job, material, revision, 'needs_review', candidates, catalog[0], cfg)
            for candidate in [c for c in candidates if c['status'] == 'candidate']:
                if cfg['low_score_review_enabled'] and candidate['score']>=cfg['review_score_threshold']:
                    continue
                self.update_job(job, progress={'phase': 'model_review', 'sales_item': candidate['name']})
                await self.review_candidate(candidate, cfg, revision, material.get('correction_notice_ids', []), material.get('units'))
                self.save_project(job, material, revision, 'needs_review', candidates, catalog[0], cfg)
        for candidate in candidates:
            if candidate['status']=='excluded':
                continue
            if cfg['manual_confirmation_enabled']:
                candidate['status']='candidate'
                candidate['reason'] += '；等待人工确认'
            elif candidate['status']=='candidate':
                if cfg['low_score_review_enabled'] and candidate['score']<cfg['review_score_threshold']:
                    candidate['reason'] += '；低分复核依据不足，保留待确认，不自动生成标签'
                elif candidate.get('sample_only'):
                    candidate.update(status='excluded',reason='无命中抽检未确认，不自动生成标签')
                else:
                    candidate.update(status='confirmed',method=candidate['method']+'_auto',
                        reason=candidate['reason']+'；人工确认关闭，自动采纳为标签（未经人工确认）')
        status = 'needs_review' if any(c['status'] == 'candidate' for c in candidates) else 'matched' if any(
            c['status'] == 'confirmed' for c in candidates) else 'no_match'
        self.save_project(job, material, revision, status, candidates, catalog[0], cfg)

    def check_snapshot(self, session, material):
        if material.get('_source_project_revision'):
            try:
                project = session.get(SourceProject, (material['_source_revision'], material['project_key']),
                                      with_for_update={'nowait':True})
            except SQLAlchemyError:
                raise SourceError('本项目来源材料正在更新，分析稍后重试',retryable=True) from None
            if not project or project.revision != material['_source_project_revision']:
                raise SourceError('本项目来源材料已变化，旧分析未采纳，稍后重试',retryable=True)
            return
        if '_snapshot_at' not in material:
            return
        try:
            sync = session.get(SalesData, 'source_sync', with_for_update={'nowait': True})
        except SQLAlchemyError:
            raise SourceError('快照正在同步，分析任务稍后自动重试', retryable=True) from None
        if not sync or sync.value.get('synced_at') != material['_snapshot_at']:
            raise SourceError('项目快照已更新，旧分析未采纳，稍后使用新快照重试', retryable=True)

    def save_project(self, job, material, revision, status, matches, signature=None, cfg=None):
        if cfg and (vector_profile(self.settings()) != vector_profile(cfg) or self.catalog(cfg)[0] != signature):
            raise ValueError('销售项或向量配置已更新，旧匹配结果未采纳')
        key = material['project_key']
        with self.store.session.begin() as s:
            owned = s.scalar(select(SalesJob).where(SalesJob.id == job['id'], SalesJob.owner == job['owner'],
                SalesJob.status == 'running').with_for_update())
            if not owned:
                raise ValueError('任务执行权已失效，结果未保存')
            self.check_snapshot(s, material)
            row = s.get(SalesProject, key, with_for_update=True)
            changed_revision = row is not None and row.revision != revision
            if changed_revision:
                previous_matches = [record(m) for m in s.scalars(select(SalesMatch).where(SalesMatch.project_key == key))]
                s.add(SalesAudit(id=str(uuid.uuid4()), project_key=key,
                    payload={'revision': row.revision, 'status': row.status, 'matches': previous_matches}, created_at=time.time()))
            if row is None:
                row = SalesProject(project_key=key)
                s.add(row)
            row.title, row.channel = material.get('title') or key, material.get('channel') or '未知'
            row.status, row.revision, row.updated_at = status, revision, time.time()
            row.material = {k: v for k, v in material.items() if k != 'chunks'}
            manuals = list(s.scalars(select(SalesMatch).where(SalesMatch.project_key == key, SalesMatch.method.in_(['manual', 'manual_stale']))))
            manual_ids = {r.sales_item_id for r in manuals}
            for manual in manuals:
                manual.revision = revision
                if changed_revision:
                    manual.method, manual.status = 'manual_stale', 'candidate'
                    manual.reason = '采购需求或能力版本已变化，原人工结果保留，需重新核验'
                    replacement = next((m for m in matches if m['sales_item_id'] == manual.sales_item_id), None)
                    if replacement:
                        manual.evidence, manual.item_version = replacement['evidence'], replacement['item_version']
                    row.status = 'needs_review'
            if any(m.method == 'manual_stale' and m.status == 'candidate' for m in manuals):
                row.status = 'needs_review'
            s.execute(delete(SalesMatch).where(SalesMatch.project_key == key, SalesMatch.method.not_in(['manual', 'manual_stale'])))
            for match in matches:
                if match['sales_item_id'] not in manual_ids:
                    s.add(SalesMatch(id=digest([key, match['sales_item_id']]), project_key=key,
                        revision=revision, **{k: match[k] for k in ('sales_item_id', 'item_version', 'status', 'method', 'score', 'reason', 'evidence')}))
            if status in ('matched', 'no_match', 'needs_review'):
                s.flush()
                self.update_project_status(s, row)

    @staticmethod
    def update_project_status(session, project):
        statuses = list(session.scalars(select(SalesMatch.status).join(SalesItem, SalesItem.id == SalesMatch.sales_item_id).where(
            SalesMatch.project_key == project.project_key, SalesMatch.revision == project.revision,
            SalesItem.enabled.is_(True), SalesMatch.item_version == SalesItem.version)))
        project.status = 'needs_review' if 'candidate' in statuses else 'matched' if 'confirmed' in statuses else 'no_match'

    def project_metrics(self):
        cfg = self.settings()
        p, m, i = SalesProject, SalesMatch, SalesItem
        scope = select(p.project_key, p.revision, p.status).where(
            p.material['_source_revision'].as_string() == digest(cfg['source_dsn']),
            p.status != 'stage_filtered')
        if not cfg['allow_later_stages']:
            scope = scope.where(p.channel.in_(['采购意向', '采购公告', '更正公告']))
        scope = scope.cte('metric_projects')
        flags = select(m.project_key,
            func.max(case((m.status == 'confirmed', 1), else_=0)).label('hit'),
            func.max(case((m.status == 'candidate', 1), else_=0)).label('review')).join(
                scope, (m.project_key == scope.c.project_key) & (m.revision == scope.c.revision)).join(
                i, (i.id == m.sales_item_id) & (i.version == m.item_version)).where(
                i.enabled.is_(True), m.status.in_(['confirmed', 'candidate']))
        if cfg['retrieval_backend'] == 'local':
            flags = flags.where(i.vector_profile == vector_profile(cfg), i.vector_version == i.version)
        flags = flags.group_by(m.project_key).subquery()
        analyzed = ['matched', 'no_match', 'needs_review', 'no_procurement_demand']
        statement = select(func.count().label('recorded'),
            func.count().filter(scope.c.status.in_(analyzed)).label('analyzed'),
            func.coalesce(func.sum(flags.c.hit), 0).label('matched'),
            func.coalesce(func.sum(flags.c.review), 0).label('needs_review'),
            func.count().filter(scope.c.status.in_(['missing_material', 'missing_primary'])).label('missing_material')
        ).select_from(scope.outerjoin(flags, flags.c.project_key == scope.c.project_key))
        with self.store.session() as session:
            values = dict(session.execute(statement).mappings().one())
        snapshot = SourceSync(self.store, cfg).status()
        current_snapshot = snapshot['ready'] and snapshot.get('source_scope') == digest(cfg['source_dsn'])
        total = snapshot.get('counts', {}).get('projects', values['recorded']) if current_snapshot else values['recorded']
        return {**values, 'total':total, 'not_analyzed':max(0, total-values['analyzed']),
                'hit_rate':round(values['matched'] / values['analyzed'] * 100, 1) if values['analyzed'] else 0,
                'snapshot_ready':bool(current_snapshot), 'synced_at':snapshot.get('synced_at') if current_snapshot else None}

    def projects(self, q='', category=None, sales_item_ids=None, match_mode='any', page=1, page_size=50, key=None, product=None, hit=None, min_score=None):
        cfg = self.settings()
        with self.store.session() as s:
            valid = {r.id: r for r in s.scalars(select(SalesItem).options(defer(SalesItem.vector)).where(SalesItem.enabled.is_(True)))}
            stmt = select(SalesProject)
            stmt = stmt.where(SalesProject.material['_source_revision'].as_string() == digest(cfg['source_dsn']))
            if key is not None:
                stmt = stmt.where(SalesProject.project_key == key)
            elif not cfg['allow_later_stages']:
                stmt = stmt.where(SalesProject.channel.in_(['采购意向', '采购公告', '更正公告']))
            if key is None:
                stmt = stmt.where(SalesProject.status != 'stage_filtered')
            if q:
                stmt = stmt.where(or_(SalesProject.title.icontains(q, autoescape=True), SalesProject.project_key.icontains(q, autoescape=True)))
            ids = list(sales_item_ids or [])
            if category:
                category_ids = {i for i, r in valid.items() if category_matches(r.categories,category)}
                ids = list(set(ids) & category_ids) if ids else list(category_ids)
                if not ids:
                    return {'items': [], 'total': 0}
            if product:
                product_ids={i for i,r in valid.items() if product in r.products}
                ids=list(set(ids)&product_ids) if ids else list(product_ids)
                if not ids:
                    return {'items':[],'total':0}
            if hit is not None or min_score is not None:
                matched=select(SalesMatch.id).join(SalesItem,SalesItem.id==SalesMatch.sales_item_id).where(
                    SalesMatch.project_key==SalesProject.project_key,SalesMatch.revision==SalesProject.revision,
                    SalesMatch.status=='confirmed',SalesItem.enabled.is_(True),SalesMatch.item_version==SalesItem.version,
                    SalesItem.id.in_([i for i,r in valid.items() if cfg['retrieval_backend']=='maas' or r.vector_profile==vector_profile(cfg)])).correlate(SalesProject).exists()
                if min_score is not None:
                    above=select(SalesMatch.id).join(SalesItem,SalesItem.id==SalesMatch.sales_item_id).where(
                        SalesMatch.project_key==SalesProject.project_key,SalesMatch.revision==SalesProject.revision,
                        SalesMatch.status=='confirmed',SalesMatch.score>min_score,
                        SalesItem.enabled.is_(True),SalesMatch.item_version==SalesItem.version,
                        SalesItem.id.in_([i for i,r in valid.items() if cfg['retrieval_backend']=='maas' or r.vector_profile==vector_profile(cfg)])).correlate(SalesProject).exists()
                    stmt=stmt.where(above)
                if hit is not None and (min_score is None or hit is False):
                    stmt=stmt.where(matched if hit else ~matched)
            if ids:
                groups = select(SalesMatch.project_key).join(SalesProject, SalesProject.project_key == SalesMatch.project_key).join(SalesItem,SalesItem.id==SalesMatch.sales_item_id).where(
                    SalesMatch.item_version==SalesItem.version,
                    SalesMatch.sales_item_id.in_([i for i in ids if i in valid]), SalesMatch.status == 'confirmed',
                    SalesMatch.revision == SalesProject.revision,
                    SalesMatch.sales_item_id.in_([i for i, r in valid.items() if cfg['retrieval_backend']=='maas' or (r.vector_version == r.version
                        and r.vector_profile == vector_profile(cfg))])).group_by(SalesMatch.project_key)
                if match_mode == 'all':
                    groups = groups.having(func.count(func.distinct(SalesMatch.sales_item_id)) == len(ids))
                if min_score is not None:
                    groups=groups.where(SalesMatch.score>min_score)
                stmt = stmt.where(SalesProject.project_key.in_(groups))
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            paged=stmt.order_by(SalesProject.updated_at.desc()).offset((page-1)*page_size).limit(page_size)
            material_fields=('main_notice_id','main_notice_ids','correction_notice_ids','selection_reason','warnings','notices','material_storage','vectorization','collect_attachments')
            if key is None:
                columns=[c for c in SalesProject.__table__.columns if c.name!='material']
                columns += [SalesProject.material[field].label(field) for field in material_fields]
                rows=[dict(row) for row in s.execute(paged.with_only_columns(*columns)).mappings()]
            else:
                rows=[record(row) for row in s.scalars(paged)]
            revisions, matches = {r['project_key']: r['revision'] for r in rows}, {}
            if rows:
                query=select(SalesMatch).where(SalesMatch.project_key.in_(list(revisions)))
                if key is None:
                    query=query.options(defer(SalesMatch.evidence))
                if min_score is not None:
                    query=query.where(SalesMatch.status=='confirmed',SalesMatch.score>min_score)
                for match in s.scalars(query):
                    item = valid.get(match.sales_item_id)
                    if not item or match.revision != revisions[match.project_key] or match.item_version != item.version or (cfg['retrieval_backend']=='local' and item.vector_profile != vector_profile(cfg)):
                        continue
                    value = {c.name:getattr(match,c.name) for c in SalesMatch.__table__.columns if key is not None or c.name!='evidence'}
                    value.update(name=item.name, description=item.description, categories=item.categories, products=item.products)
                    matches.setdefault(match.project_key, []).append(value)
            output = []
            for row in rows:
                value = dict(row)
                if key is not None:
                    material = value.pop('material')
                    value.update({k: material.get(k) for k in (*material_fields,'procurement_extraction')})
                value['matches'] = matches.get(row['project_key'], [])
                if key is not None:
                    value['history'] = [record(a) for a in s.scalars(select(SalesAudit).where(SalesAudit.project_key == key)
                        .order_by(SalesAudit.created_at.desc()).limit(20))]
                output.append(value)
            return {'items': output, 'total': total}

    def review(self, key, payload):
        with self.store.session.begin() as s:
            project = s.get(SalesProject, key, with_for_update=True)
            if not project:
                raise KeyError(key)
            if payload.get('expected_revision') != project.revision:
                raise ValueError('采购需求版本已变化，请刷新后重新确认')
            item = s.get(SalesItem, payload['sales_item_id'])
            if not item or not item.enabled:
                raise ValueError('销售项不存在或已禁用')
            if payload.get('status') not in ('confirmed', 'excluded') or not str(payload.get('reason', '')).strip():
                raise ValueError('人工确认需要状态和原因')
            row = s.get(SalesMatch, digest([key, item.id]))
            if row is None:
                raise ValueError('请先匹配并核验采购原文，不支持无证据新增标签')
            s.add(SalesAudit(id=str(uuid.uuid4()), project_key=key, payload={'event': 'manual_review',
                'previous': record(row), 'requested': payload}, created_at=time.time()))
            row.method, row.status, row.reason = 'manual', payload['status'], payload['reason']
            row.revision, row.item_version = project.revision, item.version
            project.updated_at = time.time()
            s.flush()
            self.update_project_status(s, project)

    async def search(self, text, limit=10):
        cfg = self.settings()
        extraction = extract_requirements(text)
        inputs = list(requirement_chunks(extraction, cfg['source_chunk_chars']))
        summary = {k:v for k,v in extraction.items() if k!='segments'}
        summary['preview'] = '\n'.join(segment['text'] for segment in extraction['segments'])[:12000]
        if not inputs:
            return {'items':[], 'total':0, 'extraction':summary, 'result_type':'candidates',
                    'message':'未提取到采购需求，未对全文向量化'}
        catalog = await asyncio.to_thread(self.catalog, cfg)
        if cfg['retrieval_backend']=='maas':
            chunks = [{**p,'chunk_id':digest(p),'created_at':'test','notice_id':0,'source_type':'notice'} for p in inputs]
            candidates, unresolved = await retrieve_candidates(self.knowledge_client(cfg),chunks,catalog[1],[])
            by_id = {e['id']:e for e in catalog[1]}
            items = [{k:v for k,v in by_id[c['sales_item_id']].items() if k!='vector'} | {
                'score':c['score'],'status':'candidate','matched_text':'\n'.join(e['content'] for e in c['evidence'])}
                for c in sorted(candidates,key=lambda c:-c['score'])[:limit]]
            return {'items':items,'total':len(items),'extraction':summary,'result_type':'candidates','unresolved':unresolved}
        vectors = await self.client(cfg).embed_documents([chunk['content'] for chunk in inputs])
        if len(vectors) != len(inputs):
            raise ValueError('检索向量数量与需求分片数量不一致')
        scores = np.full(len(catalog[1]), -np.inf)
        best = np.zeros(len(catalog[1]), dtype=int)
        for index, vector in enumerate(vectors):
            vector = normalized(vector)
            if len(vector) != catalog[2].shape[1]:
                raise ValueError('查询向量与销售项维度不一致')
            values = catalog[2] @ vector
            improved = values > scores
            best[improved], scores[improved] = index, values[improved]
        indexes = [int(i) for i in np.argsort(-scores) if scores[i]>=cfg['candidate_threshold']][:limit]
        items = [{k:v for k,v in catalog[1][i].items() if k!='vector'} | {
            'score':float(scores[i]), 'status':'candidate', 'matched_text':inputs[int(best[i])]['content']}
            for i in indexes]
        return {'items':items, 'total':len(items), 'extraction':summary, 'result_type':'candidates',
                'message':'仅为语义召回候选，不代表实际采购命中；正式判定请使用单个项目测试'}

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(20)
            if not self.update_job(job, lease_until=time.time()+90):
                return

    async def run_once(self):
        cfg = self.settings()
        maintenance_only = not cfg['engine_enabled'] or cfg['paused']
        kinds = ['sync_source'] if maintenance_only else None
        if not maintenance_only and isinstance(self.source_factory(cfg), SnapshotSource):
            if not SourceSync(self.store, cfg).status()['ready']:
                # Preserve old queued analyses until a matching local snapshot is ready.
                kinds = ['sync_source', 'vectorize', 'refresh_view','review_history','review_existing']
        job = await asyncio.to_thread(self.claim,kinds=kinds)
        if not job:
            return False
        def trace(phase,details):
            if not self.update_job(job,progress={'phase':phase,**details}):
                raise ValueError('任务执行权已失效，停止旧任务')
        token=trace_callback.set(trace)
        heartbeat = asyncio.create_task(self.heartbeat(job))
        try:
            self.update_job(job,progress={'phase':'task_started','started_at':time.time()})
            if job['attempts'] > 3:
                raise ValueError('超过最大重试次数，请检查配置后手动重试')
            if job['kind'] == 'vectorize':
                await self.vectorize(job, cfg)
            elif job['kind'] == 'analyze':
                await self.analyze(job, cfg)
            elif job['kind']=='review_history':
                from analyzer.sales_history_review import scan
                if not await asyncio.to_thread(scan,self,job,cfg):
                    return True
            elif job['kind']=='review_existing':
                from analyzer.sales_history_review import review_project
                await review_project(self,job,cfg)
            elif job['kind'] == 'refresh_view':
                await asyncio.to_thread(self.refresh_view)
            elif job['kind'] == 'sync_source':
                if job['payload'].get('signature') != sync_signature(cfg):
                    raise ValueError('数据源或同步范围已变化，请重新发起同步')
                def progress(value):
                    if not self.update_job(job, progress=value):
                        raise ValueError('同步任务执行权已失效，旧任务停止刷新')
                snapshot = await asyncio.to_thread(SourceSync(self.store, cfg).refresh, progress, job['id'])
                self.set_state('source_schedule:'+sync_signature(cfg), {'next_sync_at':snapshot.get('next_sync_at',time.time()+cfg['source_sync_interval_seconds'])})
                if cfg['source_download_url'] and not snapshot.get('idle'):
                    self.enqueue_download(cfg, snapshot, snapshot.get('batch_id'))
            elif job['kind'] == 'download_source':
                from analyzer.material_download import download_batch
                if not await download_batch(self, job, cfg):
                    return True
            else:
                raise ValueError('未知销售项任务类型')
            self.update_job(job,progress={'phase':'completed'})
            self.finish(job)
        except asyncio.CancelledError:
            self.update_job(job,progress={'phase':'interrupted','message':'服务关闭，任务保留待重试'})
            self.update_job(job, status='retry', owner=None, lease_until=None, available_at=time.time(),
                attempts=max(0, job['attempts']-1), error='服务关闭，任务保留')
            raise
        except Exception as error:
            if job['kind']=='sync_source':
                self.set_state('source_schedule:'+sync_signature(cfg), {'next_sync_at':time.time()+cfg['source_sync_interval_seconds']})
            retry = bool(getattr(error, 'retryable', False)) and job['attempts'] < 3
            message = str(error) if isinstance(error, ValueError) or hasattr(error, 'retryable') else '销售项任务失败，请查看服务日志'
            location=' > '.join(f'{Path(f.filename).name}:{f.lineno} {f.name}' for f in traceback.extract_tb(error.__traceback__)[-5:])
            self.update_job(job,progress={'phase':'retry' if retry else 'failed',
                'error_type':type(error).__name__,'error_location':location,'message':message[:500]})
            self.finish(job, 'failed', message[:500], retry)
            if not retry and job['kind'] == 'vectorize':
                with self.store.session.begin() as s:
                    stmt = update(SalesItem)
                    ids = job['payload'].get('ids') or []
                    if ids:
                        stmt = stmt.where(SalesItem.id.in_(ids))
                    s.execute(stmt.values(vector_error=message[:500]))
            logger.warning('sales job %s failed: %s', job['id'], type(error).__name__)
        finally:
            trace_callback.reset(token)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def run(self, index):
        while True:
            try:
                cfg = self.settings()
                if index < cfg['analysis_concurrency'] and await self.run_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('sales worker iteration failed')
            await asyncio.sleep(1)

    async def scan_once(self):
        cfg = self.settings()
        if not cfg['engine_enabled'] or cfg['paused'] or not (cfg['history_enabled'] or cfg['incremental_enabled']):
            return
        if isinstance(self.source_factory(cfg), SnapshotSource):
            if cfg['incremental_enabled'] and cfg['source_dsn']:
                schedule = self.state('source_schedule:'+sync_signature(cfg), {})
                if schedule.get('next_sync_at',0)<=time.time():
                    self.enqueue('sync_source', {'signature':sync_signature(cfg)},digest(['sync_source',sync_signature(cfg)]))
            if not SourceSync(self.store,cfg).status()['ready']:
                return
            with self.store.session() as session:
                queued = session.scalar(select(func.count()).select_from(SalesJob).where(
                    SalesJob.status.in_(['queued','retry','running'])))
                if queued>=200:
                    return
                candidates = session.scalars(select(SourceProject).where(
                    SourceProject.source_scope==digest(cfg['source_dsn']), SourceProject.pending_analysis.is_(True),
                    SourceProject.in_scope.is_(True), SourceProject.channel.in_(
                        ['采购意向','采购公告','更正公告'] if not cfg['allow_later_stages'] else
                        ['采购意向','采购公告','更正公告','结果公告','合同公告','验收公告','废标终止','其他'])
                ).order_by(SourceProject.project_key).limit(min(cfg['project_batch_size'],200-queued))).all()
                values = [(r.project_key,r.batch_id) for r in candidates]
            for key,batch_id in values:
                with self.store.session() as session:
                    exists = session.get(SalesProject,key) is not None
                if not exists:
                    self.enqueue('analyze',{'project_key':key,'force':False,'preserve_existing':True,'batch_id':batch_id})
                with self.store.session.begin() as session:
                    row = session.get(SourceProject,(digest(cfg['source_dsn']),key),with_for_update=True)
                    if row and row.batch_id==batch_id:
                        row.pending_analysis=False
            if not values and cfg['history_enabled']:
                self.save_settings({'history_enabled':False})
            return
        with self.store.session() as s:
            syncing = s.scalar(select(func.count()).select_from(SalesJob).where(
                SalesJob.kind == 'sync_source', SalesJob.status.in_(['queued', 'retry', 'running'])))
            if syncing:
                return
            queued = s.scalar(select(func.count()).select_from(SalesJob).where(SalesJob.status.in_(['queued', 'retry', 'running'])))
        if queued >= 200:
            return
        history = self.state('history', {'cursor': '', 'processed': 0})
        if not cfg['history_enabled'] and history.get('next_scan', 0) > time.time():
            return
        history.update(phase='reading_projects', updated_at=time.time())
        rows = await asyncio.to_thread(self.source_factory(cfg).list_projects,
            history.get('cursor', ''), cfg['project_batch_size'], True)
        for row in rows:
            if not cfg['allow_later_stages'] and row.get('channel') not in ('采购意向', '采购公告', '更正公告'):
                with self.store.session.begin() as session:
                    session.execute(update(SalesProject).where(SalesProject.project_key == row['project_key']).values(
                        channel=row.get('channel') or '未知', status='stage_filtered'))
                continue
            self.enqueue('analyze', {'project_key': row['project_key'], 'force': False}, digest(['analyze', row['project_key']]))
        if rows:
            history.update(cursor=rows[-1]['project_key'], processed=history.get('processed', 0)+len(rows), phase='batch_queued')
        else:
            history.update(cursor='', phase='completed', next_scan=time.time()+cfg['poll_seconds'])
        history.update(error=None, updated_at=time.time())
        if self.update_history(history) and not rows and cfg['history_enabled']:
            self.save_settings({'history_enabled': False})

    def update_history(self, history):
        with self.store.session.begin() as session:
            current = session.get(SalesData, 'history', with_for_update=True)
            if current and current.value.get('snapshot') != history.get('snapshot'):
                return False
            if current:
                current.value = history
            else:
                session.add(SalesData(key='history', value=history))
        return True

    async def scan_loop(self):
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                state = dict(self.state('history', {}))
                state.update(phase='retry', error=str(error) if hasattr(error, 'retryable') or isinstance(error, ValueError) else '来源扫描失败，请测试数据库连接', updated_at=time.time())
                self.update_history(state)
                await asyncio.sleep(10)
            await asyncio.sleep(0.5)

    def overview(self):
        cfg = self.settings()
        with self.store.session() as s:
            total = s.scalar(select(func.count()).select_from(SalesItem))
            enabled = s.scalar(select(func.count()).select_from(SalesItem).where(SalesItem.enabled.is_(True)))
            ready = s.scalar(select(func.count()).select_from(SalesItem).where(
                SalesItem.vector.is_not(None),SalesItem.vector_version==SalesItem.version,
                SalesItem.vector_profile==vector_profile(cfg)))
            projects = s.scalar(select(func.count()).select_from(SalesProject))
        return {'projects': projects, 'total_sales_items': total, 'enabled_sales_items': enabled,
            'vectorized_sales_items': ready,
            'jobs': self.jobs(20), 'settings': {k: cfg[k] for k in ('paused', 'history_enabled', 'incremental_enabled', 'engine_enabled')},
            'history': self.state('history', {}), 'source_sync':SourceSync(self.store,cfg).status()}
