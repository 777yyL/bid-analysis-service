import json

import httpx
import pytest

from analyzer.config import Settings
from analyzer.engine import prepare
from analyzer.llm import JudgeClient, JudgeError
from analyzer.schemas import TECH_FAMILIES, BusinessLine, Keyword, KnowledgeInput, NoticeInput


def inputs(**config):
    settings = Settings(_env_file=None, llm_enabled=True, llm_base_url="https://model.test/v1",
                        llm_model="company-model", llm_rpm=100000, **config)
    kb = KnowledgeInput(version="v1", lines=[BusinessLine(
        business_domain="教育", business_line="学校", body_md="校园安防设备与平台建设",
    )], keywords=[Keyword(business_line="学校", word="安防")])
    prepared = prepare(NoticeInput(notice_id=1, title="校园安防", body="采购安防设备与平台"), kb,
                       settings)
    decision = {
        "business_domain": "教育", "business_line": "学校", "target_score": 45,
        "scene_score": 20, "buyer_score": 10, "tech_families": list(TECH_FAMILIES),
        "negative_signals": ["仅设备采购"], "evidence_ids": [prepared.evidence[0].id],
        "reason": "原文明确采购安防设备。",
    }
    return settings, kb, prepared, decision


def response(decisions, **extra):
    return httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"decisions": decisions}, ensure_ascii=False),
        }}], "usage": {"prompt_tokens": 100, "completion_tokens": 40}, **extra,
    })


async def test_scores_are_computed_in_code_and_request_contains_candidate_evidence():
    settings, kb, prepared, decision = inputs()
    sent = []
    def handle(request):
        sent.append(json.loads(request.content))
        assert str(request.url) == "https://model.test/v1/chat/completions"
        return response([decision])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await JudgeClient(settings, client=http).judge(prepared, kb)
    assert result.decisions[0].score == 85
    assert result.decisions[0].level == "A"
    assert result.usage["prompt_tokens"] == 100
    assert prepared.evidence[0].id in sent[0]["messages"][1]["content"]


@pytest.mark.parametrize("mutation", [
    {"target_score": True}, {"target_score": 51}, {"business_line": "医院"},
    {"evidence_ids": ["invented"]}, {"tech_families": ["unknown"]},
])
async def test_rejects_untrusted_model_values(mutation):
    settings, kb, prepared, decision = inputs()
    decision.update(mutation)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response([decision]))) as http:
        with pytest.raises(JudgeError) as caught:
            await JudgeClient(settings, client=http).judge(prepared, kb)
    assert not caught.value.retryable
    assert caught.value.request_count == 1


async def test_rejects_missing_and_duplicate_candidate_outputs():
    settings, kb, prepared, decision = inputs()
    for decisions in ([], [decision, decision]):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(decisions))) as http:
            with pytest.raises(JudgeError):
                await JudgeClient(settings, client=http).judge(prepared, kb)


async def test_429_is_returned_without_internal_retry_and_never_leaks_server_body():
    settings, kb, prepared, _ = inputs()
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "17"}, text="secret upstream text")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(JudgeError) as caught:
            await JudgeClient(settings, client=http).judge(prepared, kb)
    assert len(calls) == 1
    assert caught.value.retryable
    assert caught.value.retry_after == 17
    assert "secret" not in str(caught.value)


async def test_oversized_input_fails_before_calling_provider():
    settings, kb, prepared, _ = inputs(llm_max_input_chars=2000)
    prepared.evidence[0].text = "证据" * 3000
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: pytest.fail("Oversized request must not reach provider"),
    )) as http:
        with pytest.raises(JudgeError) as caught:
            await JudgeClient(settings, client=http).judge(prepared, kb)
    assert caught.value.request_count == 0


async def test_truncated_model_output_is_not_accepted_as_complete():
    settings, kb, prepared, decision = inputs()
    reply = response([decision])
    payload = reply.json()
    payload["choices"][0]["finish_reason"] = "length"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload),
    )) as http:
        with pytest.raises(JudgeError):
            await JudgeClient(settings, client=http).judge(prepared, kb)


async def test_tpm_smaller_than_single_request_is_explicit_failure():
    settings, kb, prepared, _ = inputs(llm_tpm=100)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: pytest.fail("Impossible TPM reservation must fail before network"),
    )) as http:
        with pytest.raises(JudgeError) as caught:
            await JudgeClient(settings, client=http).judge(prepared, kb)
    assert not caught.value.retryable
    assert caught.value.request_count == 0
