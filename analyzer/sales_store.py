"""Versioned sales matching records, isolated from legacy results."""
import logging
import os
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    Integer,
    String,
    Text,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column

from analyzer.sales_models import RuntimeSettings
from analyzer.source import _plain
from analyzer.store import Base, digest

SECRETS = ('source_dsn', 'embedding_api_key', 'review_api_key', 'knowledge_api_key', 'source_download_key')


class SalesData(Base):
    __tablename__ = 'sales_runtime_v1'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class SalesItem(Base):
    __tablename__ = 'sales_items_v1'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), index=True)
    description: Mapped[str] = mapped_column(Text)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    products: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[str] = mapped_column(String(64))
    vector: Mapped[list | None] = mapped_column(JSON, nullable=True)
    vector_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(Float)


class SalesJob(Base):
    __tablename__ = 'sales_jobs_v1'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    active_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    available_at: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class SalesDocumentChunk(Base):
    __tablename__ = 'sales_document_chunks_v1'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_scope: Mapped[str] = mapped_column(String(64), index=True)
    project_key: Mapped[str] = mapped_column(Text, index=True)
    notice_id: Mapped[int] = mapped_column(BigInteger)
    attachment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_type: Mapped[str] = mapped_column(String(20))
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list] = mapped_column(JSON)
    embed_model: Mapped[str] = mapped_column(Text)
    profile: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(40))


class SalesProject(Base):
    __tablename__ = 'sales_projects_v1'
    project_key: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40), index=True)
    revision: Mapped[str] = mapped_column(String(64))
    material: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float)


class SalesMatch(Base):
    __tablename__ = 'sales_matches_v1'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, index=True)
    sales_item_id: Mapped[str] = mapped_column(String(100), index=True)
    revision: Mapped[str] = mapped_column(String(64))
    item_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    method: Mapped[str] = mapped_column(String(20))
    score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON)


class SalesAudit(Base):
    __tablename__ = 'sales_audit_v1'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


