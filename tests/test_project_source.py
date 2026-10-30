from types import SimpleNamespace

import httpx
import pytest

from analyzer.config import Settings
from analyzer.source import SourceClient, SourceError


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class ProjectDatabase:
    def __init__(self, notices, attachments=(), project_column="project_key_name"):
        self.notices, self.attachments = notices, attachments
        self.project_column, self.queries = project_column, []
        self.projects = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.queries.append((query, params))
        assert query.startswith(("SELECT ", "SET TRANSACTION "))
        if "information_schema.columns" in query:
            columns = {
                "notices": ["notice_id", "title", "channel", "notice_type", self.project_column, "content_path",
                            "publish_at", "revoke_status", "round_key", "lot_key", "related_notice_id", "updated_at"],
                "attachments": ["att_id", "notice_id", "filename", "text_path", "extract_status"],
                "lots": ["notice_id", "lot_no", "lot_name"],
            }
            return Rows([{"table_name": table, "column_name": name}
                         for table, names in columns.items() for name in names])
        if "FROM public.notices" in query:
            phases = {row["project_key"]: row["channel"] for row in self.projects}
            return Rows([{**row, "project_channel": phases.get(row.get(self.project_column))} for row in self.notices])
        if "FROM public.attachments" in query:
            return Rows([a for a in self.attachments if a["notice_id"] in params[0]])
        return Rows([])


def cfg(root):
    settings = Settings(_env_file=None, source_db_dsn="postgresql://readonly@db/source")
    return SimpleNamespace(**{**settings.model_dump(), "source_file_root": str(root)})


@pytest.mark.asyncio
async def test_result_project_backtracks_purchase_and_never_opens_result_material(tmp_path, monkeypatch):
    (tmp_path / "purchase.md").write_text("采购交换机10台", encoding="utf-8")
    (tmp_path / "purchase.mineru.txt").write_text("交换机技术清单", encoding="utf-8")
    database = ProjectDatabase([
        {"notice_id": 10, "title": "采购公告", "channel": "采购公告", "notice_type": "公开招标", "project_key_name": "p'1",
         "content_path": "purchase.md", "round_key": "2", "lot_key": "A"},
        {"notice_id": 30, "title": "结果公告", "channel": "结果公告", "notice_type": "成交公告", "project_key_name": "p'1",
         "content_path": "result-never-read.md", "revoke_status": "active"},
    ], [{"att_id": 11, "notice_id": 10, "filename": "清单.pdf", "text_path": "purchase.mineru.txt",
         "extract_status": "partial"},
        {"att_id": 31, "notice_id": 30, "filename": "结果.pdf", "text_path": "result-never-read.mineru.txt",
         "extract_status": "ok"}])
    database.projects = [{"project_key": "p'1", "channel": "验收公告"}]
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    opened = []
    original = source._read_source_file
    def read(path, warnings, label):
        opened.append(path)
        assert not path.startswith("result-")
        return original(path, warnings, label)
    monkeypatch.setattr(source, "_read_source_file", read)
    progress = []
    try:
        notices = await source.fetch_project_notices(["p'1"], progress=lambda **values: progress.append(values))
    finally:
        await source.aclose()
    assert [n.notice_id for n in notices] == [10, 30]
    assert notices[0].body == "采购交换机10台"
    assert notices[0].notice_type == "采购公告"
    assert notices[0].project_channel == "验收公告"
    assert notices[1].project_channel == "验收公告"
    project_query = next(q for q, p in database.queries if "FROM public.notices" in q)
    assert "SELECT mp.channel FROM public.mv_project mp" in project_query
    assert "mp.project_key = notices.project_key_name" in project_query
    assert notices[0].attachments[0].text == "交换机技术清单"
    assert notices[0].round_key == "2" and notices[0].lot_key == "A"
    assert notices[1].body == "" and notices[1].attachments == []
    assert not notices[1].material_loaded
    assert notices[1].warnings == []
    query, params = next((q, p) for q, p in database.queries if "FROM public.notices" in q)
    assert "project_key_name = ANY(%s)" in query
    assert "p'1" not in query and params[0] == ["p'1"]
    attachment_params = next(p for q, p in database.queries if "FROM public.attachments" in q)
    assert attachment_params == ([10],)
    assert opened == ["purchase.md", "purchase.mineru.txt"]
    assert any(p.get("phase") == "reading_materials" for p in progress)
    assert progress[-1]["material_processed"] == 2


@pytest.mark.asyncio
async def test_api_procurement_filter_skips_contract_attachment_calls():
    paths = []
    def respond(request):
        paths.append(request.url.path)
        assert not request.url.path.endswith("/text")
        return httpx.Response(200, json={"notice_id": 7, "title": "合同", "channel": "合同公告",
            "project_key_name": "project", "body": "合同全文不参与分析", "round_key": "1",
            "related_notice_id": 3, "attachments": [{"att_id": 8, "extract_status": "ok"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        source = SourceClient(Settings(_env_file=None), client=http)
        notice = (await source.fetch_notice_batch([7], procurement_only=True))[0]
    assert paths == ["/api/v1/admin/notices/7"]
    assert not notice.material_loaded
    assert notice.body == "" and notice.attachments == []
    assert notice.related_notice_id == 3 and notice.round_key == "1"


@pytest.mark.asyncio
async def test_project_lookup_requires_readonly_database_and_files():
    source = SourceClient(Settings(_env_file=None))
    try:
        with pytest.raises(SourceError, match="项目回溯"):
            await source.fetch_project_notices(["p"])
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_project_lookup_fallback_column_and_bound(tmp_path, monkeypatch):
    database = ProjectDatabase([], project_column="project_key")
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        assert await source.fetch_project_notices(["p"]) == []
        query = next(q for q, _ in database.queries if "FROM public.notices" in q)
        assert "project_key = ANY(%s)" in query
        database.notices = [{"notice_id": i + 1, "title": "采购", "channel": "采购公告"}
                            for i in range(25001)]
        with pytest.raises(SourceError, match="25000"):
            await source.fetch_project_notices(["p"])
    finally:
        await source.aclose()
