"""Real PostgreSQL regression tests, enabled explicitly with BID_TEST_PG_DSN."""

import os
import uuid
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy import select
from sqlalchemy.engine import URL

from analyzer.config import Settings
from analyzer.sales_service import SalesService
from analyzer.sales_store import SalesMatch, SalesProject, record
from analyzer.sales_sync import SourceSync, sync_signature
from analyzer.source import SourceError
from analyzer.source_migration import LEGACY_TABLES, migrate_local_sources
from analyzer.source_tables import (
    SourceAttachment,
    SourceNotice,
    SourceProject,
    SyncBatch,
    SyncRecord,
)
from analyzer.store import Store, digest


@pytest.fixture
def pg_source(tmp_path):
    dsn = os.getenv("BID_TEST_PG_DSN")
    if not dsn:
        pytest.skip("BID_TEST_PG_DSN not configured")
    settings = conninfo_to_dict(dsn)
    suffix = uuid.uuid4().hex[:12]
    database, schema = "bid_source_qa_" + suffix, "qa_incremental_" + suffix
    source_dsn = make_conninfo(dsn, dbname=database, connect_timeout=5)
    url = URL.create(
        "postgresql+psycopg",
        username=settings["user"],
        password=settings.get("password"),
        host=settings.get("host"),
        port=int(settings.get("port", 5432)),
        database=settings["dbname"],
    ).render_as_string(hide_password=False)
    store = None
    try:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        with psycopg.connect(source_dsn) as conn:
            conn.execute("CREATE TABLE project_seed(project_key text,channel text)")
            conn.execute(
                "INSERT INTO project_seed VALUES ('p1','采购公告'),('p2','采购公告'),('done','采购公告')"
            )
            conn.execute("CREATE MATERIALIZED VIEW mv_project AS SELECT * FROM project_seed")
            conn.execute("""CREATE TABLE notices(notice_id bigint PRIMARY KEY,title text,channel text,
                project_key_name text,revoke_status text,content_path text,publish_at timestamp(0),
                first_seen_at timestamp(0) NOT NULL,updated_at timestamp(0))""")
            conn.execute("""INSERT INTO notices VALUES
                (1,'新项目一','采购公告','p1','active','body.txt','2026-06-01','2026-11-01 08:00',NULL),
                (2,'边界项目二','采购公告','p2','active','body2.txt','2026-06-01','2026-11-02 08:00',NULL),
                (3,'已完成项目','采购公告','done','active','old.txt','2026-06-01','2026-11-01 09:00',NULL)""")
            conn.execute("""CREATE TABLE attachments(att_id bigint PRIMARY KEY,notice_id bigint,
                filename text,text_path text,local_path text,fetched_at timestamp(0))""")
            conn.execute(
                "INSERT INTO attachments VALUES (10,1,'规格.pdf',NULL,'spec.pdf','2026-11-01 08:15')"
            )
        store = Store(url, schema)
        store.initialize(sales_only=True)
        bootstrap = Settings(
            _env_file=None,
            database_url=url,
            analysis_schema=schema,
            sales_secret_key_file=str(tmp_path / "key"),
            worker_enabled=False,
        )
        service = SalesService(store, bootstrap)
        service.save_settings(
            {
                "source_dsn": source_dsn,
                "source_sync_start_at": "2026-11-01T00:00:00Z",
                "source_sync_overlap_seconds": 0,
                "source_sync_lag_seconds": 0,
                "source_sync_batch_hours": 24,
            }
        )
        with store.session.begin() as session:
            session.add(
                SalesProject(
                    project_key="done",
                    title="已完成结果不可改",
                    channel="采购公告",
                    status="matched",
                    revision="finished",
                    material={"_source_revision": digest(source_dsn), "original": "keep"},
                    updated_at=123,
                )
            )
            session.add(
                SalesMatch(
                    id="finished-label",
                    project_key="done",
                    sales_item_id="existing",
                    revision="finished",
                    item_version="v1",
                    status="confirmed",
                    method="model",
                    score=0.95,
                    reason="已完成",
                    evidence=[{"content": "keep"}],
                )
            )
        yield service, source_dsn, dsn, schema
    finally:
        if store:
            store.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))


def frozen(service):
    with service.store.session() as session:
        return record(session.get(SalesProject, "done")), record(
            session.get(SalesMatch, "finished-label")
        )


def test_time_batches_boundary_dedup_and_completed_results_unchanged(pg_source):
    service, dsn, _, _ = pg_source
    before = frozen(service)
    sync = SourceSync(service.store, service.settings())
    assert sync.check()["method"] == "direct"
    first = sync.refresh(lambda p: None, "first-batch")
    assert first["checkpoint"] == "2026-11-02T00:00:00+00:00"
    with service.store.session() as session:
        assert session.get(SourceNotice, (digest(dsn), 2)) is None
        batch = session.get(SyncBatch, "first-batch")
        assert batch.counts["retained_results"] == 1 and batch.counts["pending_projects"] == 1
        assert session.get(SourceProject, (digest(dsn), "done")).pending_analysis is False
        assert list(session.scalars(select(SyncRecord)))
    # A job reclaimed after committing must reuse its batch instead of advancing twice.
    assert sync.refresh(lambda p: None, "first-batch")["checkpoint"] == first["checkpoint"]
    second = sync.refresh(lambda p: None, "second-batch")
    assert second["checkpoint"] == "2026-11-03T00:00:00+00:00"
    with service.store.session() as session:
        assert session.get(SourceNotice, (digest(dsn), 2)) is not None
    assert frozen(service) == before