def record(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def vector_profile(cfg):
    return digest([cfg[k] for k in ('embedding_base_url', 'embedding_model', 'embedding_dimensions',
                                   'embedding_query_prefix', 'embedding_document_prefix')])


class SalesStore:
    def __init__(self, store, bootstrap):
        self.store, self.bootstrap = store, bootstrap

    def state(self, key, default=None):
        with self.store.session() as s:
            row = s.get(SalesData, key)
            return row.value if row else default

    def set_state(self, key, value):
        with self.store.session.begin() as s:
            row = s.get(SalesData, key)
            if row:
                row.value = value
            else:
                s.add(SalesData(key=key, value=value))

    def cipher(self):
        path = Path(self.bootstrap.sales_secret_key_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Fernet.generate_key())
        return Fernet(path.read_bytes().strip())

    def settings(self, masked=False):
        cfg = RuntimeSettings().model_dump()
        cfg['engine_enabled'] = self.bootstrap.sales_engine_default
        saved = dict(self.state('settings', {}))
        for key in SECRETS:
            if saved.get(key):
                try:
                    saved[key] = self.cipher().decrypt(saved[key].encode()).decode()
                except (InvalidToken, ValueError, OSError):
                    raise ValueError('配置密钥不可用，请恢复持久化 runtime/settings.key') from None
        cfg.update(saved)
        if masked:
            for key in SECRETS:
                cfg[key + '_configured'] = bool(cfg[key])
                cfg[key] = ''
        return cfg

    def save_settings(self, incoming):
        cfg, values = self.settings(), dict(incoming)
        for key in SECRETS:
            if values.pop(key + '_clear', False):
                cfg[key] = ''
                values.pop(key, None)
            elif values.get(key) in ('', None):
                values.pop(key, None)
            values.pop(key + '_configured', None)
        cfg.update(values)
        cfg = RuntimeSettings.model_validate(cfg).model_dump()
        for key in SECRETS:
            if cfg[key]:
                cfg[key] = self.cipher().encrypt(cfg[key].encode()).decode()
        self.set_state('settings', cfg)
        return self.settings(masked=True)

    def enabled(self):
        return self.settings()['engine_enabled']

    def item_json(self, row, cfg=None):
        cfg = cfg or self.settings()
        value = record(row)
        value.pop('vector', None)
        ready = row.vector is not None and row.vector_version == row.version and row.vector_profile == vector_profile(cfg)
        value['vector_status'] = 'ready' if ready else 'failed' if row.vector_error else 'pending'
        return value

    def items(self, q='', enabled=None, page=1, page_size=50):
        cfg = self.settings()
        with self.store.session() as s:
            stmt = select(SalesItem)
            if q:
                stmt = stmt.where(SalesItem.name.icontains(q, autoescape=True))
            if enabled is not None:
                stmt = stmt.where(SalesItem.enabled == enabled)
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = s.scalars(stmt.order_by(SalesItem.name, SalesItem.id).offset((page-1)*page_size).limit(page_size))
            return {'items': [self.item_json(r, cfg) for r in rows], 'total': total}

    def import_items(self, values):
        if not values or len(values) > 10000:
            raise ValueError('每次导入 1～10000 个销售项')
        prepared, seen = [], set()
        for value in values:
            if not isinstance(value.get('name'), str) or not isinstance(value.get('description'), str):
                raise ValueError('销售项名称和能力描述必须是文本')
            name = str(value.get('name', '')).strip()
            description = _plain(str(value.get('description', '')))
            if not name or not description or '\x00' in name or '\x00' in description or len(name) > 300 or len(description) > 20000:
                raise ValueError('销售项名称和能力描述不能为空或超过上限')
            sid = str(value.get('id') or digest(['sales-item', name]))
            if len(sid) > 100 or sid in seen:
                raise ValueError('销售项 ID 重复或超过上限')
            seen.add(sid)
            relations = {}
            for field in ('categories', 'products'):
                raw = value.get(field, [])
                if not isinstance(raw, list) or not all(isinstance(v, str) and len(v) <= 500 for v in raw):
                    raise ValueError('分类及产品关联必须为字符串数组')
                relations[field] = list(dict.fromkeys(v.strip() for v in raw if v.strip()))
            prepared.append((sid, name, description, relations, value.get('enabled')))
        with self.store.session.begin() as s:
            for sid, name, description, relations, enabled in prepared:
                row = s.get(SalesItem, sid)
                if row is None:
                    row = SalesItem(id=sid, enabled=True)
                    s.add(row)
                version = digest([name, description])
                if row.version != version:
                    row.vector_error = None
                row.name, row.description, row.version = name, description, version
                row.categories, row.products = relations['categories'], relations['products']
                if enabled is not None:
                    if not isinstance(enabled, bool):
                        raise ValueError('启用状态必须为布尔值')
                    row.enabled = enabled
                row.updated_at = time.time()
        return {'received': len(prepared)}

    def set_enabled(self, sid, enabled):
        with self.store.session.begin() as s:
            row = s.get(SalesItem, sid)
            if row is None:
                raise KeyError(sid)
            row.enabled, row.updated_at = enabled, time.time()

    def enqueue(self, kind, payload, key=None):
        if kind == 'analyze':
            key = digest(['analyze', payload['project_key']])
        key = key or digest([kind, payload])
        with self.store.session.begin() as s:
            row = s.scalar(select(SalesJob).where(SalesJob.active_key == key))
            if row:
                return record(row)
            row = SalesJob(id=str(uuid.uuid4()), kind=kind, payload=payload, status='queued', active_key=key,
                progress={}, attempts=0, owner=None, lease_until=None, available_at=time.time(),
                error=None, created_at=time.time(), updated_at=time.time())
            s.add(row)
        return record(row)

    def jobs(self, limit=100):
        with self.store.session() as s:
            values=[]
            for row in s.scalars(select(SalesJob).order_by(SalesJob.created_at.desc()).limit(limit)):
                value=record(row)
                value['progress']={k:v for k,v in row.progress.items() if k!='events'}
                values.append(value)
            return {'items': values,
                    'total': s.scalar(select(func.count()).select_from(SalesJob))}

    def claim(self, kinds=None):
        now, owner = time.time(), str(uuid.uuid4())
        with self.store.session.begin() as s:
            statement = select(SalesJob).where(or_(
                SalesJob.status.in_(['queued', 'retry']) & (SalesJob.available_at <= now),
                (SalesJob.status == 'running') & (SalesJob.lease_until < now)))
            if kinds:
                statement = statement.where(SalesJob.kind.in_(kinds))
            row = s.scalar(statement
                .order_by((SalesJob.kind == 'sync_source').desc(),
                    SalesJob.payload['test'].as_boolean().desc().nullslast(), SalesJob.created_at)
                .with_for_update(skip_locked=True).limit(1))
            if row is None:
                return None
            changed = s.execute(update(SalesJob).where(SalesJob.id == row.id, SalesJob.status == row.status,
                SalesJob.owner == row.owner).values(status='running', owner=owner, lease_until=now+90,
                    attempts=SalesJob.attempts+1, updated_at=now))
            if changed.rowcount != 1:
                return None
            s.refresh(row)
            return record(row)

    def update_job(self, job, **values):
        now=time.time()
        values['updated_at'] = now
        with self.store.session.begin() as s:
            row=s.scalar(select(SalesJob).where(SalesJob.id==job['id'],SalesJob.owner==job['owner'],
                SalesJob.status=='running').with_for_update())
            if row is None:
                return False
            if 'lease_until' in values and 'progress' not in values:
                stage_age=round(now-(row.progress or {}).get('phase_started_at',now),1)
                log=logging.getLogger('analyzer.tasks')
                (log.warning if stage_age>=120 else log.info)(
                    'task=%s heartbeat phase=%s stage_seconds=%s lease_renewed=true',
                    job['id'],(row.progress or {}).get('phase','unknown'),stage_age)
            if 'progress' in values:
                incoming=dict(values['progress'])
                previous=dict(row.progress or {})
                phase=incoming.get('phase',previous.get('phase','unknown'))
                events=list(previous.get('events',[]))
                event={'at':now,'phase':phase,'attempt':row.attempts,
                       'elapsed_seconds':round(now-previous.get('phase_started_at',now),3),
                       **{k:v for k,v in incoming.items() if k not in ('phase','events')}}
                events.append(event)
                if phase!=previous.get('phase'):
                    previous['phase_started_at']=now
                values['progress']={**previous,**incoming,'events':events[-100:],'last_event_at':now}
                logging.getLogger('analyzer.tasks').info('task=%s project=%s event=%s',job['id'],
                    job['payload'].get('project_key',''),event)
            for key,value in values.items():
                setattr(row,key,value)
            return True

    def finish(self, job, status='completed', error=None, retry=False):
        self.update_job(job, status='retry' if retry else status, error=error,
            active_key=job['active_key'] if retry else None, owner=None, lease_until=None,
            available_at=time.time()+min(300, 10*2**min(job['attempts'], 4)))
