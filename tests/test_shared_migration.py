"""Migration safety tests; PG cases require a disposable BID_MIGRATION_TEST_DSN."""
import os
from types import SimpleNamespace
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Json
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from analyzer.sales_store import SalesData, SalesItem, SalesProject, SalesMatch, SalesJob, SalesStore
from analyzer.shared_source import initialize_shared
from analyzer.store import Store
from scripts import migrate_to_shared as migration


def test_pause_preserves_identity_and_encrypted_settings():
    old = {'source_dsn': 'encrypted', 'knowledge_api_key': 'encrypted-key',
           'paused': False, 'incremental_enabled': True, 'history_enabled': True}
    result = migration.paused_settings(old)
    assert result['source_dsn'] == old['source_dsn']
    assert result['knowledge_api_key'] == old['knowledge_api_key']
    assert result['paused'] and not result['incremental_enabled'] and not result['history_enabled']
    assert old['paused'] is False


@pytest.mark.parametrize('name', ['public; drop schema public', '../legacy', 'UPPER', 'a' * 64])
def test_invalid_schema_rejected(name):
    with pytest.raises(migration.MigrationError):
        migration.schema_name(name)


@pytest.fixture
def database(tmp_path):
    base = os.getenv('BID_MIGRATION_TEST_DSN')
    if not base:
        pytest.skip('BID_MIGRATION_TEST_DSN not configured; disposable PostgreSQL required')
    name = 'migration_qa_' + uuid.uuid4().hex[:12]
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    dsn = make_conninfo(base, dbname=name)
    params = conninfo_to_dict(dsn)
    url = URL.create('postgresql+psycopg', username=params.get('user'), password=params.get('password'),
                     host=params.get('host'), port=int(params.get('port', 5432)), database=name)
    key = tmp_path / 'settings.key'
    key.write_bytes(Fernet.generate_key())
    cipher = Fernet(key.read_bytes())
    engine = create_engine(url).execution_options(schema_translate_map={None: 'legacy'})
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE SCHEMA legacy')
            conn.exec_driver_sql('CREATE TABLE public.notices (notice_id bigint, project_key_name text, '
                                 'first_seen_at timestamp, has_procurement_content boolean, procurement_content text)')
            conn.exec_driver_sql('CREATE TABLE public.attachments (att_id bigint, notice_id bigint)')
            conn.exec_driver_sql("CREATE VIEW public.mv_project AS SELECT 'project'::text project_key, '采购公告'::text channel")
            for table in migration.TABLES:
                table.create(conn)
        with Session(engine) as session, session.begin():
            session.add(SalesData(key='settings', value={
                'paused': True, 'incremental_enabled': True,
                'source_dsn': cipher.encrypt(b'old-source-identity').decode(),
                'knowledge_api_key': cipher.encrypt(b'fixture-not-a-real-key').decode(),
            }))
            session.add(SalesData(key='active_source_revision', value='original-source-revision'))
            session.add(SalesItem(id='item', name='访客管理', description='访客预约、审批', categories=['通行'],
                                  products=['园区'], enabled=True, version='item-v1', updated_at=1234.5))
            session.add(SalesProject(project_key='project', title='智慧园区', channel='采购公告', status='matched',
                                     revision='project-v1', material={'_source_revision': 'original-source-revision'},
                                     updated_at=1234.5))
            for identity, revision in [('label', 'project-v1'), ('stale', 'old-version')]:
                session.add(SalesMatch(id=identity, project_key='project', sales_item_id='item', revision=revision,
                                      item_version='item-v1', status='confirmed', method='model', score=0.92,
                                      reason='明确采购能力', evidence=[{'text': '中文\n带\x00历史转义'}], updated_at=1234.5))
            session.add(SalesJob(id='job', kind='analyze', status='completed', payload={'project_key': 'project'},
                                available_at=1234.5, created_at=1234.5, updated_at=1234.5))
        yield SimpleNamespace(dsn=dsn, url=url, key=key, engine=engine)
    finally:
        engine.dispose()
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def run(db, apply=False):
    return migration.migrate(db.dsn, db.url, 'legacy', 'bid_analysis', db.key, apply)


def exists(db):
    with psycopg.connect(db.dsn) as conn:
        return bool(conn.execute("SELECT 1 FROM pg_namespace WHERE nspname='bid_analysis'").fetchone())


def test_copy_preserves_labels_nul_json_timestamps_key_and_view(database):
    before = run(database)
    assert not exists(database)
    assert run(database, True) == before
    store = Store(database.url.render_as_string(hide_password=False), 'bid_analysis')
    try:
        initialize_shared(SimpleNamespace(store=store))
        state = SalesStore(store, SimpleNamespace(sales_secret_key_file=str(database.key),
                                                 sales_engine_default=True, source_file_root='unused'))
        assert state.settings()['knowledge_api_key'] == 'fixture-not-a-real-key'
        assert state.settings()['source_dsn'] == 'old-source-identity'
        assert state.settings()['paused'] and not state.settings()['incremental_enabled']
        with store.session() as session:
            label = session.get(SalesMatch, 'label')
            assert label.evidence == [{'text': '中文\n带\x00历史转义'}]
            assert label.updated_at == 1234.5
        with store.engine.connect() as conn:
            assert conn.exec_driver_sql('SELECT count(*) FROM bid_analysis.v_project_sales_tags').scalar() == 1
        with pytest.raises(migration.MigrationError, match='已存在'):
            run(database, True)
    finally:
        store.engine.dispose()


def test_copy_error_rolls_back_schema_and_all_tables(database, monkeypatch):
    original = migration.copy_table

    def fail_second(*args):
        if args[-1].name == 'sales_projects_v1':
            raise RuntimeError('simulated copy failure')
        return original(*args)

    monkeypatch.setattr(migration, 'copy_table', fail_second)
    with pytest.raises(RuntimeError, match='simulated'):
        run(database, True)
    assert not exists(database)


def test_wrong_key_writes_nothing(database):
    database.key.write_bytes(Fernet.generate_key())
    with pytest.raises(migration.MigrationError, match='密钥'):
        run(database, True)
    assert not exists(database)


def test_running_jobs_and_unpaused_worker_rejected(database):
    with psycopg.connect(database.dsn) as conn:
        conn.execute("UPDATE legacy.sales_jobs_v1 SET status='running'")
    with pytest.raises(migration.MigrationError, match='归零'):
        run(database, True)
    with psycopg.connect(database.dsn) as conn:
        conn.execute("UPDATE legacy.sales_jobs_v1 SET status='completed'")
        settings = conn.execute("SELECT value FROM legacy.sales_runtime_v1 WHERE key='settings'").fetchone()[0]
        settings['paused'] = False
        conn.execute("UPDATE legacy.sales_runtime_v1 SET value=%s WHERE key='settings'", (Json(settings),))
    with pytest.raises(migration.MigrationError, match='暂停'):
        run(database, True)
    assert not exists(database)
