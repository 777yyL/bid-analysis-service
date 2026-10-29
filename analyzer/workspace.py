"""Independent analysis records, indexed labels and revision-bound durable queues."""

import json
import time
import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    Index,
    Integer,
    String,
    Text,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Mapped, defer, mapped_column

from analyzer.schemas import NoticeInput
from analyzer.store import Base, StateRow, Store, digest


class NoticeRow(Base):
    __tablename__ = "analysis_notices_v2"
    notice_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    buyer: Mapped[str] = mapped_column(Text)
    notice_type: Mapped[str] = mapped_column(String(100))
    project_key: Mapped[str | None] = mapped_column(Text, index=True)
    publish_at: Mapped[str | None] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[str] = mapped_column(String(64))
    knowledge_id: Mapped[str] = mapped_column(String(64))
    analysis_status: Mapped[str] = mapped_column(String(32), index=True)
    material_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    analysis_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float, index=True)


class TagRow(Base):
    __tablename__ = "analysis_tags_v2"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    dimension: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON)
    effective: Mapped[bool] = mapped_column(Boolean)
    __table_args__ = (Index("ix_analysis_tags_filter", "effective", "dimension", "category", "status"),)


class ReviewRow(Base):
    __tablename__ = "analysis_reviews_v2"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class TaskRow(Base):
    __tablename__ = "analysis_tasks_v2"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    notice_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    knowledge_id: Mapped[str] = mapped_column(String(64))
    revision: Mapped[str] = mapped_column(String(64))
    active_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    priority: Mapped[int] = mapped_column(Integer)
    use_model: Mapped[bool] = mapped_column(Boolean)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[float] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_analysis_tasks_claim", "kind", "status", "available_at"),)


def _tag(row, include_evidence=True):
    value = {key: getattr(row, key) for key in (
        "dimension", "category", "label", "status", "source", "reason")}
    value["evidence"] = row.evidence if include_evidence else []
    return value


def _task(row):
    return {c.name: getattr(row, c.name) for c in TaskRow.__table__.columns}