def test_many_rows_at_the_same_timestamp_are_not_lost_between_pages(pg_source):
    service, dsn, _, _ = pg_source
    service.save_settings({"source_sync_page_size": 10})
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO notices VALUES (%s,'同秒公告','采购公告','p1','active',
                'body.txt','2026-06-01','2026-11-01 08:00',NULL)""",
                [(n,) for n in range(10, 35)],
            )
    SourceSync(service.store, service.settings()).refresh(lambda p: None, "same-second")
    with service.store.session() as session:
        assert session.get(SyncBatch, "same-second").counts["notice_events"] == 27
        assert len(list(session.scalars(select(SourceNotice)))) == 27


def test_failed_batch_rolls_back_and_retries_same_window(pg_source):
    service, dsn, _, _ = pg_source
    sync = SourceSync(service.store, service.settings())

    def fail(progress):
        if progress["phase"] == "sync_batch_reading":
            raise ValueError("simulate interruption")

    with pytest.raises(SourceError, match="未推进"):
        sync.refresh(fail, "retry-batch")
    assert sync.status()["checkpoint"] is None
    with service.store.session() as session:
        assert session.get(SourceNotice, (digest(dsn), 1)) is None
        assert session.get(SyncBatch, "retry-batch").status == "failed"
    sync.refresh(lambda p: None, "retry-batch")
    with service.store.session() as session:
        assert session.get(SyncBatch, "retry-batch").attempts == 2
        assert session.get(SyncBatch, "retry-batch").status == "completed"


def test_late_attachment_text_is_rechecked_without_timestamp_change(pg_source):
    service, dsn, _, _ = pg_source
    sync = SourceSync(service.store, service.settings())
    sync.refresh(lambda p: None, "first")
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE attachments SET text_path='spec.pdf.mineru.txt' WHERE att_id=10")
    sync.refresh(lambda p: None, "next")
    with service.store.session() as session:
        assert session.get(SourceAttachment, (digest(dsn), 10)).text_path == "spec.pdf.mineru.txt"


@pytest.mark.asyncio
async def test_incremental_dispatches_only_unprocessed_projects(pg_source):
    service, dsn, _, _ = pg_source
    sync = SourceSync(service.store, service.settings())
    sync.refresh(lambda p: None, "first")
    before = frozen(service)
    service.save_settings({"incremental_enabled": True})
    service.set_state(
        "source_schedule:" + sync_signature(service.settings()), {"next_sync_at": 9999999999}
    )
    await service.scan_once()
    jobs = service.jobs()["items"]
    assert [j["payload"]["project_key"] for j in jobs if j["kind"] == "analyze"] == ["p1"]
    service.enqueue("analyze", {"project_key": "done", "force": False})
    # Directly exercise the production preservation guard with a claimed job.
    job = service.claim()
    if job["payload"]["project_key"] == "p1":
        service.finish(job)
        job = service.claim()
    await service.analyze(job, service.settings())
    service.finish(job)
    assert frozen(service) == before


def test_migration_copies_old_views_then_removes_legacy_tables(pg_source):
    service, dsn, admin_dsn, schema = pg_source
    before = frozen(service)
    scope = digest(dsn)
    old_time = datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()
    service.set_state(
        "source_sync",
        {
            "source_scope": scope,
            "synced_at": old_time,
            "signature": "old",
            "counts": {"projects": 2, "notices": 1, "attachments": 0},
        },
    )
    with psycopg.connect(admin_dsn) as conn:
        prefix = sql.Identifier(schema)
        conn.execute(
            sql.SQL(
                "CREATE MATERIALIZED VIEW {}.mv_sales_source_projects AS SELECT * FROM (VALUES ('done'::text,'采购公告'::text),('unprocessed','采购公告')) AS x(project_key,channel)"
            ).format(prefix)
        )
        conn.execute(
            sql.SQL("""CREATE MATERIALIZED VIEW {}.mv_sales_source_notices AS SELECT 8::bigint notice_id,
            '旧公告'::text title,'采购公告'::text channel,'done'::text project_key_name,'active'::text revoke_status,
            'old.txt'::text content_path,NULL::text buyer,NULL::text publish_at,NULL::text round_key,
            NULL::text lot_key,NULL::bigint related_notice_id""").format(prefix)
        )
        conn.execute(
            sql.SQL("""CREATE MATERIALIZED VIEW {}.mv_sales_source_attachments AS SELECT NULL::bigint att_id,
            NULL::bigint notice_id,NULL::text filename,NULL::text text_path WHERE false""").format(
                prefix
            )
        )
        for name in LEGACY_TABLES:
            conn.execute(sql.SQL("CREATE TABLE {}.{}(id int)").format(prefix, sql.Identifier(name)))
    migrate_local_sources(service)
    with service.store.session() as session:
        assert session.get(SourceNotice, (scope, 8)).content_path == "old.txt"
        assert session.get(SourceProject, (scope, "done")).pending_analysis is False
        assert session.get(SourceProject, (scope, "unprocessed")).pending_analysis is True
    with psycopg.connect(admin_dsn) as conn:
        assert (
            conn.execute(
                """SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND (c.relname LIKE 'analysis_%%' OR c.relname LIKE 'mv_sales_source_%%')""",
                (schema,),
            ).fetchone()[0]
            == 0
        )
    assert (
        SourceSync(service.store, service.settings()).status()["checkpoint"]
        == "2026-11-02T00:00:00+00:00"
    )
    assert frozen(service) == before
    migrate_local_sources(service)
