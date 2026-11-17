"""Copy legacy analysis results into a NEW schema in the main PostgreSQL database.

Default: read-only checks. --apply creates the schema in one transaction.
Supply MIGRATION_SOURCE_DSN, DATABASE_URL and the original settings.key.
Can run via `python - < scripts/migrate_to_shared.py` inside the release image.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

import psycopg
from psycopg import sql
from psycopg.types.json import Json
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema

# Support both direct invocation from the checkout and stdin from /app.
if __file__ != '<stdin>':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analyzer.sales_store import (
    SECRETS, SalesAudit, SalesData, SalesItem, SalesJob, SalesMatch, SalesProject,
)

TABLES = tuple(model.__table__ for model in (
    SalesItem, SalesProject, SalesMatch, SalesJob, SalesAudit, SalesData,
))


class MigrationError(Exception):
    """Safe to display: never include connection strings or source record values."""


def schema_name(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value):
        raise MigrationError('schema 只能使用小写字母、数字、下划线，以字母开头，最多 63 字符')
    return value


def check_columns(conn, schema):
    for table in TABLES:
        rows = conn.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema=%s AND table_name=%s', (schema, table.name),
        ).fetchall()
        missing = set(table.columns.keys()) - {r[0] for r in rows}
        if missing:
            raise MigrationError(f'{table.name} 缺少字段：{", ".join(sorted(missing))}；请先核对旧库版本，不自动补造历史字段')


def check_key(settings, path):
    try:
        cipher = Fernet(Path(path).read_bytes().strip())
        for name in SECRETS:
            if settings.get(name):
                cipher.decrypt(settings[name].encode()).decode('utf-8')
    except (OSError, InvalidToken, ValueError, TypeError, AttributeError):
        raise MigrationError('settings.key 缺失、不可读或无法解密原配置；请复制原服务的密钥文件') from None


def counts(conn, schema):
    return {
        table.name: conn.execute(sql.SQL('SELECT count(*) FROM {}').format(
            sql.Identifier(schema, table.name))).fetchone()[0]
        for table in TABLES
    }


def tag_counts(conn, schema):
    return conn.execute(sql.SQL('''
        SELECT count(*), count(DISTINCT p.project_key)
        FROM {} p JOIN {} m ON m.project_key=p.project_key AND m.revision=p.revision
        JOIN {} i ON i.id=m.sales_item_id AND i.version=m.item_version
        WHERE m.status='confirmed' AND i.enabled=true
    ''').format(*(sql.Identifier(schema, name) for name in (
        'sales_projects_v1', 'sales_matches_v1', 'sales_items_v1')))).fetchone()


def paused_settings(settings):
    return dict(settings, paused=True, incremental_enabled=False, history_enabled=False)


def copy_table(source, target, source_schema, target_schema, table):
    columns = sql.SQL(', ').join(sql.Identifier(c.name) for c in table.columns)
    outgoing = sql.SQL('COPY (SELECT {} FROM {}) TO STDOUT WITH (FORMAT CSV, ENCODING \'UTF8\')').format(
        columns, sql.Identifier(source_schema, table.name))
    incoming = sql.SQL('COPY {} ({}) FROM STDIN WITH (FORMAT CSV, ENCODING \'UTF8\')').format(
        sql.Identifier(target_schema, table.name), columns)
    # CSV copies JSON verbatim, including historical escaped \u0000; never cast JSONB.
    with source.cursor().copy(outgoing) as reader, target.cursor().copy(incoming) as writer:
        buffer = bytearray()
        for block in reader:
            buffer.extend(block)
            if len(buffer) >= 1024 * 1024:
                writer.write(buffer)
                buffer.clear()
        if buffer:
            writer.write(buffer)


def migrate(source_dsn, target_url, source_schema, target_schema, key_file, apply=False):
    source_schema, target_schema = schema_name(source_schema), schema_name(target_schema)
    if target_schema in ('public', 'information_schema') or target_schema.startswith('pg_'):
        raise MigrationError('目标必须是独立的新 schema，例如 bid_analysis')
    url = make_url(target_url)
    if url.drivername not in ('postgresql', 'postgresql+psycopg'):
        raise MigrationError('DATABASE_URL 必须使用 PostgreSQL / psycopg')
    engine = create_engine(url.set(drivername='postgresql+psycopg'),
                           connect_args={'connect_timeout': 10}, hide_parameters=True)
    engine = engine.execution_options(schema_translate_map={None: target_schema})
    try:
        with psycopg.connect(source_dsn, connect_timeout=10) as source, engine.begin() as connection:
            source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            source.execute("SET LOCAL lock_timeout='10s'")
            target = connection.connection.driver_connection
            target.execute("SET LOCAL lock_timeout='10s'")
            if target.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (target_schema,)).fetchone():
                raise MigrationError('目标 schema 已存在，拒绝覆盖；如已启动空服务，请停止新服务并另选新的 ANALYSIS_SCHEMA')
            if not target.execute("SELECT has_database_privilege(current_database(),'CREATE')").fetchone()[0]:
                raise MigrationError('目标账号缺少数据库 CREATE 权限，无法创建结果 schema')
            # Confirm the destination really is a readable main database, not the old analysis DB.
            target.execute('SELECT project_key, channel FROM public.mv_project LIMIT 0')
            target.execute('SELECT notice_id, project_key_name, first_seen_at, '
                           'has_procurement_content, procurement_content FROM public.notices LIMIT 0')
            target.execute('SELECT att_id, notice_id FROM public.attachments LIMIT 0')
            check_columns(source, source_schema)
            row = source.execute(sql.SQL('SELECT value FROM {} WHERE key=%s').format(
                sql.Identifier(source_schema, 'sales_runtime_v1')), ('settings',)).fetchone()
            if not row or not isinstance(row[0], dict):
                raise MigrationError('旧库缺少 settings 配置，不能确认迁移来源')
            settings = row[0]
            check_key(settings, key_file)
            running = source.execute(sql.SQL("SELECT count(*) FROM {} WHERE status='running'").format(
                sql.Identifier(source_schema, 'sales_jobs_v1'))).fetchone()[0]
            if running or not settings.get('paused'):
                raise MigrationError('请先在旧页面暂停，等待运行中任务归零，再停止旧 analyzer；旧库 PostgreSQL 保持运行')
            before, before_tags = counts(source, source_schema), tag_counts(source, source_schema)
            print(json.dumps({'mode': 'apply' if apply else 'check', 'tables': before,
                              'valid_labels': before_tags[0], 'tagged_projects': before_tags[1],
                              'key_verified': True}, ensure_ascii=False), flush=True)
            if not apply:
                print('检查通过，未创建目标 schema 或写入数据；确认旧 analyzer 已停止后加 --apply。')
                return before
            connection.execute(CreateSchema(target_schema))
            for table in TABLES:
                table.create(connection, checkfirst=False)
                print(f'正在复制 {table.name}（{before[table.name]} 行）…', flush=True)
                copy_table(source, target, source_schema, target_schema, table)
            after, after_tags = counts(target, target_schema), tag_counts(target, target_schema)
            if before != after or before_tags != after_tags:
                raise MigrationError('迁移前后记录数或有效标签数不一致，目标事务回滚')
            target.execute(sql.SQL('UPDATE {} SET value=%s WHERE key=%s').format(
                sql.Identifier(target_schema, 'sales_runtime_v1')),
                (Json(paused_settings(settings)), 'settings'))
            for table in TABLES:
                target.execute(sql.SQL('ANALYZE {}').format(sql.Identifier(target_schema, table.name)))
        print('迁移已提交：六表行数、有效标签数一致。新库保持暂停；启动服务后核对页面，再开启自动发现。', flush=True)
        return after
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='执行迁移；默认只读检查')
    parser.add_argument('--source-schema', default='public')
    parser.add_argument('--target-schema', default=os.getenv('ANALYSIS_SCHEMA', 'bid_analysis'))
    parser.add_argument('--key-file', default=os.getenv('SALES_SECRET_KEY_FILE', '/app/runtime/settings.key'))
    args = parser.parse_args()
    source_dsn, target_url = os.getenv('MIGRATION_SOURCE_DSN'), os.getenv('DATABASE_URL')
    if not source_dsn or not target_url:
        parser.error('请设置 MIGRATION_SOURCE_DSN（旧库 libpq DSN）和 DATABASE_URL（新主库 URL），不要把密码写到参数里')
    try:
        migrate(source_dsn, target_url, args.source_schema, args.target_schema, args.key_file, args.apply)
    except MigrationError as exc:
        print(f'迁移停止：{exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        # DB exceptions may contain DSNs, JSON evidence or encrypted configuration.
        original = getattr(exc, 'orig', exc)
        code = getattr(original, 'sqlstate', None) or 'unknown'
        print(f'迁移未确认成功：{type(original).__name__}，SQLSTATE={code}。'
              '未提交的目标事务会回滚；如提交阶段断连，请检查目标 schema 后再操作。'
              '请核对连接、权限和旧字段类型；为保护数据不输出原始异常。', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
