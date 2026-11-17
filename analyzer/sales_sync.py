"""Direct read-only source pulls into local tables with durable time-window checkpoints."""

import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from sqlalchemy import JSON, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from analyzer.sales_source import SalesSource
from analyzer.sales_store import SalesData, SalesProject
from analyzer.source import SourceError
from analyzer.source_tables import (
    SourceAttachment,
    SourceNotice,
    SourceProject,
    SyncBatch,
    SyncRecord,
)
from analyzer.store import digest

EARLY = ("采购意向", "采购公告", "更正公告")
ALL = (*EARLY, "结果公告", "合同公告", "验收公告", "废标终止", "其他")
NOTICE_COLUMNS = (
    "notice_id",
    "title",
    "channel",
    "project_key_name",
    "revoke_status",
    "content_path",
    "buyer",
    "publish_at",
    "round_key",
    "lot_key",
    "related_notice_id",
    "content_hash",
    "has_procurement_content",
    "procurement_content",
)


def sync_signature(cfg):
    return digest(
        [
            "source-tables-v1",
            cfg["source_dsn"],
            cfg["allow_later_stages"],
            cfg["collect_attachments"],
            cfg.get("source_notice_time_column", ""),
            cfg.get("source_attachment_time_column", ""),
            cfg.get("source_timezone", "Asia/Shanghai"),
        ]
    )


def source_conninfo(cfg):
    if not cfg.get("source_dsn"):
        raise SourceError("请先配置主服务数据库连接")
    try:
        return make_conninfo(
            cfg["source_dsn"],
            connect_timeout="10",
            application_name="bid-analysis-direct-sync",
            options="-c default_transaction_read_only=on -c statement_timeout=600000 -c search_path=pg_catalog",
        )
    except Exception:
        raise SourceError("主服务数据库连接格式无效") from None


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value, zone="Asia/Shanghai"):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (value if value.tzinfo else value.replace(tzinfo=ZoneInfo(zone))).astimezone(
        timezone.utc
    )


def time_expression(columns, alias, override, candidates, zone):
    names = [override] if override else [n for n in candidates if n in columns]
    if not names:
        return None, []
    expressions = []
    for name in names:
        kind = columns.get(name)
        if kind not in ("timestamp with time zone", "timestamp without time zone"):
            raise SourceError(f"时间字段 {name} 不存在或不是 timestamp 类型，请在页面调整")
        value = alias + '."' + name.replace('"', '""') + '"'
        if kind == "timestamp without time zone":
            value += " AT TIME ZONE '" + zone.replace("'", "''") + "'"
        expressions.append("(" + value + ")")
    return "GREATEST(" + ",".join(expressions) + ")", names


def upsert(session, model, rows, keys):
    if not rows:
        return
    table = model.__table__
    for offset in range(0, len(rows), 500):
        statement = insert(table).values(rows[offset : offset + 500])
        changes = {k: statement.excluded[k] for k in rows[0] if k not in keys}
        comparable = [
            table.c[k].is_distinct_from(statement.excluded[k])
            for k in changes
            if not isinstance(table.c[k].type, JSON)
        ]
        statement = (
            statement.on_conflict_do_update(
                index_elements=keys, set_=changes, where=or_(*comparable) if comparable else None
            )
            if changes
            else statement.on_conflict_do_nothing(index_elements=keys)
        )
        session.execute(statement)


