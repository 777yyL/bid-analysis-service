import json

import httpx
import pytest

from analyzer.config import Settings
from analyzer.llm import JudgeClient, JudgeError
from analyzer.schemas import KnowledgeInput, NoticeInput
from analyzer.tag_model import TagJudge


def test_later_correction_overrides_main_and_cross_notice_evidence_is_distinct():
    from analyzer.tag_model import finish_judgments, merge_judgments
    notice = NoticeInput(notice_id=1, title="采购", analysis_scope="project", analysis_documents=[
        {"notice_id": 2, "notice_type": "更正公告", "source": "body", "text": "删除交换机"}])
    def tag(nid, status):
        return {"dimension": "product", "category": "网络", "label": "交换机", "status": status,
                "reason": status, "evidence": [{"notice_id": nid, "source": "body", "start": 0, "end": 10}]}
    merged = {}
    merge_judgments(merged, tag(1, "confirmed"), notice)
    merge_judgments(merged, tag(2, "excluded"), notice)
    merge_judgments(merged, tag(1, "confirmed"), notice)
    assert not finish_judgments(merged)
    assert next(iter(merged.values()))["status"] == "excluded"
    merged = {}
    merge_judgments(merged, tag(1, "confirmed"), notice)
    merge_judgments(merged, tag(3, "confirmed"), notice)
    assert not finish_judgments(merged)
    assert len(next(iter(merged.values()))["evidence"]) == 2


@pytest.mark.asyncio
async def test_product_only_model_and_unknown_evidence_rejected():
    knowledge = KnowledgeInput.model_validate({"version": "v1", "lines": [],
        "product_keywords": [{"category": "网络产品", "word": "交换机"}]})
    notice = NoticeInput(notice_id=1, title="交换机采购", body="采购交换机10台")
    response_data = {"tags": [{"dimension": "product", "category": "网络产品", "label": "交换机",
        "status": "confirmed", "reason": "本次采购", "evidence_ids": ["invented"]}], "needs_more": False}
    def respond(request):
        payload = json.loads(request.content)
        assert "products" in payload["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(response_data, ensure_ascii=False)}}], "usage": {"prompt_tokens": 20}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    judge = JudgeClient(Settings(_env_file=None, llm_enabled=True, llm_base_url="https://model/v1",
                                 llm_model="test", llm_rpm=1000000), client)
    with pytest.raises(JudgeError, match="证据"):
        await TagJudge(judge).judge(notice, knowledge, [])
    response_data["tags"][0]["evidence_ids"] = ["e1"]
    result = await TagJudge(judge).judge(notice, knowledge, [])
    assert result["tags"][0]["source"] == "model"
    assert result["tags"][0]["evidence"][0]["source"] == "title"
    assert result["metrics"]["input_tokens"] == 20
    await client.aclose()


@pytest.mark.asyncio
async def test_many_candidates_use_output_sized_batches():
    kb = KnowledgeInput.model_validate({"version": "many", "product_keywords": [
        {"category": "产品", "word": f"设备{i}"} for i in range(30)]})
    notice = NoticeInput(notice_id=3, title="设备采购", body="采购设备清单")
    tags = [{"dimension": "product", "category": "产品", "label": f"设备{i}", "status": "candidate"}
            for i in range(30)]
    batch_sizes = []
    def respond(request):
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        batch_sizes.append(len(state["rule_tags"]))
        data = {"needs_more": False, "tags": [{**t, "reason": "清单", "status": "confirmed",
                "evidence_ids": ["e1"]} for t in state["rule_tags"]]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(data)}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    judge = JudgeClient(Settings(_env_file=None, llm_enabled=True, llm_base_url="https://model/v1",
                                 llm_model="test", llm_rpm=1000000), client)
    result = await TagJudge(judge).judge_all(notice, kb, tags)
    assert len(result["tags"]) == 30
    assert max(batch_sizes) <= 13
    assert result["metrics"]["candidate_batch_count"] == 3
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_failure", [False, True])
async def test_later_batch_conflict_or_failure_preserves_results_and_accounting(provider_failure):
    kb = KnowledgeInput.model_validate({"version": "batches", "product_keywords": [
        {"category": "产品", "word": label} for label in ("设备甲", "设备乙", "设备丙")]})
    tags = [{"dimension": "product", "category": "产品", "label": p.word, "status": "candidate"}
            for p in kb.product_keywords]
    notice = NoticeInput(notice_id=5, title="采购设备甲乙丙", body="采购清单")
    calls = []
    def respond(request):
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(state)
        if len(calls) == 2 and provider_failure:
            return httpx.Response(429)
        decisions = [{**t, "status": "confirmed", "reason": "采购清单", "evidence_ids": ["e1"]}
                     for t in state["rule_tags"]]
        if len(calls) == 2:
            decisions.append({**tags[0], "status": "excluded", "reason": "后续否定",
                              "evidence_ids": ["e1"]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"tags": decisions, "needs_more": False})}}],
            "usage": {"prompt_tokens": 10}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    judge = JudgeClient(Settings(_env_file=None, llm_enabled=True, llm_base_url="https://model/v1",
                                 llm_model="test", llm_rpm=1000000, llm_max_output_tokens=512), client)
    if provider_failure:
        with pytest.raises(JudgeError) as error:
            await TagJudge(judge).judge_all(notice, kb, tags)
        assert error.value.retryable
        assert error.value.request_count == 2
        assert error.value.usage["prompt_tokens"] == 10
    else:
        result = await TagJudge(judge).judge_all(notice, kb, tags)
        assert next(t for t in result["tags"] if t["label"] == "设备甲")["status"] == "candidate"
        assert result["needs_more"]
        assert result["metrics"]["llm_calls"] == 2
        assert result["metrics"]["input_tokens"] == 20
    await client.aclose()


@pytest.mark.asyncio
async def test_long_material_continues_into_later_fragments():
    kb = KnowledgeInput.model_validate({"version": "v1", "product_keywords": [
        {"category": "网络产品", "word": "交换机"}]})
    notice = NoticeInput(notice_id=2, title="设备采购",
                         body="".join(f"第{i}项普通说明。" for i in range(900)) + "采购交换机10台。")
    calls = []
    def respond(request):
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(state)
        hit = next((e for e in state["evidence"] if "交换机10台" in e["text"]), None)
        data = {"needs_more": False, "tags": [] if hit is None else [{"dimension": "product",
            "category": "网络产品", "label": "交换机", "status": "confirmed", "reason": "采购需求",
            "evidence_ids": [hit["id"]]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(data)}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    judge = JudgeClient(Settings(_env_file=None, llm_enabled=True, llm_base_url="https://model/v1",
                                 llm_model="test", llm_rpm=1000000, llm_max_input_chars=2000), client)
    result = await TagJudge(judge).judge_all(notice, kb, [])
    assert len(calls) > 1
    assert result["tags"][0]["status"] == "confirmed"
    assert not result["needs_more"]
    assert result["metrics"]["llm_calls"] == len(calls)
    assert result["metrics"]["fragment_count"] == len(calls)
    await client.aclose()
