import httpx
import pytest

from analyzer.config import Settings
from analyzer.source import SourceClient, SourceError


async def test_bare_array_keyword_pages_are_not_mistaken_for_total_count():
    def handler(request):
        page = int(request.url.params["page"])
        if page == 1:
            rows = [{"business_domain": "教育", "business_line": "学校", "word": f"采购词{i}",
                     "kind": "core"} for i in range(500)]
        elif page == 2:
            rows = [{"business_domain": "教育", "business_line": "学校", "word": "最后一个采购词",
                     "kind": "core"}]
        else:
            rows = []
        return httpx.Response(200, json=rows)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SourceClient(Settings(_env_file=None), client=client)
        kb = await source.fetch_knowledge()
    assert len(kb.keywords) == 501
    assert any(kw.word == "最后一个采购词" for kw in kb.keywords)


@pytest.mark.asyncio
async def test_truncated_api_text_is_usable_but_never_cached_as_complete():
    count = []
    def respond(request):
        if request.url.path.endswith("/text"):
            count.append(request.url.path)
            return httpx.Response(200, json={"exists": True, "chars": 5000,
                "text": "采购清单：交换机10台", "truncated": True, "error": None})
        return httpx.Response(200, json={"notice_id": 1, "title": "采购", "body": "详见清单",
            "attachments": [{"att_id": 9, "sha256": "c" * 64, "extract_status": "ok"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        source = SourceClient(settings(), MemoryCache(), http)
        first = await source.fetch_notice(1)
        await source.fetch_notice(1)
    assert first.attachments[0].text == "采购清单：交换机10台"
    assert first.attachments[0].text_truncated
    assert any("截断" in warning for warning in first.warnings)
    assert len(count) == 2


@pytest.mark.parametrize("status", [429, 503])
async def test_temporary_attachment_failure_propagates_to_durable_retry(status):
    def handler(request):
        if request.url.path.endswith("/text"):
            return httpx.Response(status)
        return httpx.Response(200, json={"notice_id": 1, "title": "采购公告", "body": "见技术附件",
            "attachments": [{"att_id": 2, "filename": "技术要求.pdf", "extract_status": "ok"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceError) as caught:
            await SourceClient(Settings(_env_file=None), client=client).fetch_notice(1)
    assert caught.value.retryable


def settings(**kwargs):
    return Settings(_env_file=None, source_base_url="https://source.test", **kwargs)


class MemoryCache:
    def __init__(self):
        self.values = {}

    def get_text(self, key):
        return self.values.get(key)

    def set_text(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_notice_preserves_outer_lots_and_html_paragraphs():
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": {
            "notice": {"notice_id": 7, "title": "学校安防", "body_html": "<p>采购摄像机</p><p>含安装</p>"},
            "lots": [{"lot_no": "1", "lot_name": "设备采购"}], "attachments": [],
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await SourceClient(settings(), client=http).fetch_notice(7)
    assert result.body == "采购摄像机\n含安装"
    assert result.lots[0].lot_name == "设备采购"


@pytest.mark.asyncio
async def test_duplicate_attachment_uses_sha_cache_without_second_http_read():
    requests = []
    sha = "a" * 64

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/text"):
            return httpx.Response(200, text="采购高清摄像机 20 台")
        notice_id = int(request.url.path.split("/")[-1])
        return httpx.Response(200, json={"notice_id": notice_id, "title": "安防", "body": "采购公告",
            "attachments": [{"att_id": notice_id, "filename": "清单.pdf", "sha256": sha,
                             "extract_status": "ok" if notice_id == 1 else "skipped"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        source = SourceClient(settings(), MemoryCache(), http)
        first = await source.fetch_notice(1)
        second = await source.fetch_notice(2)
    assert first.attachments[0].text == second.attachments[0].text == "采购高清摄像机 20 台"
    assert requests == ["/api/v1/admin/notices/1", "/api/v1/admin/attachments/1/text", "/api/v1/admin/notices/2"]
    assert not second.warnings


@pytest.mark.asyncio
async def test_missing_attachment_is_visible_and_not_silent_empty_success():
    def respond(request):
        if request.url.path.endswith("/text"):
            return httpx.Response(404, text="private internal message")
        return httpx.Response(200, json={"notice_id": 1, "title": "采购", "body": "正文",
            "attachments": [{"att_id": 8, "filename": "附件.pdf", "extract_status": "failed"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await SourceClient(settings(), client=http).fetch_notice(1)
    assert result.attachments[0].text == ""
    assert any("8" in warning for warning in result.warnings)
    assert "private internal message" not in str(result.warnings)


@pytest.mark.asyncio
async def test_unrecognized_list_shape_raises_instead_of_empty_page():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"unexpected": [{"notice_id": 1}]}))) as http:
        with pytest.raises(SourceError):
            await SourceClient(settings(), client=http).list_notice_ids(1, 20)


@pytest.mark.asyncio
async def test_list_sends_page_and_auth_and_validates_ids():
    def respond(request):
        assert request.url.params["page"] == "2"
        assert request.url.params["page_size"] == "10"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"data": {"items": [{"notice_id": 10}, {"notice_id": 11}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        ids = await SourceClient(settings(source_api_token="secret"), client=http).list_notice_ids(2, 10)
    assert ids == [10, 11]


@pytest.mark.asyncio
async def test_mismatched_notice_id_is_rejected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"notice_id": 999, "title": "别的公告", "body": "内容"}))) as http:
        with pytest.raises(SourceError):
            await SourceClient(settings(), client=http).fetch_notice(7)


@pytest.mark.asyncio
async def test_response_size_is_bounded_and_error_does_not_echo_content():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, text="secret" * 2000))) as http:
        with pytest.raises(SourceError) as error:
            await SourceClient(settings(source_max_response_bytes=1000), client=http).list_notice_ids(1, 1)
    assert "secret" not in str(error.value)
    assert not error.value.retryable


@pytest.mark.asyncio
async def test_rate_limit_is_retryable_and_sanitized():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(429, text="api-key-secret"))) as http:
        with pytest.raises(SourceError) as error:
            await SourceClient(settings(), client=http).list_notice_ids(1, 1)
    assert error.value.retryable
    assert "api-key-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_knowledge_reads_all_reported_pages():
    def respond(request):
        page = int(request.url.params["page"])
        items = [{"business_domain": "教育教学", "business_line": "基础教育",
                  "word": f"词{page}", "kind": "core"}]
        return httpx.Response(200, json={"items": items, "total": 2, "rubric_version": "v1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        knowledge = await SourceClient(settings(), client=http).fetch_knowledge()
    assert [kw.word for kw in knowledge.keywords] == ["词1", "词2"]
    assert knowledge.quality == "keywords_only"
    assert knowledge.warnings


@pytest.mark.asyncio
async def test_ignored_pagination_is_reported_not_infinite_or_partial():
    data = {"items": [{"business_domain": "教育", "business_line": "学校", "word": "校园", "kind": "scene"}], "total": 704}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=data))) as http:
        with pytest.raises(SourceError, match="分页"):
            await SourceClient(settings(), client=http).fetch_knowledge()


@pytest.mark.asyncio
async def test_keyword_without_domain_requires_import_or_database():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"items": [{"business_line": "学校", "word": "校园", "kind": "scene"}], "total": 1}))) as http:
        with pytest.raises(SourceError, match="业务域"):
            await SourceClient(settings(), client=http).fetch_knowledge()


