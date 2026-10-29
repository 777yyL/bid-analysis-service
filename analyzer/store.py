import hashlib
import json
import time
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, defer, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from analyzer.schemas import KnowledgeInput


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


class Base(DeclarativeBase):
    pass


class KnowledgeRow(Base):
    __tablename__ = "analysis_knowledge"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class StateRow(Base):
    __tablename__ = "analysis_state"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class JobRow(Base):
    __tablename__ = "analysis_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    knowledge_id: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    active_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt_history: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (Index("ix_analysis_jobs_queue", "status", "available_at"),)


class ResultCache(Base):
    __tablename__ = "analysis_result_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class TextCacheRow(Base):
    __tablename__ = "analysis_text_cache"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


def job_dict(row: JobRow, include_payload: bool = False, include_details: bool = True) -> dict:
    fields = [
        "id", "notice_id", "knowledge_id", "mode", "status", "attempts", "owner",
        "created_at", "available_at", "started_at", "finished_at", "lease_until",
        "error",
    ]
    if include_details:
        fields.extend(["result", "attempt_history"])
    result = {key: getattr(row, key) for key in fields}
    if include_payload:
        result["payload"] = row.payload
    return result


class Store:
    def __init__(self, url: str):
        options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        # These tables belong exclusively to DATABASE_URL, never SOURCE_DB_DSN.
        Base.metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    def save_knowledge(self, knowledge: KnowledgeInput) -> str:
        payload = knowledge.model_dump()
        key = digest(payload)
        for attempt in range(2):
            try:
                with self.session.begin() as session:
                    if session.get(KnowledgeRow, key) is None:
                        session.add(KnowledgeRow(id=key, payload=payload, created_at=time.time()))
                    state = session.get(StateRow, "active_knowledge")
                    if state is None:
                        session.add(StateRow(key="active_knowledge", value=key))
                    else:
                        state.value = key
                return key
            except IntegrityError:
                if attempt:
                    raise
        raise RuntimeError("knowledge transaction failed")

    def active_knowledge_id(self) -> str | None:
        with self.session() as session:
            row = session.get(StateRow, "active_knowledge")
            return row.value if row else None

    def get_knowledge(self, key: str) -> KnowledgeInput:
        with self.session() as session:
            row = session.get(KnowledgeRow, key)
            if row is None:
                raise ValueError("知识快照不存在")
            return KnowledgeInput.model_validate(row.payload)

    def enqueue(self, notice_id: int, knowledge_id: str, mode: str, payload: dict | None = None):
        key = digest([notice_id, knowledge_id, mode, payload])
        with self.session() as session:
            existing = session.scalar(select(JobRow).where(JobRow.active_key == key))
            if existing:
                return job_dict(existing)
            now = time.time()
            row = JobRow(id=str(uuid.uuid4()), notice_id=notice_id, knowledge_id=knowledge_id,
                         mode=mode, status="queued", active_key=key, payload=payload,
                         available_at=now, created_at=now, attempts=0, attempt_history=[])
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(select(JobRow).where(JobRow.active_key == key))
                if existing:
                    return job_dict(existing)
                raise
            return job_dict(row)

    def claim(self, now: float | None = None, lease_seconds: float = 90) -> dict | None:
        now = time.time() if now is None else now
        eligible = or_(
            (JobRow.status.in_(["queued", "retry"])) & (JobRow.available_at <= now),
            (JobRow.status == "running") & (JobRow.lease_until < now),
        )
        with self.session.begin() as session:
            row = session.scalar(select(JobRow).where(eligible)
                                 .order_by(JobRow.available_at, JobRow.created_at)
                                 .with_for_update(skip_locked=True).limit(1))
            if row is None:
                return None
            owner = str(uuid.uuid4())
            changed = session.execute(update(JobRow).where(JobRow.id == row.id, eligible).values(
                status="running", owner=owner, lease_until=now + lease_seconds,
                attempts=JobRow.attempts + 1, started_at=now, error=None,
            ))
            if changed.rowcount != 1:
                return None
            session.refresh(row)
            return job_dict(row, include_payload=True)

    def heartbeat(self, job: dict) -> bool:
        with self.session.begin() as session:
            result = session.execute(update(JobRow).where(
                JobRow.id == job["id"], JobRow.owner == job["owner"], JobRow.status == "running"
            ).values(lease_until=time.time() + 90))
            return result.rowcount == 1

    def complete(self, job: dict, status: str, result: dict, *, cache_key: str | None = None,
                 cache_value: dict | None = None, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.session.begin() as session:
            row = session.scalar(select(JobRow).where(
                JobRow.id == job["id"], JobRow.owner == job["owner"], JobRow.status == "running"
            ).with_for_update())
            if row is None:
                return False
            row.status, row.result, row.finished_at = status, result, now
            row.active_key = row.owner = row.lease_until = None
            row.error = None
            row.attempt_history = [*row.attempt_history, {
                "attempt": row.attempts, "finished_at": now,
                "status": status, "metrics": result.get("metrics", {}),
            }]
            if cache_key and cache_value is not None:
                cached = session.get(ResultCache, cache_key)
                if cached is None:
                    session.add(ResultCache(key=cache_key, payload=cache_value, created_at=now))
                else:
                    cached.payload, cached.created_at = cache_value, now
            return True

    def fail(self, job: dict, error: str, *, retry_delay: float | None, metrics: dict,
             now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.session.begin() as session:
            row = session.scalar(select(JobRow).where(
                JobRow.id == job["id"], JobRow.owner == job["owner"], JobRow.status == "running"
            ).with_for_update())
            if row is None:
                return False
            row.error = error[:1000]
            row.status = "retry" if retry_delay is not None else "failed"
            row.owner = row.lease_until = None
            if retry_delay is not None:
                row.available_at = now + max(0, retry_delay)
            else:
                row.finished_at, row.active_key = now, None
            row.attempt_history = [*row.attempt_history, {
                "attempt": row.attempts, "finished_at": now,
                "status": row.status, "error": row.error, "metrics": metrics,
            }]
            return True

    def get_job(self, key: str) -> dict | None:
        with self.session() as session:
            row = session.get(JobRow, key)
            return job_dict(row) if row else None

    def list_jobs(self, *, status: str | None = None, notice_id: int | None = None,
                  limit: int = 50, offset: int = 0) -> dict:
        with self.session() as session:
            query = select(JobRow)
            if status:
                query = query.where(JobRow.status == status)
            if notice_id:
                query = query.where(JobRow.notice_id == notice_id)
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            rows = session.scalars(query.options(
                defer(JobRow.payload), defer(JobRow.result), defer(JobRow.attempt_history)
            ).order_by(JobRow.created_at.desc()).limit(limit).offset(offset))
            items = []
            for row in rows:
                item = job_dict(row, include_details=False)
                item.pop("owner", None)
                items.append(item)
            return {"total": total, "items": items}

    def get_cached_result(self, key: str) -> dict | None:
        with self.session() as session:
            row = session.get(ResultCache, key)
            return row.payload if row else None

    def set_cached_result(self, key: str, value: dict):
        with self.session.begin() as session:
            row = session.get(ResultCache, key)
            if row:
                row.payload, row.created_at = value, time.time()
            else:
                session.add(ResultCache(key=key, payload=value, created_at=time.time()))

    def get_text(self, key: str) -> str | None:
        with self.session() as session:
            row = session.get(TextCacheRow, key)
            return row.value if row else None

    def set_text(self, key: str, value: str) -> None:
        with self.session.begin() as session:
            row = session.get(TextCacheRow, key)
            if row:
                row.value = value
            else:
                session.add(TextCacheRow(key=key, value=value))

    def metrics(self) -> dict:
        with self.session() as session:
            counts = dict(session.execute(select(JobRow.status, func.count()).group_by(JobRow.status)).all())
            rows = list(session.execute(select(
                JobRow.created_at, JobRow.finished_at, JobRow.attempt_history,
                JobRow.result["cache_hit"].as_boolean().label("cache_hit"),
            ).where(JobRow.finished_at.is_not(None))
                .order_by(JobRow.finished_at.desc()).limit(1000)))
            latencies = sorted(row.finished_at - row.created_at for row in rows)
            calls = sum(item.get("metrics", {}).get("llm_calls", 0)
                        for row in rows for item in row.attempt_history)
            tokens = sum(item.get("metrics", {}).get("input_tokens", 0)
                         for row in rows for item in row.attempt_history)
            hits = sum(bool(row.cache_hit) for row in rows)
            return {"status_counts": counts, "finished_sample_size": len(rows),
                    "sample_llm_calls": calls, "sample_reported_input_tokens": tokens,
                    "sample_cache_hits": hits,
                    "sample_end_to_end_p95_seconds": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)]
                    if latencies else None}
