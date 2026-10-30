"""Project demand snapshots reuse the durable task queue without altering old tables."""

import time
import uuid
from collections import defaultdict

from sqlalchemy import JSON, BigInteger, Boolean, Float, String, Text, delete, func, select, update
from sqlalchemy.orm import Mapped, defer, mapped_column

from analyzer.procurement import procurement_sections
from analyzer.project_selection import select_project
from analyzer.schemas import AnalysisDocument, NoticeInput
from analyzer.store import Base, digest
from analyzer.workspace import NoticeRow, ReviewRow, TagRow, TaskRow, _tag, _task

POLICY_VERSION = "one-main-with-corrections-v1"
ELIGIBLE = {"采购公告", "采购意向", "更正公告"}


class ProjectPlanRow(Base):
    __tablename__ = "analysis_project_plans_v3"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_key: Mapped[str] = mapped_column(Text, index=True)
    unit_key: Mapped[str] = mapped_column(String(300))
    primary_notice_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    revision: Mapped[str] = mapped_column(String(64))
    knowledge_id: Mapped[str] = mapped_column(String(64))
    selection: Mapped[dict] = mapped_column(JSON)
    input_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    analysis_status: Mapped[str] = mapped_column(String(32), index=True)
    material_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    rule_revision: Mapped[str | None] = mapped_column(String(64))
    use_model: Mapped[bool] = mapped_column(Boolean)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


def project_key(notice):
    return notice.project_key or f"notice:{notice.notice_id}"


def _documents(notices):
    result = []
    for notice in notices:
        sources = [("body", notice.body)]
        sources.extend((f"lot:{i}", f"{lot.lot_name}\n{lot.description}") for i, lot in enumerate(notice.lots))
        sources.extend((f"attachment:{att.att_id}", att.text) for att in notice.attachments)
        for source, text in sources:
            parsed = procurement_sections(text)
            for span in parsed["spans"]:
                if span["role"] == "process" or not span["text"].strip():
                    continue
                result.append(AnalysisDocument(notice_id=notice.notice_id, notice_title=notice.title,
                    notice_type=notice.notice_type, source=source, text=span["text"], start=span["start"],
                    section_role=span["role"]))
    return result


def build_material(selection):
    main = selection["main"]
    if main is None:
        return None, digest([POLICY_VERSION, "missing", selection["unit_key"]])
    notices = [main, *selection["corrections"]]
    docs = _documents(notices)
    warnings = list(selection["warnings"])
    for notice in notices:
        warnings.extend(f"公告 {notice.notice_id}：{warning}" for warning in notice.warnings)
    effective = main.model_copy(update={"analysis_documents": docs, "analysis_scope": "project",
        "attachments": [a for n in notices for a in n.attachments],
        "warnings": list(dict.fromkeys(warnings))})
    revision = digest([POLICY_VERSION, main.notice_id, selection.get("round_key"), selection.get("lot_key"),
        [n.notice_id for n in selection["corrections"]], [d.model_dump() for d in docs],
        [[n.notice_id, n.revoke_status, n.material_loaded,
          [[a.att_id, a.extract_status, a.text_truncated, bool(a.text)] for a in n.attachments]] for n in notices],
        warnings])
    return effective, revision


