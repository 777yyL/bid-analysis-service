"""Material embeddings owned by the analysis database, with content-addressed reuse."""
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select

from analyzer.procurement_extraction import (
    EXTRACTION_VERSION,
    extract_requirements,
    requirement_chunks,
)
from analyzer.sales_source import parse_vector
from analyzer.sales_store import SalesDocumentChunk, SalesProject, record
from analyzer.source import SourceError
from analyzer.store import digest


def document_profile(cfg):
    return digest(['local-demand-v1', EXTRACTION_VERSION, *[cfg[k] for k in ('embedding_base_url', 'embedding_model',
        'embedding_dimensions', 'embedding_document_prefix', 'source_chunk_chars')]])


class LocalDocuments:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.scope, self.profile = digest(cfg['source_dsn']), document_profile(cfg)

    async def prepare(self, material, client, progress, dimensions):
        documents = material.pop('documents', [])
        requested, roles = [], {}
        extraction = {'version': EXTRACTION_VERSION, 'input_documents':0, 'original_chars':0,
            'extracted_chars':0, 'removed_chars':0, 'demand_chars':0, 'uncertain_chars':0,
            'documents':[], 'omitted_documents':0}
        preview_budget = 24000
        warnings = list(material.get('warnings', []))
        size = self.cfg['source_chunk_chars']
        for document in documents:
            if document['source_type'] == 'attachment' and not self.cfg.get('collect_attachments', False):
                continue
            content = document['content']
            progress({'phase':'extracting_requirements', 'notice_id':document['notice_id'],
                      'attachment_id':document.get('attachment_id')})
            result = extract_requirements(content,
                correction=document['notice_id'] in material.get('correction_notice_ids', []))
            extraction['input_documents'] += 1
            for name in ('original_chars', 'extracted_chars', 'removed_chars', 'demand_chars', 'uncertain_chars'):
                extraction[name] += result[name]
            warnings.extend(result['warnings'])
            if len(extraction['documents']) < 50:
                limit = min(3000, preview_budget)
                full_preview = '\n'.join(segment['text'] for segment in result['segments'])
                preview = full_preview[:limit]
                preview_budget -= len(preview)
                extraction['documents'].append({'notice_id':document['notice_id'],
                    'attachment_id':document.get('attachment_id'), 'source_type':document['source_type'],
                    'title':document.get('chunk_title') or '', 'original_chars':result['original_chars'],
                    'extracted_chars':result['extracted_chars'], 'uncertain_chars':result['uncertain_chars'],
                    'preview':preview, 'preview_truncated':len(full_preview)>len(preview)})
            else:
                extraction['omitted_documents'] += 1
            document_key = digest([self.scope, material['project_key'], document['notice_id'],
                document.get('attachment_id'), document['source_type'], digest(content), self.profile])
            for index, chunk in enumerate(requirement_chunks(result, size)):
                chunk_id = digest([document_key, index])
                roles[chunk_id] = chunk['section_role']
                requested.append({'id': chunk_id, 'source_scope': self.scope,
                    'project_key': material['project_key'], 'notice_id': document['notice_id'],
                    'source_type': document['source_type'], 'attachment_id': document.get('attachment_id'),
                    'chunk_index': index, 'chunk_title': document.get('chunk_title') or '', 'content': chunk['content'],
                    'content_hash': digest(chunk['content']), 'char_start': chunk['char_start'], 'char_end': chunk['char_end'],
                    'embed_model': self.cfg['embedding_model'], 'profile': self.profile})
        if len(requested) > 80000:
            raise SourceError('单项目分片超过80000，未截断分析')
        chunks, reused = [], 0
        batch_size = self.cfg['embedding_batch_size']
        for start in range(0, len(requested), batch_size):
            batch = requested[start:start + batch_size]
            with self.store.session() as session:
                existing = {r.id: record(r) for r in session.scalars(select(SalesDocumentChunk).where(
                    SalesDocumentChunk.id.in_([r['id'] for r in batch])))}
            pending = [row for row in batch if row['id'] not in existing]
            reused += len(batch) - len(pending)
            progress({'phase': 'vectorizing_material', 'storage': 'local', 'processed': start,
                      'total': len(requested), 'reused': reused})
            if pending:
                vectors = await client.embed_documents([row['content'] for row in pending])
                if len(vectors) != len(pending):
                    raise SourceError('公告向量数量不一致，未写入本地库')
                vectors = [parse_vector(vector) for vector in vectors]
                if any(len(vector) != dimensions for vector in vectors):
                    raise SourceError('公告与销售项向量维度不一致，未写入本地库')
                progress({'phase': 'saving_local_vectors', 'processed': start, 'total': len(requested)})
                with self.store.session.begin() as session:
                    for row, vector in zip(pending, vectors, strict=True):
                        values = {**row, 'embedding': vector, 'created_at': datetime.now(timezone.utc).isoformat()}
                        stored = session.get(SalesDocumentChunk, row['id'])
                        if stored is None:
                            session.add(SalesDocumentChunk(**values))
                            existing[row['id']] = values
                        else:
                            existing[row['id']] = record(stored)
            for row in batch:
                values = existing[row['id']]
                if len(parse_vector(values['embedding'])) != dimensions:
                    raise SourceError('本地公告缓存与销售项向量维度不一致')
                chunks.append({**values, 'chunk_id': values['id'], 'dimensions': dimensions,
                               'procurement_extracted':True, 'section_role':roles[values['id']]})
        material.update(chunks=chunks, local_chunk_ids=[r['chunk_id'] for r in chunks],
            local_chunk_roles=roles, procurement_extraction=extraction,
            material_storage='local', collect_attachments=self.cfg.get('collect_attachments', False),
            vectorization={'chunks': len(chunks), 'reused': reused, 'generated': len(chunks)-reused,
                'body_documents': sum(d['source_type']=='notice' for d in documents),
                'attachment_documents': sum(d['source_type']=='attachment' for d in documents)
                    if self.cfg.get('collect_attachments', False) else 0})
        material['status'] = 'ready' if chunks else 'no_procurement_demand' if extraction['original_chars'] else 'missing_material'
        if material['status'] == 'no_procurement_demand':
            warnings.append('未提取到具体采购需求，不对全文进行向量化，请核对原文或附件')
        material['warnings'] = list(dict.fromkeys(warnings))
        return material

    def search(self, vector, limit=20):
        query = np.asarray(parse_vector(vector), dtype=np.float64)
        query /= np.linalg.norm(query)
        found = []
        with self.store.session() as session:
            projects = select(SalesProject).where(
                SalesProject.material['_source_revision'].as_string() == self.scope)
            if not self.cfg['allow_later_stages']:
                projects = projects.where(SalesProject.channel.in_(['采购意向', '采购公告', '更正公告']))
            projects = projects.where(SalesProject.status != 'stage_filtered')
            for project in session.scalars(projects.execution_options(yield_per=50)):
                ids = project.material.get('local_chunk_ids', [])
                for start in range(0, len(ids), 500):
                    rows = session.scalars(select(SalesDocumentChunk).where(
                        SalesDocumentChunk.id.in_(ids[start:start+500]), SalesDocumentChunk.profile == self.profile))
                    for row in rows:
                        if row.source_type == 'attachment' and not self.cfg.get('collect_attachments', False):
                            continue
                        value = np.asarray(row.embedding, dtype=np.float64)
                        if value.shape != query.shape:
                            continue
                        score = float(np.dot(value / np.linalg.norm(value), query))
                        found.append({**record(row), 'chunk_id': row.id, 'similarity': score,
                                      'notice_title': row.chunk_title})
                    found = sorted(found, key=lambda r: r['similarity'], reverse=True)[:limit]
        return found
