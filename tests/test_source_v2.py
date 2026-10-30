from types import SimpleNamespace

import httpx
import pytest

from analyzer.config import Settings
from analyzer.source import SourceClient, SourceError


def cfg(root):
    base = Settings(_env_file=None, source_db_dsn="postgresql://readonly@db/source")
    return SimpleNamespace(**{**base.model_dump(), "source_file_root": str(root)})


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Database:
    def __init__(self, notices, attachments=(), updated=True):
        self.notices, self.attachments, self.updated = notices, attachments, updated
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        self.queries.append((query, params))
        assert query.startswith(("SELECT ", "SET TRANSACTION "))
        if "information_schema.columns" in query:
            columns = {
                "notices": ["notice_id", "title", "buyer", "channel", "project_key_name",
                            "content_path", "scope_summary", "revoke_status"],
                "attachments": ["att_id", "notice_id", "filename", "sha256", "text_path",
                                "extract_status", "summary"],
                "lots": ["notice_id", "lot_no", "lot_name", "description"],
            }
            if self.updated:
                columns["notices"].append("updated_at")
            return Rows([{"table_name": table, "column_name": column}
                         for table, values in columns.items() for column in values])
        if "FROM public.notices" in query:
            return Rows(self.notices)
        if "FROM public.attachments" in query:
            return Rows(self.attachments)
        return Rows([])


@pytest.mark.asyncio
async def test_batch_reads_md_and_metadata_without_http(tmp_path, monkeypatch):
    (tmp_path / "body.md").write_text("采购交换机", encoding="utf-8")
    (tmp_path / "att.md").write_text("交换机 20 台", encoding="utf-8")
    database = Database([{"notice_id": 8, "title": "设备采购", "buyer": "大学",
                          "channel": "采购公告", "project_key_name": "p1", "content_path": "body.md"}],
                        [{"att_id": 9, "notice_id": 8, "filename": "清单.pdf", "text_path": "att.md",
                          "extract_status": "ok", "sha256": "a" * 64}])
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("HTTP forbidden"))) as http:
        source = SourceClient(cfg(tmp_path), client=http)
        result = await source.fetch_notice_batch([8])
    assert result[0].body == "采购交换机"
    assert result[0].notice_type == "采购公告"
    assert result[0].project_key == "p1"
    assert result[0].attachments[0].text == "交换机 20 台"
    assert len(database.queries) <= 6


@pytest.mark.asyncio
async def test_file_outside_root_is_never_read(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-source-secret.md"
    outside.write_text("secret", encoding="utf-8")
    database = Database([{"notice_id": 1, "title": "采购", "content_path": "../outside-source-secret.md"}])
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        result = await source.fetch_notice_batch([1])
    finally:
        await source.aclose()
    assert result[0].body == ""
    assert result[0].warnings
    assert "secret" not in str(result[0].warnings)


@pytest.mark.asyncio
async def test_incremental_requires_real_update_column(tmp_path, monkeypatch):
    database = Database([], updated=False)
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        with pytest.raises(SourceError, match="更新时间"):
            await source.list_notice_batch(updated_since=1000)
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_missing_batch_notice_is_explicit(tmp_path, monkeypatch):
    database = Database([])
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        with pytest.raises(SourceError, match="不存在"):
            await source.fetch_notice_batch([99])
    finally:
        await source.aclose()


@pytest.mark.asyncio
async def test_skipped_duplicate_reuses_readable_sha_copy(tmp_path, monkeypatch):
    (tmp_path / "body.md").write_text("采购公告", encoding="utf-8")
    (tmp_path / "duplicate.md").write_text("采购摄像机 20 台", encoding="utf-8")

    class DuplicateDatabase(Database):
        def execute(self, query, params=None):
            if "sha256 = ANY" in query:
                self.queries.append((query, params))
                return Rows([{"att_id": 2, "notice_id": 3, "text_path": "duplicate.md",
                              "sha256": "a" * 64, "extract_status": "ok"}])
            return super().execute(query, params)

    database = DuplicateDatabase(
        [{"notice_id": 1, "title": "采购", "content_path": "body.md"}],
        [{"att_id": 9, "notice_id": 1, "text_path": None, "extract_status": "skipped",
          "sha256": "a" * 64}],
    )
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        result = await source.fetch_notice_batch([1])
    finally:
        await source.aclose()
    assert result[0].attachments[0].text == "采购摄像机 20 台"
    assert result[0].attachments[0].extract_status == "ok"
    assert not result[0].warnings


@pytest.mark.asyncio
async def test_mineru_text_is_read_even_when_extraction_not_ok(tmp_path, monkeypatch):
    from analyzer.schemas import KnowledgeInput, ProductKeyword
    from analyzer.tagging import analyze_tags
    path = tmp_path / "attachments/2026/10/5d/hash/中小企业声明函.png.mineru.txt"
    path.parent.mkdir(parents=True)
    path.write_text("采购清单：交换机10台。", encoding="utf-8")
    (tmp_path / "body.md").write_text("本次采购见附件。", encoding="utf-8")
    database = Database([{"notice_id": 1, "title": "采购", "content_path": "body.md"}],
        [{"att_id": 9, "notice_id": 1, "text_path": path.relative_to(tmp_path).as_posix(),
          "extract_status": "partial", "sha256": "a" * 64}])
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        notice = (await source.fetch_notice_batch([1]))[0]
    finally:
        await source.aclose()
    assert notice.attachments[0].text == "采购清单：交换机10台。"
    assert notice.attachments[0].extract_status == "partial"
    result = analyze_tags(notice, KnowledgeInput(version="v1", product_keywords=[
        ProductKeyword(category="网络产品", word="交换机")]))
    assert any(t["label"] == "交换机" for t in result["tags"])
    assert not result["material_complete"]


@pytest.mark.asyncio
async def test_missing_ok_file_can_reuse_partial_sha_copy(tmp_path, monkeypatch):
    (tmp_path / "body.md").write_text("采购公告", encoding="utf-8")
    (tmp_path / "copy.mineru.txt").write_text("采购摄像机20台", encoding="utf-8")
    class Copies(Database):
        def execute(self, query, params=None):
            if "sha256 = ANY" in query:
                assert "AND extract_status = 'ok'" not in query
                return Rows([{"sha256": "a" * 64, "text_path": "copy.mineru.txt",
                              "extract_status": "partial"}])
            return super().execute(query, params)
    database = Copies([{"notice_id": 1, "title": "采购", "content_path": "body.md"}],
        [{"att_id": 9, "notice_id": 1, "sha256": "a" * 64,
          "text_path": "missing.txt", "extract_status": "ok"}])
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *a, **kw: database)
    source = SourceClient(cfg(tmp_path))
    try:
        notice = (await source.fetch_notice_batch([1]))[0]
    finally:
        await source.aclose()
    assert notice.attachments[0].text == "采购摄像机20台"
    assert notice.attachments[0].extract_status == "partial"
    assert notice.warnings
