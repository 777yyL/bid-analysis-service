from datetime import datetime

import pytest

from analyzer.sales_source import SalesSource, parse_vector
from analyzer.source import SourceError


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Database:
    def __init__(self, channel="合同公告"):
        self.channel, self.queries = channel, []
        self.notices = [
            {"notice_id": 1, "title": "访客软件采购", "channel": "采购公告", "revoke_status": "active",
             "content_path": "body.html", "publish_at": datetime(2026, 10, 1)},
            {"notice_id": 2, "title": "采购结果", "channel": "结果公告", "revoke_status": "active",
             "publish_at": datetime(2026, 10, 2)}]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        assert sql.startswith(("SELECT", "SET TRANSACTION"))
        if "information_schema" in sql:
            return Rows([{"column_name": x} for x in self.notices[0]] + [{"column_name": "project_key_name"}])
        if "FROM public.mv_project" in sql:
            return Rows([{"project_key": "p'1", "channel": self.channel}])
        if "FROM public.notices WHERE" in sql:
            return Rows(self.notices)
        if "FROM public.attachments" in sql:
            return Rows([{'att_id': 3, 'notice_id': 1, 'filename': '附件', 'text_path': 'att.txt'}])
        return Rows([])


def source(monkeypatch, database):
    def connect(*args, **kwargs):
        assert "default_transaction_read_only=on" in kwargs["options"]
        return database
    monkeypatch.setattr("analyzer.sales_source.psycopg.connect", connect)
    result = SalesSource({"source_dsn": "postgresql://readonly@source/bidinfo"})
    result._read_text = lambda path: '采购需求：访客预约管理软件'
    return result


def test_shared_source_waits_for_upstream_extraction_without_reading_raw(monkeypatch):
    db=Database('采购公告')
    db.notices[0].update(has_procurement_content=None,procurement_content=None)
    svc=source(monkeypatch,db)
    svc.upstream_only=True
    svc._read_text=lambda path: (_ for _ in ()).throw(AssertionError('must not read raw files'))
    result=svc.project_material("p'1")
    assert result['status']=='awaiting_extraction' and result['documents']==[]
    db.notices[0].update(has_procurement_content=True,procurement_content='采购访客管理软件，实现预约审批')
    result=svc.project_material("p'1")
    assert result['status']=='ready' and result['documents'][0]['upstream_extracted']


def test_later_phase_filtered_before_loading_material(monkeypatch):
    db = Database()
    result = source(monkeypatch, db).project_material("p'1")
    assert result["status"] == "stage_filtered"
    assert not any("FROM public.bid_chunks" in q for q, _ in db.queries)


def test_body_only_when_attachment_collection_disabled(monkeypatch):
    db = Database()
    svc = source(monkeypatch, db)
    svc.settings["collect_attachments"] = False
    result = svc.project_material("p'1", allow_later=True)
    assert result['main_notice_id'] == 1
    assert result['channel'] == '合同公告'
    assert [d['source_type'] for d in result['documents']] == ['notice']
    assert not any('bid_chunks' in q or 'public.attachments' in q for q, _ in db.queries)
    assert all(q.startswith(('SELECT', 'SET TRANSACTION')) for q, _ in db.queries)


def test_attachments_are_read_by_default(monkeypatch):
    db = Database('采购公告')
    svc = source(monkeypatch, db)
    result = svc.project_material("p'1")
    assert [d['source_type'] for d in result['documents']] == ['notice', 'attachment']
    assert result['documents'][1]['attachment_id'] == 3
    assert not any('bid_chunks' in q for q, _ in db.queries)


@pytest.mark.parametrize("found", [True, False, None])
def test_upstream_procurement_content_controls_material(monkeypatch, found):
    db = Database('采购公告')
    db.notices[0].update(has_procurement_content=found,
                         procurement_content='访客预约及审批' if found else None)
    svc = source(monkeypatch, db)
    result = svc.project_material("p'1")
    if found is True:
        assert [(d['content'], d['upstream_extracted']) for d in result['documents']] == [
            ('访客预约及审批', True)]
    elif found is False:
        assert result['status'] == 'no_procurement_demand'
        assert not result['documents']
    else:
        assert len(result['documents']) == 2
    assert len(svc.project_material("p'1", raw_files=True)['documents']) == 2


def test_project_cursor_uses_bound_parameters(monkeypatch):
    db = Database('采购公告')
    source(monkeypatch, db).list_projects("p'0", 7)
    query, params = db.queries[-1]
    assert 'channel = ANY(%s)' in query and params[-1] == 7
    assert "p'0" not in query


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan"), 1], [True, 1], "[Infinity]", "garbage"])
def test_invalid_vector_rejected(vector):
    with pytest.raises(SourceError):
        parse_vector(vector)