class ProjectStore:
    def __init__(self, ws):
        self.ws, self.store = ws, ws.store

    def bootstrap(self):
        if self.ws.state("project_policy_version") == POLICY_VERSION:
            return
        # Already running old tasks fail their snapshot check after selection changes.
        with self.store.session.begin() as s:
            s.execute(update(TaskRow).where(TaskRow.kind.in_(["rule", "model"]),
                TaskRow.status.in_(["queued", "retry"])).values(status="stale", active_key=None,
                error="升级为项目分析，公告任务由项目需求任务替代", updated_at=time.time()))
        key = self.store.active_knowledge_id()
        if key:
            self.ws.set_state("reindex", {"cursor": 0, "knowledge_id": key, "enabled": True})
        self.ws.set_state("project_policy_version", POLICY_VERSION)

    def notices(self, key):
        with self.store.session() as s:
            return [NoticeInput.model_validate(n.payload) for n in s.scalars(
                select(NoticeRow).where(NoticeRow.project_key == key).order_by(NoticeRow.notice_id))]

    def _enqueue(self, s, plan, kind, priority=70):
        if plan.primary_notice_id is None:
            return None
        active_key = digest([plan.id, plan.revision, plan.knowledge_id, kind])
        existing = s.scalar(select(TaskRow).where(TaskRow.active_key == active_key))
        if existing:
            existing.use_model = existing.use_model or plan.use_model
            return _task(existing)
        now = time.time()
        row = TaskRow(id=str(uuid.uuid4()), notice_id=plan.primary_notice_id, kind="project_" + kind,
            knowledge_id=plan.knowledge_id, revision=plan.revision, active_key=active_key,
            status="queued", priority=priority, use_model=plan.use_model, attempts=0,
            available_at=now, created_at=now, updated_at=now,
            metrics={"project_plan_id": plan.id, "project_key": plan.project_key})
        s.add(row)
        return {"id": row.id, "project_key": plan.project_key, "kind": row.kind}

    def refresh(self, key, knowledge_id, use_model=True, priority=70, force=False, extra_warnings=None):
        notices = self.notices(key)
        selections = select_project(notices)
        if not selections:
            selections = [{"unit_key": "project", "main": None, "corrections": [],
                "ignored_correction_ids": [], "warnings": [], "selection_reason": "缺少采购分析材料",
                "status": "missing_primary", "round_key": None, "lot_key": None}]
        prepared = []
        for selection in selections:
            selection = {**selection, "warnings": [*selection["warnings"], *(extra_warnings or [])]}
            material, revision = build_material(selection)
            prepared.append((selection, material, revision))
        now, result = time.time(), []
        with self.store.session.begin() as s:
            active_ids = []
            for selection, material, revision in prepared:
                plan_id = digest([key, selection["unit_key"]])
                active_ids.append(plan_id)
                plan = s.get(ProjectPlanRow, plan_id, with_for_update=True)
                changed = plan is None or plan.revision != revision or plan.knowledge_id != knowledge_id or not plan.active
                if plan is None:
                    plan = ProjectPlanRow(id=plan_id, project_key=key, unit_key=selection["unit_key"],
                                          created_at=now, use_model=use_model, active=True)
                    s.add(plan)
                old_primary = plan.primary_notice_id
                main = selection["main"]
                plan.selection = {k: v for k, v in selection.items() if k not in ("main", "corrections")}
                plan.selection.update(primary_title=main.title if main else "", primary_type=main.notice_type if main else "",
                    correction_notice_ids=[n.notice_id for n in selection["corrections"]],
                    policy_version=POLICY_VERSION)
                plan.primary_notice_id = main.notice_id if main else None
                plan.input_payload = material.model_dump() if material else None
                plan.active, plan.updated_at = True, now
                requested_model = use_model and not plan.use_model
                plan.use_model = use_model if force else use_model or plan.use_model
                if changed or force:
                    plan.revision, plan.knowledge_id = revision, knowledge_id
                    plan.rule_revision = None
                    plan.material_complete = False
                    plan.warnings = selection["warnings"]
                    plan.analysis_status = "queued" if main else "missing_primary"
                    if main:
                        s.execute(delete(TagRow).where(TagRow.notice_id == main.notice_id, TagRow.source != "manual"))
                        row = s.get(NoticeRow, main.notice_id)
                        row.analysis_status = "queued"
                        row.analysis_meta = {"project_plan_id": plan.id, "project_key": key,
                                             "policy_version": POLICY_VERSION}
                        self._enqueue(s, plan, "rule", priority)
                elif main:
                    row = s.get(NoticeRow, main.notice_id)
                    row.analysis_status = plan.analysis_status
                    if requested_model:
                        self._enqueue(s, plan, "model" if plan.rule_revision == revision else "rule", priority)
                if old_primary and old_primary != plan.primary_notice_id:
                    old = s.get(NoticeRow, old_primary)
                    if old:
                        old.analysis_status = "unselected"
                result.append({"plan_id": plan_id, "changed": changed or force,
                               "primary_notice_id": plan.primary_notice_id, "revision": revision})
            s.execute(update(ProjectPlanRow).where(ProjectPlanRow.project_key == key,
                ProjectPlanRow.id.not_in(active_ids)).values(active=False, analysis_status="superseded"))
        return result

    def internal(self, plan_id):
        with self.store.session() as s:
            row = s.get(ProjectPlanRow, plan_id)
            if row is None:
                raise KeyError(plan_id)
            return {c.name: getattr(row, c.name) for c in ProjectPlanRow.__table__.columns}

    def valid(self, s, task):
        plan_id = task.get("metrics", {}).get("project_plan_id")
        plan = s.get(ProjectPlanRow, plan_id, with_for_update=True) if plan_id else None
        owned = s.scalar(select(TaskRow.id).where(TaskRow.id == task["id"], TaskRow.owner == task["owner"],
                                                 TaskRow.status == "running"))
        if (not plan or not owned or not plan.active or plan.revision != task["revision"]
                or plan.knowledge_id != task["knowledge_id"] or plan.primary_notice_id != task["notice_id"]):
            return None
        primary = s.get(NoticeRow, plan.primary_notice_id)
        if not primary or primary.project_key != plan.project_key:
            return None
        if task["kind"] == "project_model" and not plan.use_model:
            return None
        return plan

    def current(self, task):
        with self.store.session.begin() as s:
            return self.valid(s, task) is not None

    def save_rules(self, task, result):
        with self.store.session.begin() as s:
            plan = self.valid(s, task)
            if not plan:
                return False
            tags = result["tags"]
            self.ws._replace_tags(s, plan.primary_notice_id, "rule", tags)
            plan.rule_revision = plan.revision
            plan.material_complete = result["material_complete"]
            plan.warnings = list(dict.fromkeys([*plan.selection.get("warnings", []), *result["warnings"]]))
            plan.analysis_status = "needs_review" if plan.selection.get("warnings") or not plan.material_complete or not tags or any(
                t["status"] == "candidate" for t in tags) else "ready"
            row = s.get(NoticeRow, plan.primary_notice_id)
            row.analysis_status, row.material_complete, row.warnings = plan.analysis_status, plan.material_complete, plan.warnings
            row.updated_at = plan.updated_at = time.time()
            if plan.use_model and (plan.input_payload or {}).get("analysis_documents"):
                candidate = any(t["status"] == "candidate" for t in tags)
                self._enqueue(s, plan, "model", task["priority"] + (20 if candidate else -20))
            return True

    def save_model(self, task, result):
        with self.store.session.begin() as s:
            plan = self.valid(s, task)
            if not plan:
                return False
            self.ws._replace_tags(s, plan.primary_notice_id, "model", result["tags"])
            plan.analysis_status = "needs_review" if plan.selection.get("warnings") or result["needs_more"] or not plan.material_complete or any(
                t["status"] == "candidate" for t in result["tags"]) else "completed"
            row = s.get(NoticeRow, plan.primary_notice_id)
            row.analysis_status = plan.analysis_status
            row.updated_at = plan.updated_at = time.time()
            s.add(ReviewRow(id=str(uuid.uuid4()), notice_id=plan.primary_notice_id, created_at=time.time(),
                payload={"source": "model", "project_plan_id": plan.id, "project_key": plan.project_key,
                         "revision": plan.revision, "knowledge_id": plan.knowledge_id,
                         "tags": result["tags"], "metrics": result["metrics"], "needs_more": result["needs_more"]}))
            return True

    def mark_failed(self, task):
        with self.store.session.begin() as s:
            plan = self.valid(s, task)
            if plan:
                plan.analysis_status = "failed"
                row = s.get(NoticeRow, plan.primary_notice_id)
                row.analysis_status = "failed"

    def retry(self, task_id):
        with self.store.session.begin() as s:
            task = s.get(TaskRow, task_id)
            if not task:
                raise KeyError(task_id)
            if task.status not in ("failed", "stale"):
                raise ValueError("只能重试失败或过期任务")
            plan = s.get(ProjectPlanRow, (task.metrics or {}).get("project_plan_id"))
            if not plan or not plan.active:
                raise ValueError("项目需求选择已更新，请重新分析项目")
            return self._enqueue(s, plan, "rule" if task.kind == "project_rule" else "model", task.priority)

    def analyses(self, s, keys, include_evidence=False):
        rows = list(s.scalars(select(ProjectPlanRow).where(ProjectPlanRow.project_key.in_(keys),
            ProjectPlanRow.active.is_(True)).options(defer(ProjectPlanRow.input_payload))))
        ids = [r.primary_notice_id for r in rows if r.primary_notice_id]
        tags = defaultdict(list)
        if ids:
            query = select(TagRow).where(TagRow.notice_id.in_(ids), TagRow.effective.is_(True))
            if not include_evidence:
                query = query.options(defer(TagRow.evidence))
            for row in s.scalars(query):
                tags[row.notice_id].append(_tag(row, include_evidence))
        result = defaultdict(list)
        for row in rows:
            result[row.project_key].append({"plan_id": row.id, "unit_key": row.unit_key,
                "primary_notice_id": row.primary_notice_id, **row.selection,
                "analysis_status": row.analysis_status, "material_complete": row.material_complete,
                "revision": row.revision, "knowledge_id": row.knowledge_id, "warnings": row.warnings,
                "tags": tags[row.primary_notice_id], "updated_at": row.updated_at})
        return result

    def listing(self, q="", page=1, page_size=20):
        with self.store.session() as s:
            stmt = select(NoticeRow.project_key, func.count().label("notice_count"),
                          func.max(NoticeRow.updated_at).label("updated_at"), func.min(NoticeRow.title).label("title"))
            stmt = stmt.where(NoticeRow.project_key.is_not(None), NoticeRow.project_key != "")
            if q:
                stmt = stmt.where(NoticeRow.project_key.icontains(q, autoescape=True))
            stmt = stmt.group_by(NoticeRow.project_key)
            total = s.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = list(s.execute(stmt.order_by(func.max(NoticeRow.updated_at).desc())
                .offset((page - 1) * page_size).limit(page_size)).mappings())
            keys = [r["project_key"] for r in rows]
            plans = self.analyses(s, keys)
            phases = {}
            if keys:
                for row in s.execute(select(NoticeRow.project_key, NoticeRow.notice_type)
                    .where(NoticeRow.project_key.in_(keys))
                    .order_by(NoticeRow.publish_at.desc().nulls_last(), NoticeRow.notice_id.desc())):
                    phases.setdefault(row.project_key, row.notice_type)
            items = []
            for row in rows:
                analyses = plans[row["project_key"]]
                states = [a["analysis_status"] for a in analyses]
                state = next((v for v in ("failed", "queued", "needs_review", "missing_primary", "ready", "completed") if v in states), "missing_primary")
                items.append({**dict(row), "analyses": analyses, "current_stage": phases.get(row["project_key"], ""),
                    "analysis_status": state, "analysis_count": len(analyses),
                    "primary_notice_ids": [a["primary_notice_id"] for a in analyses if a["primary_notice_id"]],
                    "tags": [t for a in analyses for t in a["tags"]]})
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    def project(self, key):
        base = self.ws.project(key)
        with self.store.session() as s:
            analyses = self.analyses(s, [key], include_evidence=True)[key]
            stage = s.scalar(select(NoticeRow.notice_type).where(NoticeRow.project_key == key)
                             .order_by(NoticeRow.publish_at.desc().nulls_last(), NoticeRow.notice_id.desc()).limit(1))
        base.update(analyses=analyses, current_stage=stage or "",
                    tags=[t for a in analyses for t in a["tags"]])
        selected = {a["primary_notice_id"] for a in analyses}
        corrections = {i for a in analyses for i in a.get("correction_notice_ids", [])}
        for notice in base["notices"]:
            notice["analysis_role"] = "primary" if notice["notice_id"] in selected else "correction" if notice["notice_id"] in corrections else "context"
        return base

    def review(self, payload, knowledge):
        with self.store.session.begin() as s:
            plan = s.get(ProjectPlanRow, payload.plan_id, with_for_update=True)
            if not plan:
                raise KeyError(payload.plan_id)
            if not plan.active or plan.primary_notice_id is None:
                raise ValueError("项目没有可用主公告，请先补齐采购材料")
            if payload.expected_revision and payload.expected_revision != plan.revision:
                raise ValueError("项目采购需求版本已更新，请刷新后重新复核")
            rows = list(s.scalars(select(TagRow).where(TagRow.notice_id == plan.primary_notice_id)))
            allowed = {(line.business_domain, line.business_line) for line in knowledge.lines} if payload.dimension == "business" else {
                (p.category, p.word) for p in knowledge.product_keywords}
            allowed |= {(t.category, t.label) for t in rows if t.dimension == payload.dimension}
            if (payload.category, payload.label) not in allowed:
                raise ValueError("新标签不在当前词库中，请先维护词库")
            key = (payload.dimension, payload.category, payload.label)
            existing = next((t for t in rows if t.effective and (t.dimension, t.category, t.label) == key), None)
            tag = payload.model_dump(include={"dimension", "category", "label", "status", "reason"})
            tag.update(source="manual", evidence=existing.evidence if existing else [])
            manual = [_tag(t) for t in rows if t.source == "manual" and (t.dimension, t.category, t.label) != key]
            self.ws._replace_tags(s, plan.primary_notice_id, "manual", [*manual, tag])
            s.add(ReviewRow(id=str(uuid.uuid4()), notice_id=plan.primary_notice_id,
                payload={**tag, "project_plan_id": plan.id, "project_key": plan.project_key,
                         "revision": plan.revision, "knowledge_id": plan.knowledge_id}, created_at=time.time()))
            plan.updated_at = time.time()
            s.get(NoticeRow, plan.primary_notice_id).updated_at = plan.updated_at
        return self.project(plan.project_key)

    def stats(self):
        with self.store.session() as s:
            total = s.scalar(select(func.count(func.distinct(NoticeRow.project_key))).where(NoticeRow.project_key.is_not(None)))
            analyzed = s.scalar(select(func.count(func.distinct(ProjectPlanRow.project_key))).where(
                ProjectPlanRow.active.is_(True), ProjectPlanRow.analysis_status.in_(["ready", "completed", "needs_review"])))
            pending = s.scalar(select(func.count(func.distinct(ProjectPlanRow.project_key))).where(
                ProjectPlanRow.active.is_(True), ProjectPlanRow.analysis_status == "missing_primary"))
            reviewed = s.scalar(select(func.count(func.distinct(ProjectPlanRow.id))).join(
                TaskRow, (TaskRow.notice_id == ProjectPlanRow.primary_notice_id) &
                (TaskRow.revision == ProjectPlanRow.revision) &
                (TaskRow.knowledge_id == ProjectPlanRow.knowledge_id)).where(
                    ProjectPlanRow.active.is_(True), TaskRow.kind == "project_model", TaskRow.status == "completed"))
            return {"project_total": total, "project_analyzed": analyzed, "project_pending_material": pending,
                    "model_reviewed": reviewed}

    def reindex_batch(self, knowledge_id, cursor=0, limit=100, use_model=None):
        with self.store.session() as s:
            values = [r.payload for r in s.scalars(select(NoticeRow).where(NoticeRow.notice_id > cursor)
                .order_by(NoticeRow.notice_id).limit(limit))]
            ids = [v["notice_id"] for v in values]
            enabled_ids = set(s.scalars(select(TaskRow.notice_id).where(TaskRow.notice_id.in_(ids),
                TaskRow.use_model.is_(True)).distinct())) if ids and use_model is None else set()
        keys = {}
        for value in values:
            notice = NoticeInput.model_validate(value)
            key = project_key(notice)
            notice = notice.model_copy(update={"project_key": key})
            self.ws.ingest(notice, knowledge_id, False, record_only=True)
            keys[key] = keys.get(key, False) or (notice.notice_id in enabled_ids if use_model is None else use_model)
        changed = sum(int(p["changed"]) for key, enabled in keys.items()
                      for p in self.refresh(key, knowledge_id, enabled, priority=30))
        return {"cursor": max([cursor, *[v["notice_id"] for v in values]]), "received": len(values), "changed": changed}