@pytest.mark.asyncio
async def test_configured_body_field_and_json_attachment_text():
    def respond(request):
        if request.url.path.endswith("/text"):
            return httpx.Response(200, json={"data": {"text": "设备清单"}})
        return httpx.Response(200, json={"data": {"notice_id": 1, "title": "公告",
            "material": {"plain": "公告正文"}, "attachments": [{"att_id": 9}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        notice = await SourceClient(settings(source_body_field="material.plain"), client=http).fetch_notice(1)
    assert notice.body == "公告正文"
    assert notice.attachments[0].text == "设备清单"


@pytest.mark.asyncio
async def test_unknown_configured_body_field_fails_explicitly():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"notice_id": 1, "title": "采购", "body": "正文"}))) as http:
        with pytest.raises(SourceError, match="SOURCE_BODY_FIELD"):
            await SourceClient(settings(source_body_field="bad.field"), client=http).fetch_notice(1)


class DatabaseRows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class ReadOnlyDatabase:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))
        assert query.startswith(("SELECT ", "SET TRANSACTION "))
        if "FROM judge_rubric" in query:
            return DatabaseRows([{"rubric_version": "v3", "weights": {"target": 50},
                                  "thresholds": {"A": 75}, "exceptions": None,
                                  "boundary_cases": None, "negative_signals": None}])
        if "FROM dim_business_line" in query:
            assert params == ("v3",)
            return DatabaseRows([{"business_domain": "教育教学", "business_line": "基础教育",
                                  "body_md": "校园安全设备采购"}])
        if "FROM biz_keyword" in query:
            return DatabaseRows([{"business_line": "基础教育", "word": "校园", "kind": "scene",
                                  "term_family": None, "enabled": True}])
        if "FROM attachments" in query:
            assert params == ("b" * 64, 8)
            return DatabaseRows([{"att_id": 3}])
        return DatabaseRows([])


@pytest.mark.asyncio
async def test_database_knowledge_uses_readonly_snapshot_and_published_version(monkeypatch):
    database = ReadOnlyDatabase()

    def connect(dsn, **kwargs):
        assert dsn == "postgresql://readonly:secret@db/source"
        assert "default_transaction_read_only=on" in kwargs["options"]
        assert "statement_timeout=" in kwargs["options"]
        return database

    monkeypatch.setattr("analyzer.source.psycopg.connect", connect)
    source = SourceClient(settings(source_db_dsn="postgresql://readonly:secret@db/source"))
    try:
        result = await source.fetch_knowledge()
    finally:
        await source.aclose()
    assert result.version == "v3"
    assert result.quality == "full"
    assert result.lines[0].body_md == "校园安全设备采购"
    assert result.source_rubric["weights"] == {"target": 50}
    assert len(database.queries) == 4


@pytest.mark.asyncio
async def test_duplicate_missing_text_falls_back_to_readonly_database_reference(monkeypatch):
    database = ReadOnlyDatabase()
    monkeypatch.setattr("analyzer.source.psycopg.connect", lambda *args, **kwargs: database)

    def respond(request):
        if request.url.path.endswith("/8/text"):
            return httpx.Response(404)
        if request.url.path.endswith("/3/text"):
            return httpx.Response(200, text="同一文件的完整采购清单")
        return httpx.Response(200, json={"notice_id": 1, "title": "采购公告", "body": "正文",
            "attachments": [{"att_id": 8, "sha256": "b" * 64, "extract_status": "skipped"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await SourceClient(settings(source_db_dsn="postgresql://readonly:secret@db/source"),
                                    client=http).fetch_notice(1)
    assert result.attachments[0].text == "同一文件的完整采购清单"
    assert result.attachments[0].extract_status == "ok"
    assert result.warnings == []