class SourceSync:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.scope, self.signature = digest(cfg["source_dsn"]), sync_signature(cfg)
        self.prefix = f'"{store.schema or "public"}".'

    def require_pg(self):
        if self.store.engine.dialect.name != "postgresql":
            raise SourceError("本地来源表需要 PostgreSQL 分析库")

    def status(self):
        with self.store.session() as session:
            row = session.get(SalesData, "source_sync")
            result = dict(row.value) if row else {}
            checkpoint = session.get(SalesData, "source_checkpoint:" + self.signature)
            schedule = session.get(SalesData, "source_schedule:" + self.signature)
        return {
            **result,
            "ready": result.get("signature") == self.signature,
            "storage": "tables",
            "checkpoint": checkpoint.value.get("end") if checkpoint else None,
            "supported": self.store.engine.dialect.name == "postgresql",
            "next_sync_at": schedule.value.get("next_sync_at")
            if schedule
            else result.get("next_sync_at"),
        }

    @contextmanager
    def remote_connection(self):
        try:
            with psycopg.connect(source_conninfo(self.cfg), row_factory=dict_row) as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                yield conn
        except psycopg.Error as error:
            code = error.sqlstate or "connection"
            raise SourceError(
                f"主库只读连接或查询失败（{code}）；本批未提交、截止点未推进，请检查网络及 SELECT 权限",
                retryable=True,
            ) from None

    def schema(self, conn):
        rows = conn.execute(
            """SELECT c.relname AS table_name,a.attname AS column_name,
            CASE t.typname WHEN 'timestamp' THEN 'timestamp without time zone'
                WHEN 'timestamptz' THEN 'timestamp with time zone' ELSE t.typname END AS data_type
            FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
            WHERE n.nspname='public' AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped""",
            (["notices", "attachments", "mv_project"],),
        ).fetchall()
        tables = {
            t: {r["column_name"]: r["data_type"] for r in rows if r["table_name"] == t}
            for t in ("notices", "attachments", "mv_project")
        }
        if not {
            "notice_id",
            "title",
            "channel",
            "project_key_name",
            "revoke_status",
            "content_path",
        } <= set(tables["notices"]):
            raise SourceError("主库 notices 缺少必需字段或无 SELECT 权限")
        if not {"project_key", "channel"} <= set(tables["mv_project"]):
            raise SourceError("主库 mv_project 缺少 project_key/channel 或无 SELECT 权限")
        zone = self.cfg.get("source_timezone", "Asia/Shanghai")
        expr, names = time_expression(
            tables["notices"],
            "n",
            self.cfg.get("source_notice_time_column", ""),
            ("updated_at", "first_seen_at", "created_at"),
            zone,
        )
        if not expr:
            expr, names = time_expression(
                tables["notices"], "n", "", ("last_seen_at", "fetched_at"), zone
            )
        if not expr:
            raise SourceError(
                "公告没有可用采集/更新时间列，请配置公告增量时间字段；不能用发布日期代替采集时间"
            )
        att_expr, att_names = None, []
        if self.cfg["collect_attachments"]:
            if not {"att_id", "notice_id", "filename", "text_path", "local_path"} <= set(
                tables["attachments"]
            ):
                raise SourceError("主库 attachments 缺少必需字段或无 SELECT 权限")
            att_expr, att_names = time_expression(
                tables["attachments"],
                "a",
                self.cfg.get("source_attachment_time_column", ""),
                ("updated_at", "extract_at", "parsed_at", "fetched_at", "created_at"),
                zone,
            )
        return tables, expr, names, att_expr, att_names

    def check(self):
        with self.remote_connection() as conn:
            _, _, names, _, att_names = self.schema(conn)
            conn.execute("SELECT 1 FROM public.notices LIMIT 1").fetchone()
            conn.execute("SELECT 1 FROM public.mv_project LIMIT 1").fetchone()
            if self.cfg["collect_attachments"]:
                conn.execute("SELECT 1 FROM public.attachments LIMIT 1").fetchone()
        return {
            "ok": True,
            "method": "direct",
            "notice_time_fields": names,
            "attachment_time_fields": att_names,
            "warnings": []
            if att_names or not self.cfg["collect_attachments"]
            else ["附件无时间列，将随公告同步并补查待解析附件"],
            **self.status(),
        }

    def _state(self, session, key, value):
        upsert(session, SalesData, [{"key": key, "value": value}], ["key"])

    def _hydrate(self, remote, session, keys, tables, batch, counts, notice_time_expr):
        projects = remote.execute(
            "SELECT project_key,channel FROM public.mv_project WHERE project_key=ANY(%s)", (keys,)
        ).fetchall()
        event_times = {
            row["project_key_name"]: row["event_at"].timestamp()
            for row in remote.execute(
                f"""SELECT n.project_key_name, max({notice_time_expr}) AS event_at
                    FROM public.notices n WHERE n.project_key_name=ANY(%s)
                    GROUP BY n.project_key_name""", (keys,)
            ).fetchall()
            if row["event_at"] is not None
        }
        grouped = {}
        for row in projects:
            if row["project_key"] in grouped and grouped[row["project_key"]] != row["channel"]:
                raise SourceError("主库同一个 project_key 对应多个阶段，请检查项目聚合")
            grouped[row["project_key"]] = row["channel"]
        eligible = [
            k for k in keys if grouped.get(k) in (ALL if self.cfg["allow_later_stages"] else EARLY)
        ]
        columns = ",".join(
            "n." + n + "::text AS " + n
            if n == "publish_at" and n in tables["notices"]
            else "n." + n
            if n in tables["notices"]
            else "NULL AS " + n
            for n in NOTICE_COLUMNS
        )
        notices = (
            remote.execute(
                "SELECT "
                + columns
                + """ FROM public.notices n WHERE n.project_key_name=ANY(%s)
            AND n.channel=ANY(%s) AND n.revoke_status='active' ORDER BY n.notice_id""",
                (eligible, list(EARLY)),
            ).fetchall()
            if eligible
            else []
        )
        ids = [n["notice_id"] for n in notices]
        attachments = (
            remote.execute(
                """SELECT att_id,notice_id,filename,text_path,local_path FROM public.attachments
            WHERE notice_id=ANY(%s) ORDER BY att_id""",
                (ids,),
            ).fetchall()
            if ids and self.cfg["collect_attachments"]
            else []
        )
        old_ids = set(
            session.scalars(
                select(SourceNotice.notice_id).where(
                    SourceNotice.source_scope == self.scope,
                    SourceNotice.project_key_name.in_(eligible),
                )
            )
        )
        stale = old_ids - set(ids)
        if stale:
            session.execute(
                delete(SourceAttachment).where(
                    SourceAttachment.source_scope == self.scope,
                    SourceAttachment.notice_id.in_(stale),
                )
            )
            session.execute(
                delete(SourceNotice).where(
                    SourceNotice.source_scope == self.scope, SourceNotice.notice_id.in_(stale)
                )
            )
        if ids:
            session.execute(
                delete(SourceAttachment).where(
                    SourceAttachment.source_scope == self.scope,
                    SourceAttachment.notice_id.in_(ids),
                    SourceAttachment.att_id.not_in([a["att_id"] for a in attachments]),
                )
            )
        upsert(
            session,
            SourceNotice,
            [{"source_scope": self.scope, **r} for r in notices],
            ["source_scope", "notice_id"],
        )
        upsert(
            session,
            SourceAttachment,
            [{"source_scope": self.scope, **r} for r in attachments],
            ["source_scope", "att_id"],
        )
        preserved = set(
            session.scalars(
                select(SalesProject.project_key).where(SalesProject.project_key.in_(keys))
            )
        )
        previous = {
            r.project_key: r
            for r in session.scalars(
                select(SourceProject).where(
                    SourceProject.source_scope == self.scope, SourceProject.project_key.in_(keys)
                )
            )
        }
        by_notice = {n["notice_id"]: n["project_key_name"] for n in notices}
        for key in keys:
            data = [n for n in notices if n["project_key_name"] == key]
            atts = [a for a in attachments if by_notice.get(a["notice_id"]) == key]
            old = previous.get(key)
            pending = key in eligible and key not in preserved
            value = {
                "source_scope": self.scope,
                "project_key": key,
                "channel": grouped.get(key) or "",
                "notice_event_at": event_times.get(key),
                "in_scope": key in eligible,
                "revision": digest([grouped.get(key), data, atts]),
                "checked_at": time.time()
                if not old or old.revision != digest([grouped.get(key), data, atts])
                else old.checked_at,
                "pending_analysis": pending
                or bool(old and old.pending_analysis and key not in preserved),
                "batch_id": batch.id if pending else old.batch_id if old else None,
            }
            upsert(session, SourceProject, [value], ["source_scope", "project_key"])
            session.add(
                SyncRecord(
                    batch_id=batch.id, kind="project", source_id=key, project_key=key, payload=value
                )
            )
        for kind, rows, identity in (
            ("notice", notices, "notice_id"),
            ("attachment", attachments, "att_id"),
        ):
            for row in rows:
                key = row.get("project_key_name") or by_notice[row["notice_id"]]
                session.add(
                    SyncRecord(
                        batch_id=batch.id,
                        kind=kind,
                        source_id=str(row[identity]),
                        project_key=key,
                        payload=dict(row),
                    )
                )
        for key, value in {
            "projects": len(keys),
            "notices": len(notices),
            "attachments": len(attachments),
            "retained_results": len(preserved),
            "pending_projects": len(set(eligible) - preserved),
        }.items():
            counts[key] += value

    def refresh(self, progress, batch_id=None):
        self.require_pg()
        batch_id = batch_id or str(uuid.uuid4())
        try:
            with self.remote_connection() as remote:
                tables, expr, fields, att_expr, att_fields = self.schema(remote)
                now = remote.execute("SELECT transaction_timestamp() AS now").fetchone()["now"]
                upper = now - timedelta(seconds=self.cfg.get("source_sync_lag_seconds", 120))
                with self.store.session() as s:
                    checkpoint = s.get(SalesData, "source_checkpoint:" + self.signature)
                    previous = s.get(SyncBatch, batch_id)
                    status_row = s.get(SalesData, "source_sync")
                    status = dict(status_row.value) if status_row else {}
                if previous:
                    if previous.status == "completed":
                        progress(
                            {
                                "phase": "sync_batch_reused",
                                "batch_id": batch_id,
                                "window_end": previous.window_end,
                            }
                        )
                        return self.status()
                    if previous.signature != self.signature:
                        raise SourceError("批次数据源配置已变化，请重新创建同步任务")
                    start, end = parse_time(previous.window_start), parse_time(previous.window_end)
                else:
                    if checkpoint:
                        start = parse_time(checkpoint.value["end"])
                    elif self.cfg.get("source_sync_start_at"):
                        start = parse_time(
                            self.cfg["source_sync_start_at"],
                            self.cfg.get("source_timezone", "Asia/Shanghai"),
                        )
                    else:
                        earliest = remote.execute(
                            "SELECT min("
                            + expr
                            + ") AS earliest FROM public.notices n WHERE n.project_key_name IS NOT NULL"
                        ).fetchone()["earliest"]
                        start = earliest or upper - timedelta(
                            hours=self.cfg.get("source_sync_batch_hours", 24)
                        )
                    end = min(
                        start + timedelta(hours=self.cfg.get("source_sync_batch_hours", 24)), upper
                    )
                if end <= start:
                    return {
                        **self.status(),
                        "idle": True,
                        "next_sync_at": time.time()
                        + self.cfg.get("source_sync_interval_seconds", 1800),
                    }
                read_start = start - timedelta(
                    seconds=self.cfg.get("source_sync_overlap_seconds", 300)
                )
                details = {
                    "notice_time_fields": fields,
                    "attachment_time_fields": att_fields,
                    "timezone": self.cfg.get("source_timezone", "Asia/Shanghai"),
                    "recheck_since": stamp(now - timedelta(days=7)),
                }
                with self.store.session.begin() as s:
                    old = s.get(SyncBatch, batch_id)
                    if old:
                        old.status, old.error, old.attempts = "running", None, old.attempts + 1
                    else:
                        s.add(
                            SyncBatch(
                                id=batch_id,
                                source_scope=self.scope,
                                signature=self.signature,
                                status="running",
                                window_start=stamp(start),
                                window_end=stamp(end),
                                read_start=stamp(read_start),
                                counts={},
                                details=details,
                                attempts=1,
                                started_at=time.time(),
                            )
                        )
                counts = {
                    k: 0
                    for k in (
                        "notice_events",
                        "attachment_events",
                        "projects",
                        "notices",
                        "attachments",
                        "retained_results",
                        "pending_projects",
                    )
                }
                with self.store.session.begin() as s:
                    if not s.execute(select(func.pg_try_advisory_xact_lock(715020261104))).scalar():
                        raise SourceError("已有同步批次正在提交，请稍后重试", retryable=True)
                    current = s.get(
                        SalesData, "source_checkpoint:" + self.signature, with_for_update=True
                    )
                    if current and parse_time(current.value["end"]) != start:
                        raise SourceError("截止点已由其他任务推进，请重新创建下一批")
                    batch = s.get(SyncBatch, batch_id)
                    s.execute(delete(SyncRecord).where(SyncRecord.batch_id == batch_id))
                    seen = set()

                    def hydrate(keys):
                        pending = sorted(set(k for k in keys if k) - seen)
                        if pending:
                            self._hydrate(remote, s, pending, tables, batch, counts, expr)
                            seen.update(pending)
                            progress(
                                {
                                    "phase": "sync_batch_reading",
                                    "batch_id": batch_id,
                                    "window_start": stamp(start),
                                    "window_end": stamp(end),
                                    "counts": dict(counts),
                                }
                            )

                    with remote.cursor(name="notice_changes_" + uuid.uuid4().hex) as cursor:
                        cursor.execute(
                            "SELECT n.notice_id,n.project_key_name FROM public.notices n WHERE "
                            + expr
                            + " >= %s AND "
                            + expr
                            + " < %s AND n.project_key_name IS NOT NULL ORDER BY n.notice_id",
                            (read_start, end),
                        )
                        while rows := cursor.fetchmany(self.cfg.get("source_sync_page_size", 500)):
                            counts["notice_events"] += len(rows)
                            old_keys = s.scalars(
                                select(SourceNotice.project_key_name).where(
                                    SourceNotice.source_scope == self.scope,
                                    SourceNotice.notice_id.in_([r["notice_id"] for r in rows]),
                                )
                            ).all()
                            hydrate([r["project_key_name"] for r in rows] + old_keys)
                    if self.cfg["collect_attachments"] and att_expr:
                        with remote.cursor(name="attachment_changes_" + uuid.uuid4().hex) as cursor:
                            cursor.execute(
                                """SELECT n.project_key_name FROM public.attachments a JOIN public.notices n ON n.notice_id=a.notice_id
                                WHERE """
                                + att_expr
                                + " >= %s AND "
                                + att_expr
                                + " < %s AND n.project_key_name IS NOT NULL ORDER BY a.att_id",
                                (read_start, end),
                            )
                            while rows := cursor.fetchmany(
                                self.cfg.get("source_sync_page_size", 500)
                            ):
                                counts["attachment_events"] += len(rows)
                                hydrate([r["project_key_name"] for r in rows])
                    after = status.get("recheck_cursor", "") if status.get("source_scope") == self.scope else ""
                    recheck_window = SourceProject.notice_event_at.between(
                        (now - timedelta(days=7)).timestamp(), now.timestamp()
                    )
                    pending_keys = select(SourceProject.project_key).where(
                        SourceProject.source_scope == self.scope, SourceProject.channel == "",
                        recheck_window,
                    )
                    if self.cfg["collect_attachments"]:
                        pending_keys = pending_keys.union(
                            select(SourceNotice.project_key_name)
                            .join(
                                SourceProject,
                                (SourceProject.source_scope == SourceNotice.source_scope)
                                & (SourceProject.project_key == SourceNotice.project_key_name),
                            )
                            .join(
                                SourceAttachment,
                                (SourceAttachment.notice_id == SourceNotice.notice_id)
                                & (SourceAttachment.source_scope == SourceNotice.source_scope),
                            )
                            .where(
                                SourceNotice.source_scope == self.scope,
                                recheck_window,
                                or_(
                                    SourceAttachment.text_path.is_(None),
                                    SourceAttachment.text_path == "",
                                ),
                            )
                        )
                    pending_keys = pending_keys.subquery()
                    recheck = list(
                        s.scalars(
                            select(pending_keys.c.project_key)
                            .where(pending_keys.c.project_key > after)
                            .order_by(pending_keys.c.project_key)
                            .limit(self.cfg.get("source_sync_page_size", 500))
                        )
                    )
                    if not recheck and after:
                        recheck = list(
                            s.scalars(
                                select(pending_keys.c.project_key)
                                .order_by(pending_keys.c.project_key)
                                .limit(self.cfg.get("source_sync_page_size", 500))
                            )
                        )
                    hydrate(recheck)
                    total_projects = s.scalar(
                        select(func.count())
                        .select_from(SourceProject)
                        .where(
                            SourceProject.source_scope == self.scope,
                            SourceProject.in_scope.is_(True),
                            SourceProject.channel.in_(
                                ALL if self.cfg["allow_later_stages"] else EARLY
                            ),
                        )
                    )
                    total_notices = s.scalar(
                        select(func.count())
                        .select_from(SourceNotice)
                        .where(SourceNotice.source_scope == self.scope)
                    )
                    total_atts = s.scalar(
                        select(func.count())
                        .select_from(SourceAttachment)
                        .where(SourceAttachment.source_scope == self.scope)
                    )
                    completed = time.time()
                    status = {
                        "signature": self.signature,
                        "source_scope": self.scope,
                        "storage": "tables",
                        "batch_id": batch_id,
                        "synced_at": completed,
                        "counts": {
                            "projects": total_projects,
                            "notices": total_notices,
                            "attachments": total_atts,
                        },
                        "duration_seconds": round(completed - batch.started_at, 2),
                        "checkpoint": stamp(end),
                        "next_sync_at": completed
                        + (
                            0 if end < upper else self.cfg.get("source_sync_interval_seconds", 1800)
                        ),
                        "recheck_cursor": recheck[-1] if recheck else "",
                        "allow_later_stages": self.cfg["allow_later_stages"],
                        "collect_attachments": self.cfg["collect_attachments"],
                    }
                    batch.status, batch.counts, batch.completed_at = (
                        "completed",
                        dict(counts),
                        completed,
                    )
                    self._state(
                        s,
                        "source_checkpoint:" + self.signature,
                        {"end": stamp(end), "batch_id": batch_id},
                    )
                    self._state(s, "source_sync", status)
                    progress(
                        {
                            "phase": "sync_batch_committing",
                            "batch_id": batch_id,
                            "counts": dict(counts),
                            "window_end": stamp(end),
                        }
                    )
                return status
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, SourceError)
                else "本地批次写入失败，未推进截止点，请检查分析库权限与日志"
            )
            with self.store.session.begin() as s:
                row = s.get(SyncBatch, batch_id)
                if row:
                    row.status, row.error, row.completed_at = "failed", message, time.time()
                state = s.get(SalesData, "source_sync")
                if state:
                    state.value = {
                        **state.value,
                        "last_error": message,
                        "next_sync_at": time.time()
                        + self.cfg.get("source_sync_interval_seconds", 1800),
                    }
            if isinstance(error, SourceError):
                raise
            raise SourceError(message, retryable=True) from None


class LocalResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class SnapshotConnection:
    def __init__(self, conn, prefix, scope):
        self.conn, self.prefix, self.scope = conn, prefix, scope

    def execute(self, sql, params=None):
        for source, target in (
            ("public.mv_project", "sales_source_projects"),
            ("public.notices", "sales_source_notices"),
            ("public.attachments", "sales_source_attachments"),
        ):
            where = (
                "source_scope='"
                + self.scope
                + "'"
                + (" AND in_scope=true" if target == "sales_source_projects" else "")
            )
            sql = sql.replace(
                source,
                "(SELECT * FROM " + self.prefix + target + " WHERE " + where + ") AS " + target,
            )
        if not sql.lstrip().startswith("SELECT") or "public." in sql:
            raise SourceError("本地来源只允许查询已同步的项目、公告及附件表")
        return LocalResult(self.conn.exec_driver_sql(sql, params or ()).mappings().all())


class SnapshotSource(SalesSource):
    def __init__(self, settings, store):
        super().__init__(settings)
        self.store = store

    @contextmanager
    def _connection(self):
        sync = SourceSync(self.store, self.settings)
        sync.require_pg()
        try:
            with self.store.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as conn:
                with conn.begin():
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    row = conn.exec_driver_sql(
                        f"SELECT value FROM {sync.prefix}sales_runtime_v1 WHERE key='source_sync'"
                    ).scalar()
                    if not row or row.get("signature") != sync_signature(self.settings):
                        raise SourceError(
                            "请先同步本地来源表；数据源或范围变化后需启动新的时间批次"
                        )
                    self.snapshot_at = row.get("synced_at")
                    current_project = (
                        conn.exec_driver_sql(
                            f"""SELECT revision,checked_at FROM {sync.prefix}sales_source_projects
                        WHERE source_scope=%s AND project_key=%s""",
                            (sync.scope, getattr(self, "project_key", "")),
                        )
                        .mappings()
                        .first()
                    )
                    self.project_revision = current_project["revision"] if current_project else None
                    self.material_at = (
                        current_project["checked_at"] if current_project else self.snapshot_at
                    )
                    yield SnapshotConnection(conn, sync.prefix, sync.scope)
        except SQLAlchemyError:
            raise SourceError(
                "本地来源表读取失败，请检查升级迁移和分析库权限", retryable=True
            ) from None

    def _notice_columns(self, conn):
        return set(NOTICE_COLUMNS)

    def project_material(self, project_key, allow_later=False, *, raw_files=False):
        self.project_key = project_key
        material = super().project_material(project_key, allow_later, raw_files=raw_files)
        material["_snapshot_at"] = self.snapshot_at
        material["_source_project_revision"] = getattr(self, "project_revision", None)
        if self.settings.get("source_download_url"):
            from analyzer.material_files import MaterialFiles

            files = MaterialFiles(
                {
                    **self.settings,
                    "_material_snapshot_at": getattr(self, "material_at", self.snapshot_at),
                }
            )
            for a in material.get("attachments", []):
                if a.get("local_path"):
                    try:
                        files.fetch(
                            a["local_path"], refresh=self.settings.get("_refresh_downloads", False)
                        )
                    except (SourceError, ValueError) as error:
                        material["warnings"].append(f"附件原件{a['att_id']}：{error}")
            material["material_storage"] = "download_cache"
        return material

    def _resolve_file(self, relative):
        if self.settings.get("source_download_url"):
            from analyzer.material_files import MaterialFiles

            return MaterialFiles(
                {
                    **self.settings,
                    "_material_snapshot_at": getattr(self, "material_at", self.snapshot_at),
                }
            ).fetch(
                relative,
                max_bytes=20 * 1024 * 1024,
                refresh=self.settings.get("_refresh_downloads", False),
            )
        return super()._resolve_file(relative)

    def _missing_project(self, project_key):
        return {
            "project_key": project_key,
            "title": project_key,
            "channel": "已过滤",
            "status": "stage_filtered",
            "chunks": [],
            "notices": [],
            "units": [],
            "main_notice_id": None,
            "main_notice_ids": [],
            "correction_notice_ids": [],
            "material_storage": "local",
            "collect_attachments": self.settings["collect_attachments"],
            "warnings": ["本地来源表未包含此项目，请检查阶段范围及同步截止时间"],
        }

    def profile(self):
        return SourceSync(self.store, self.settings).status()