class Workspace:
    def __init__(self, store: Store):
        self.store = store

    def state(self, key, default=None):
        with self.store.session() as s:
            row = s.get(StateRow, "v2:" + key)
            return json.loads(row.value) if row else default

    def set_state(self, key, value):
        with self.store.session.begin() as s:
            row = s.get(StateRow, "v2:" + key)
            if row:
                row.value = json.dumps(value, ensure_ascii=False)
            else:
                s.add(StateRow(key="v2:" + key, value=json.dumps(value, ensure_ascii=False)))

    def ingest(self, notice: NoticeInput, knowledge_id: str, use_model=True, priority=70):
        payload = notice.model_dump()
        revision = digest(payload)
        now = time.time()
        with self.store.session.begin() as s:
            row = s.get(NoticeRow, notice.notice_id)
            changed = row is None or row.revision != revision or row.knowledge_id != knowledge_id
            if row is None:
                row = NoticeRow(notice_id=notice.notice_id, created_at=now)
                s.add(row)
            if changed:
                for name in ("title", "buyer", "notice_type", "project_key", "publish_at"):
                    setattr(row, name, getattr(notice, name))
                row.payload, row.revision, row.knowledge_id = payload, revision, knowledge_id
                row.analysis_status, row.updated_at = "queued", now
                row.material_complete, row.warnings = False, notice.warnings
                row.analysis_meta = {}
                # Invalidate machine conclusions immediately; manual decisions are durable.
                s.execute(delete(TagRow).where(TagRow.notice_id == notice.notice_id, TagRow.source != "manual"))
                self._enqueue(s, row, knowledge_id, "rule", priority, use_model)
            elif use_model:
                # An explicit model request remains meaningful after a prepare-only run.
                existing = s.scalar(select(TaskRow.id).where(
                    TaskRow.notice_id == row.notice_id, TaskRow.revision == row.revision,
                    TaskRow.knowledge_id == knowledge_id, TaskRow.kind == "model",
                    TaskRow.status.in_(["queued", "running", "retry", "completed"])))
                if not existing:
                    self._enqueue(s, row, knowledge_id, "model", 50, True)
        return {"notice_id": notice.notice_id, "changed": changed, "revision": revision}

    def _enqueue(self, s, row, knowledge_id, kind, priority, use_model):
        key = digest([row.notice_id, row.revision, knowledge_id, kind])
        existing = s.scalar(select(TaskRow).where(TaskRow.active_key == key))
        if existing:
            existing.use_model = existing.use_model or use_model
            return _task(existing)
        now = time.time()
        task = TaskRow(id=str(uuid.uuid4()), notice_id=row.notice_id, revision=row.revision,
                       knowledge_id=knowledge_id, kind=kind, active_key=key, status="queued",
                       priority=priority, use_model=use_model, attempts=0, available_at=now,
                       created_at=now, updated_at=now, metrics={})
        s.add(task)
        return {"id": task.id}

    def enqueue(self, notice_id, knowledge_id, kind, priority=50, use_model=True):
        with self.store.session.begin() as s:
            row = s.get(NoticeRow, notice_id)
            if row is None:
                raise KeyError(notice_id)
            return self._enqueue(s, row, knowledge_id, kind, priority, use_model)

    def claim(self, kind):
        now = time.time()
        eligible = or_((TaskRow.status.in_(["queued", "retry"])) & (TaskRow.available_at <= now),
                       (TaskRow.status == "running") & (TaskRow.lease_until < now))
        with self.store.session.begin() as s:
            counter_key = "v2:claim_counter:" + kind
            counter = s.get(StateRow, counter_key, with_for_update=True)
            count = int(counter.value) if counter else 0
            # Four priority picks, then one oldest pick: new work retains capacity
            # while history cannot starve, even after days of accumulated backlog.
            order = [TaskRow.created_at, TaskRow.id] if count % 5 == 4 else [
                TaskRow.priority.desc(), TaskRow.created_at, TaskRow.id]
            row = s.scalar(select(TaskRow).where(eligible, TaskRow.kind == kind)
                           .order_by(*order)
                           .with_for_update(skip_locked=True).limit(1))
            if not row:
                return None
            owner = str(uuid.uuid4())
            result = s.execute(update(TaskRow).where(TaskRow.id == row.id, eligible).values(
                status="running", owner=owner, lease_until=now + 90,
                attempts=TaskRow.attempts + 1, updated_at=now))
            if result.rowcount != 1:
                return None
            if counter:
                counter.value = str(count + 1)
            else:
                s.add(StateRow(key=counter_key, value=str(count + 1)))
            s.refresh(row)
            return _task(row)

    def heartbeat(self, task):
        with self.store.session.begin() as s:
            return s.execute(update(TaskRow).where(
                TaskRow.id == task["id"], TaskRow.owner == task["owner"], TaskRow.status == "running"
            ).values(lease_until=time.time() + 90)).rowcount == 1

    def task_is_current(self, task):
        with self.store.session() as s:
            row = s.get(NoticeRow, task["notice_id"])
            return bool(row and row.revision == task["revision"]
                        and row.knowledge_id == task["knowledge_id"])

    def finish_task(self, task, status, error=None, retry_delay=None, metrics=None, consume_attempt=True):
        now = time.time()
        with self.store.session.begin() as s:
            row = s.scalar(select(TaskRow).where(TaskRow.id == task["id"], TaskRow.owner == task["owner"],
                                                  TaskRow.status == "running").with_for_update())
            if not row:
                return False
            totals = dict(row.metrics or {})
            for name, value in (metrics or {}).items():
                totals[name] = totals.get(name, 0) + value if name in (
                    "llm_calls", "input_tokens", "rate_limits", "processing_seconds") else value
            if status == "failed" and retry_delay is None:
                notice = s.get(NoticeRow, task["notice_id"])
                if notice and notice.revision == task["revision"] and notice.knowledge_id == task["knowledge_id"]:
                    notice.analysis_status = "failed"
            return s.execute(update(TaskRow).where(
                TaskRow.id == task["id"], TaskRow.owner == task["owner"], TaskRow.status == "running"
            ).values(status="retry" if retry_delay is not None else status,
                     attempts=TaskRow.attempts if consume_attempt else TaskRow.attempts - 1,
                     active_key=task["active_key"] if retry_delay is not None else None,
                     owner=None, lease_until=None, updated_at=now,
                     available_at=now + (retry_delay or 0), error=error,
                     metrics=totals)).rowcount == 1

    def retry(self, task_id):
        with self.store.session.begin() as s:
            row = s.get(TaskRow, task_id)
            if row is None:
                raise KeyError(task_id)
            if row.status not in ("failed", "stale"):
                raise ValueError("只能重试失败或过期任务")
            notice = s.get(NoticeRow, row.notice_id)
            return self._enqueue(s, notice, notice.knowledge_id, row.kind, row.priority, row.use_model)

    def _replace_tags(self, s, notice_id, source, tags):
        s.execute(delete(TagRow).where(TagRow.notice_id == notice_id, TagRow.source == source))
        for tag in tags:
            s.add(TagRow(id=str(uuid.uuid4()), notice_id=notice_id, source=source, effective=False,
                         **{k: tag.get(k, [] if k == "evidence" else "") for k in (
                             "dimension", "category", "label", "status", "reason", "evidence")}))
        s.flush()
        rows = list(s.scalars(select(TagRow).where(TagRow.notice_id == notice_id)))
        rank = {"rule": 1, "model": 2, "manual": 3}
        winners = {}
        for row in rows:
            key = (row.dimension, row.category, row.label)
            if key not in winners or rank[row.source] > rank[winners[key].source]:
                winners[key] = row
        for row in rows:
            row.effective = winners[(row.dimension, row.category, row.label)].id == row.id

    def save_rules(self, notice_id, knowledge_id, tags, material_complete, warnings, task=None, metadata=None):
        with self.store.session.begin() as s:
            row = s.get(NoticeRow, notice_id)
            if task and (row.revision != task["revision"] or row.knowledge_id != knowledge_id):
                return False
            self._replace_tags(s, notice_id, "rule", tags)
            row.material_complete, row.warnings = material_complete, warnings
            row.analysis_meta = metadata or {}
            row.analysis_status = "needs_review" if not material_complete or not tags or any(
                t["status"] == "candidate" for t in tags) else "ready"
            row.updated_at = time.time()
            return True

    def save_model(self, task, tags, needs_more, metrics):
        with self.store.session.begin() as s:
            row = s.get(NoticeRow, task["notice_id"])
            if row.revision != task["revision"] or row.knowledge_id != task["knowledge_id"]:
                return False
            self._replace_tags(s, row.notice_id, "model", tags)
            row.analysis_status = "needs_review" if needs_more or not row.material_complete or any(
                tag["status"] == "candidate" for tag in tags) else "completed"
            row.updated_at = time.time()
            s.add(ReviewRow(id=str(uuid.uuid4()), notice_id=row.notice_id,
                            payload={"source": "model", "tags": tags, "metrics": metrics,
                                     "knowledge_id": task["knowledge_id"], "revision": task["revision"],
                                     "needs_more": needs_more}, created_at=time.time()))
            return True

    def review(self, notice_id, tag):
        with self.store.session.begin() as s:
            row = s.get(NoticeRow, notice_id)
            if row is None:
                raise KeyError(notice_id)
            prior = [_tag(t) for t in s.scalars(select(TagRow).where(
                TagRow.notice_id == notice_id, TagRow.source == "manual"))]
            def key(t):
                return (t["dimension"], t["category"], t["label"])
            current = next((t for t in s.scalars(select(TagRow).where(
                TagRow.notice_id == notice_id, TagRow.effective.is_(True))) if key(_tag(t)) == key(tag)), None)
            value = {**tag, "source": "manual", "evidence": _tag(current)["evidence"] if current else []}
            self._replace_tags(s, notice_id, "manual", [t for t in prior if key(t) != key(tag)] + [value])
            s.add(ReviewRow(id=str(uuid.uuid4()), notice_id=notice_id,
                            payload={**value, "knowledge_id": row.knowledge_id, "revision": row.revision},
                            created_at=time.time()))
            row.updated_at = time.time()

    def _summaries(self, s, rows, include_evidence=False):
        rows = list(rows)
        ids = [n.notice_id for n in rows]
        tags = {}
        if ids:
            query = select(TagRow).where(TagRow.notice_id.in_(ids), TagRow.effective.is_(True))
            if not include_evidence:
                query = query.options(defer(TagRow.evidence))
            for t in s.scalars(query):
                tags.setdefault(t.notice_id, []).append(_tag(t, include_evidence))
        return [{**{k: getattr(n, k) for k in ("notice_id", "title", "buyer", "notice_type",
                   "project_key", "publish_at", "updated_at", "analysis_status", "material_complete")},
                 "tags": tags.get(n.notice_id, [])} for n in rows]

    def notices(self, q="", dimension=None, category=None, tag_status=None, analysis_status=None,
                page=1, page_size=20, business_category=None, product_category=None):
        with self.store.session() as s:
            stmt = select(NoticeRow)
            if q:
                searches = [NoticeRow.title.icontains(q, autoescape=True), NoticeRow.buyer.icontains(q, autoescape=True)]
                if q.isdecimal():
                    searches.append(NoticeRow.notice_id == int(q))
                stmt = stmt.where(or_(*searches))
            if analysis_status:
                stmt = stmt.where(NoticeRow.analysis_status == analysis_status)
            if dimension or category or tag_status:
                tag_query = select(TagRow.notice_id).where(TagRow.effective.is_(True))
                for name, value in (("dimension", dimension), ("category", category), ("status", tag_status)):
                    if value:
                        tag_query = tag_query.where(getattr(TagRow, name) == value)
                stmt = stmt.where(NoticeRow.notice_id.in_(tag_query))
            for dim, value in (("business", business_category), ("product", product_category)):
                if value:
                    stmt = stmt.where(NoticeRow.notice_id.in_(select(TagRow.notice_id).where(
                        TagRow.effective.is_(True), TagRow.dimension == dim, TagRow.category == value,
                        TagRow.status != "excluded")))
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = s.scalars(stmt.options(defer(NoticeRow.payload)).order_by(NoticeRow.updated_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))
            return {"items": self._summaries(s, rows), "total": total, "page": page, "page_size": page_size}

    def detail(self, notice_id):
        with self.store.session() as s:
            row = s.get(NoticeRow, notice_id)
            if row is None:
                raise KeyError(notice_id)
            result = self._summaries(s, [row], include_evidence=True)[0]
            result.update(notice=row.payload, warnings=row.warnings, revision=row.revision,
                          knowledge_id=row.knowledge_id, analysis_meta=row.analysis_meta)
            tags = list(s.scalars(select(TagRow).where(TagRow.notice_id == notice_id)))
            for source in ("rule", "model", "manual"):
                result[source + "_tags"] = [_tag(t) for t in tags if t.source == source]
            result["history"] = [{"id": h.id, "created_at": h.created_at, **h.payload}
                                 for h in s.scalars(select(ReviewRow).where(ReviewRow.notice_id == notice_id)
                                                   .order_by(ReviewRow.created_at.desc()).limit(100))]
            return result

    def projects(self, q="", page=1, page_size=20):
        with self.store.session() as s:
            stmt = select(NoticeRow.project_key, func.count().label("notice_count"),
                          func.max(NoticeRow.updated_at).label("updated_at"), func.min(NoticeRow.title).label("title"))
            stmt = stmt.where(NoticeRow.project_key.is_not(None), NoticeRow.project_key != "")
            if q:
                stmt = stmt.where(NoticeRow.project_key.icontains(q, autoescape=True))
            stmt = stmt.group_by(NoticeRow.project_key)
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = s.execute(stmt.order_by(func.max(NoticeRow.updated_at).desc())
                             .offset((page - 1) * page_size).limit(page_size)).mappings().all()
            items = []
            project_tags = {}
            keys = [r["project_key"] for r in rows]
            if keys:
                tag_rows = s.execute(select(NoticeRow.project_key, TagRow)
                                     .join(TagRow, TagRow.notice_id == NoticeRow.notice_id)
                                     .where(NoticeRow.project_key.in_(keys), TagRow.effective.is_(True))
                                     .options(defer(TagRow.evidence)))
                for project_key, tag in tag_rows:
                    project_tags.setdefault(project_key, []).append(tag)
            for row in rows:
                item = dict(row)
                item["tags"] = self._aggregate_tags(project_tags.get(row["project_key"], []))
                items.append(item)
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    @staticmethod
    def _aggregate_tags(rows):
        result = {}
        for row in rows:
            key = (row.dimension, row.category, row.label, row.status)
            if key not in result:
                result[key] = {**_tag(row, False), "notice_ids": []}
            result[key]["notice_ids"].append(row.notice_id)
        return list(result.values())

    def project(self, project_key):
        with self.store.session() as s:
            rows = list(s.scalars(select(NoticeRow).where(NoticeRow.project_key == project_key)
                                  .options(defer(NoticeRow.payload))
                                  .order_by(NoticeRow.publish_at.asc().nulls_last(), NoticeRow.notice_id)))
            if not rows:
                raise KeyError(project_key)
            tags = s.scalars(select(TagRow).where(TagRow.notice_id.in_([r.notice_id for r in rows]),
                                                  TagRow.effective.is_(True)).options(defer(TagRow.evidence)))
            return {"project_key": project_key, "notices": self._summaries(s, rows),
                    "tags": self._aggregate_tags(tags)}

    def tasks(self, page=1, page_size=50, status=None, kind=None):
        with self.store.session() as s:
            stmt = select(TaskRow)
            if status:
                stmt = stmt.where(TaskRow.status == status)
            if kind:
                stmt = stmt.where(TaskRow.kind == kind)
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            items = [_task(row) for row in s.scalars(stmt.order_by(TaskRow.created_at.desc())
                                                    .offset((page - 1) * page_size).limit(page_size))]
            for item in items:
                item.pop("owner", None)
                item.pop("active_key", None)
            return {"items": items, "total": total}

    def overview(self):
        with self.store.session() as s:
            total = s.scalar(select(func.count()).select_from(NoticeRow))
            tagged = s.scalar(select(func.count(func.distinct(TagRow.notice_id))).where(
                TagRow.effective.is_(True), TagRow.status == "confirmed"))
            counts = dict(s.execute(select(NoticeRow.analysis_status, func.count())
                                    .group_by(NoticeRow.analysis_status)).all())
            distributions = {}
            for dimension in ("business", "product"):
                rows = s.execute(select(TagRow.category, func.count(func.distinct(TagRow.notice_id)))
                                 .where(TagRow.dimension == dimension, TagRow.effective.is_(True),
                                        TagRow.status == "confirmed").group_by(TagRow.category)
                                 .order_by(func.count(func.distinct(TagRow.notice_id)).desc()).limit(30))
                distributions[dimension + "_distribution"] = [{"name": name, "count": count}
                                                                for name, count in rows]
            day = func.floor(NoticeRow.created_at / 86400) * 86400
            daily = [{"date": float(date), "count": count} for date, count in s.execute(
                select(day, func.count()).group_by(day).order_by(day.desc()).limit(30))]
            # Bounded recent task metrics, including retry/running failures.
            task_samples = list(s.execute(select(TaskRow.kind, TaskRow.metrics)
                                           .order_by(TaskRow.updated_at.desc()).limit(1000)))
            task_metrics = [row.metrics for row in task_samples]
            def p95(kind):
                values = sorted(row.metrics["processing_seconds"] for row in task_samples
                                if row.kind == kind and type(row.metrics.get("processing_seconds")) in (int, float))
                return values[max(0, (95 * len(values) + 99) // 100 - 1)] if values else None
            throughput = s.scalar(select(func.count()).select_from(TaskRow).where(
                TaskRow.status == "completed", TaskRow.updated_at >= time.time() - 60))
            return {"total_notices": total, "tagged_notices": tagged,
                    "model_reviewed": s.scalar(select(func.count(func.distinct(TaskRow.notice_id)))
                        .join(NoticeRow, NoticeRow.notice_id == TaskRow.notice_id)
                        .where(TaskRow.kind == "model", TaskRow.status == "completed",
                               TaskRow.revision == NoticeRow.revision,
                               TaskRow.knowledge_id == NoticeRow.knowledge_id)),
                    "model_confirmed": s.scalar(select(func.count(func.distinct(TagRow.notice_id))).where(
                        TagRow.effective.is_(True), TagRow.source == "model", TagRow.status == "confirmed")),
                    "needs_review": counts.get("needs_review", 0),
                    "material_incomplete": s.scalar(select(func.count()).select_from(NoticeRow).where(
                        NoticeRow.material_complete.is_(False))), "counts_by_status": counts,
                    **distributions, "daily": list(reversed(daily)),
                    "metrics": {"sample_size": len(task_metrics),
                                "rule_p95_seconds": p95("rule"), "model_p95_seconds": p95("model"),
                                "recent_completed_tasks": throughput,
                                "llm_calls": sum(m.get("llm_calls", 0) for m in task_metrics),
                                "input_tokens": sum(m.get("input_tokens", 0) for m in task_metrics),
                                "rate_limits": sum(m.get("rate_limits", 0) for m in task_metrics)}}

    def reindex_batch(self, knowledge_id, cursor=0, limit=100, use_model=None):
        with self.store.session() as s:
            values = [n.payload for n in s.scalars(select(NoticeRow).where(NoticeRow.notice_id > cursor)
                                                  .order_by(NoticeRow.notice_id).limit(limit))]
            ids = [v["notice_id"] for v in values]
            model_ids = set(s.scalars(select(TaskRow.notice_id).where(TaskRow.notice_id.in_(ids),
                TaskRow.use_model.is_(True)).distinct())) if use_model is None and ids else set()
        changed = 0
        for value in values:
            enabled = value["notice_id"] in model_ids if use_model is None else use_model
            changed += int(self.ingest(NoticeInput.model_validate(value), knowledge_id, enabled)["changed"])
        return {"cursor": max([cursor, *[v["notice_id"] for v in values]]),
                "received": len(values), "changed": changed}
