import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from analyzer.sales_models import ModelError, RuntimeSettings, SalesModelClient


def settings(**values):
    return RuntimeSettings(embedding_base_url="https://example/v1/embeddings", embedding_model="embed",
                           embedding_dimensions=2, **values)


def test_settings_validate_thresholds_and_urls():
    with pytest.raises(ValidationError):
        RuntimeSettings(candidate_threshold=.9, confirm_threshold=.8)
    with pytest.raises(ValidationError):
        RuntimeSettings(embedding_base_url="https://user:secret@example/v1")


@pytest.mark.asyncio
async def test_embeddings_preserve_order_and_input_contract():
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        assert str(request.url) == "https://example/v1/embeddings"
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SalesModelClient(settings(embedding_query_prefix="Q:"), http)
        result = await client.embed_sales([{"id": "1", "name": "访客", "description": "预约"},
                                           {"id": "2", "name": "视频", "description": "回放"}])
    assert result == [[1, 0], [0, 1]]
    assert requests[0]["input"] == ["Q:销售项：访客\n能力描述：预约", "Q:销售项：视频\n能力描述：回放"]
    assert set(requests[0]) == {"model", "input"}


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], [{"index": 0, "embedding": [0, 0]}],
    [{"index": 0, "embedding": [1]}], [{"index": 1, "embedding": [1, 0]}],
    [{"index": 0, "embedding": [True, 1]}]])
async def test_invalid_vectors_rejected(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": data}))) as http:
        with pytest.raises(ModelError, match="向量结果"):
            await SalesModelClient(settings(), http).embed_text("采购访客软件")


@pytest.mark.asyncio
async def test_review_validates_candidates_and_evidence():
    result = {"items": [{"sales_item_id": "1", "status": "confirmed", "reason": "采购预约能力",
                        "evidence_keys": ["1:2026-11-02"]}]}
    def respond(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(result)}}]})
    cfg = settings(review_enabled=True, review_base_url="https://example/v1", review_model="judge",
                   review_rpm=1000000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SalesModelClient(cfg, http)
        args = ([{"id": "1", "name": "访客"}], [{"key": "1:2026-11-02", "content": "采购访客预约系统"}])
        assert (await client.review(*args))[0]["status"] == "confirmed"
        result["items"][0]["evidence_keys"] = ["invented"]
        with pytest.raises(ModelError, match="证据校验"):
            await client.review(*args)
        result["items"] = []
        with pytest.raises(ModelError, match="证据校验"):
            await client.review(*args)


@pytest.mark.asyncio
async def test_embedding_requests_bounded_and_errors_sanitized():
    active, peak = 0, 0
    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(.02)
        active -= 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SalesModelClient(settings(embedding_concurrency=2), http)
        await asyncio.gather(*(client.embed_text("访客") for _ in range(5)))
        assert peak == 2
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, text="sensitive", headers={"retry-after": "2"}))) as http:
        with pytest.raises(ModelError) as error:
            await SalesModelClient(settings(), http).embed_text("访客")
        assert error.value.retryable and error.value.retry_after == 2
        assert "sensitive" not in str(error.value)
def test_default_matching_threshold_is_conservative_with_empty_prefixes():
    from analyzer.sales_models import RuntimeSettings

    cfg = RuntimeSettings()
    assert cfg.candidate_threshold == .80
    assert cfg.confirm_threshold == .85
    assert cfg.embedding_query_prefix == cfg.embedding_document_prefix == ''
