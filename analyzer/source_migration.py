"""One-time, transactional retirement of the explicitly named legacy workbench objects."""

import time
from datetime import datetime, timezone

from psycopg.conninfo import conninfo_to_dict
from sqlalchemy import text

from analyzer.sales_store import SalesData
from analyzer.source import SourceError
from analyzer.store import digest

LEGACY_TABLES = (
    "analysis_project_plans_v3",
    "analysis_tasks_v2",
    "analysis_reviews_v2",
    "analysis_tags_v2",
    "analysis_notices_v2",
    "analysis_jobs",
    "analysis_knowledge",
    "analysis_state",
    "analysis_result_cache",
    "analysis_text_cache",
)
LEGACY_VIEWS = ("v_project_analysis", "mv_project_analysis")
MIGRATION = "local_source_tables_v1"


def migrate_local_sources(service):
    store, cfg = service.store, service.settings()
    if store.engine.dialect.name != "postgresql":
        return
    if cfg["source_dsn"] and not store.schema:
        source = conninfo_to_dict(cfg["source_dsn"])
        target = store.engine.url
        if (
            source.get("dbname") == target.database
            and source.get("host", "localhost") == (target.host or "localhost")
            and int(source.get("port", 5432)) == (target.port or 5432)
        ):
            raise SourceError("主库与分析库相同，清理旧结构前必须配置独立 ANALYSIS_SCHEMA")
    schema = store.schema or "public"
    prefix = f'"{schema}".'
    from analyzer.sales_sync import sync_signature

    signature = sync_signature(cfg)
    with store.engine.begin() as conn:
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(715020261104)")
        if conn.execute(
            text(f"SELECT value FROM {prefix}sales_runtime_v1 WHERE key=:key"), {"key": MIGRATION}
        ).scalar():
            return
        old = (
            conn.execute(
                text(f"SELECT value FROM {prefix}sales_runtime_v1 WHERE key='source_sync'")
            ).scalar()
            or {}
        )
        scope = old.get("source_scope") or digest(cfg["source_dsn"])
        objects = dict(
            conn.exec_driver_sql(
                """SELECT c.relname, c.relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s""",
                (schema,),
            ).all()
        )
        copied = False
        for kind, columns in (
            ("projects", "project_key, channel"),
            (
                "notices",
                "notice_id, title, channel, project_key_name, revoke_status, content_path, buyer, publish_at, round_key, lot_key, related_notice_id",
            ),
            ("attachments", "att_id, notice_id, filename, text_path, local_path"),
        ):
            name = "mv_sales_source_" + kind
            if objects.get(name) != "m":
                continue
            populated = conn.exec_driver_sql(
                """SELECT c.relispopulated FROM pg_class c JOIN pg_namespace n
                ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s""",
                (schema, name),
            ).scalar()
            if populated:
                destination = f"{prefix}sales_source_{kind}"
                extra_columns, extra_values = ("", "")
                if kind == "projects":
                    extra_columns = ", in_scope, revision, pending_analysis"
                    extra_values = f", true, md5(project_key), NOT EXISTS (SELECT 1 FROM {prefix}sales_projects_v1 p WHERE p.project_key={name}.project_key)"
                source_columns = columns
                if kind == "attachments":
                    has_local = conn.exec_driver_sql(
                        """SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
                        JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s
                        AND a.attname='local_path' AND NOT a.attisdropped""",
                        (schema, name),
                    ).scalar()
                    if not has_local:
                        source_columns = (
                            "att_id, notice_id, filename, text_path, NULL::text AS local_path"
                        )
                conn.exec_driver_sql(
                    f"""INSERT INTO {destination} (source_scope, {columns}{extra_columns})
                    SELECT %s, {source_columns}{extra_values} FROM {prefix}{name} ON CONFLICT DO NOTHING""",
                    (scope,),
                )
                copied = True
            conn.exec_driver_sql(f"DROP MATERIALIZED VIEW {prefix}{name}")
        for name in LEGACY_VIEWS:
            if objects.get(name) in ("m", "v"):
                noun = "MATERIALIZED VIEW" if objects[name] == "m" else "VIEW"
                conn.exec_driver_sql(f"DROP {noun} {prefix}{name}")
        for name in LEGACY_TABLES:
            if objects.get(name) in ("r", "p"):
                # No CASCADE: unknown user-owned dependencies stop and roll back the entire migration.
                conn.exec_driver_sql(f"DROP TABLE {prefix}{name}")
        if copied and scope == digest(cfg["source_dsn"]):
            old.update(signature=signature, storage="tables", migrated_at=time.time())
            conn.execute(
                SalesData.__table__.update().where(SalesData.key == "source_sync").values(value=old)
            )
            if old.get("synced_at"):
                end = datetime.fromtimestamp(old["synced_at"], timezone.utc).isoformat()
                conn.execute(
                    SalesData.__table__.insert().values(
                        key="source_checkpoint:" + signature,
                        value={"end": end, "origin": "migrated_snapshot"},
                    )
                )
        conn.execute(
            SalesData.__table__.insert().values(
                key=MIGRATION,
                value={
                    "at": time.time(),
                    "copied_snapshot": copied,
                    "retired_tables": list(LEGACY_TABLES),
                },
            )
        )
