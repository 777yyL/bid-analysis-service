import time

import httpx
from fastapi.testclient import TestClient

from analyzer.app import create_app
from analyzer.config import Settings
from analyzer.llm import JudgeClient
from analyzer.source import SourceClient

KEY = "local-test-key-123456789"
HEADERS = {"X-API-Key": KEY}
KNOWLEDGE = {
    "version": "verified-test-v1", "quality": "full",
    "lines": [{"business_domain": "教育教学", "business_line": "基础教育",
               "body_md": "学校安防建设，包括摄像机采购和视频监控平台。"}],
    "keywords": [{"business_line": "基础教育", "word": "摄像机", "kind": "core"}],
}
NOTICE = {"notice_id": 12, "title": "校园安防采购", "body": "本次采购摄像机和视频监控平台。"}


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'api.db'}",
                    analyzer_api_key=KEY, worker_poll_seconds=0.01, **kwargs)


def wait_job(client, job_id):
    for _ in range(200):
        response = client.get(f"/jobs/{job_id}", headers=HEADERS)
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"ready", "completed", "needs_review", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_api_auth_and_requires_knowledge_before_enqueue(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/jobs").status_code == 401
        response = client.post("/jobs", headers=HEADERS, json={"notice_ids": [1]})
        assert response.status_code == 409
        assert client.post("/knowledge/import", headers=HEADERS, json=KNOWLEDGE).status_code == 200
        assert client.post("/jobs", headers=HEADERS,
                           json={"notice_ids": [1], "mode": "judge"}).status_code == 409


def test_imported_notice_runs_without_source_or_model_and_is_not_final_grade(tmp_path):
    def never_network(request):
        raise AssertionError("prepare must not call source or model for imported notice")
    cfg = settings(tmp_path)
    source = SourceClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(never_network)))
    with TestClient(create_app(cfg, source_client=source)) as client:
        client.post("/knowledge/import", headers=HEADERS, json=KNOWLEDGE)
        response = client.post("/jobs/import", headers=HEADERS, json={"notices": [NOTICE]})
        assert response.status_code == 202
        job = wait_job(client, response.json()["jobs"][0]["id"])
        assert job["status"] == "ready"
        assert job["result"]["prepared"]["candidates"][0]["business_line"] == "基础教育"
        assert job["result"]["judgment"] is None
        assert job["result"]["metrics"]["llm_calls"] == 0
        assert client.get("/metrics", headers=HEADERS).json()["status_counts"]["ready"] == 1


def test_missing_attachment_is_review_not_irrelevant(tmp_path):
    notice = {**NOTICE, "attachments": [{"att_id": 7, "filename": "技术要求.pdf", "text": ""}]}
    with TestClient(create_app(settings(tmp_path))) as client:
        client.post("/knowledge/import", headers=HEADERS, json=KNOWLEDGE)
        response = client.post("/jobs/import", headers=HEADERS, json={"notices": [notice]})
        job = wait_job(client, response.json()["jobs"][0]["id"])
        assert job["status"] == "needs_review"
        assert not job["result"]["prepared"]["material_complete"]
        assert job["result"]["judgment"] is None


def test_incomplete_material_does_not_spend_model_quota(tmp_path):
    cfg = settings(tmp_path, llm_enabled=True, llm_base_url="http://model.test/v1",
                   llm_model="company-model")
    def never_call(request):
        raise AssertionError("missing original attachments must be resolved before paid judgment")
    judge = JudgeClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(never_call)))
    notice = {**NOTICE, "attachments": [{"att_id": 7, "filename": "技术要求.pdf", "text": ""}]}
    with TestClient(create_app(cfg, judge_client=judge)) as client:
        client.post("/knowledge/import", headers=HEADERS, json=KNOWLEDGE)
        response = client.post("/jobs/import", headers=HEADERS,
                               json={"mode": "judge", "notices": [notice]})
        job = wait_job(client, response.json()["jobs"][0]["id"])
        assert job["status"] == "needs_review"
        assert job["result"]["metrics"]["llm_calls"] == 0


def test_source_schema_error_becomes_failed_job_with_safe_error(tmp_path):
    cfg = settings(tmp_path)
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"unexpected": "shape"})
    source = SourceClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(create_app(cfg, source_client=source)) as client:
        client.post("/knowledge/import", headers=HEADERS, json=KNOWLEDGE)
        response = client.post("/jobs", headers=HEADERS, json={"notice_ids": [1]})
        job = wait_job(client, response.json()["jobs"][0]["id"])
        assert job["status"] == "failed"
        assert job["error"]
        assert KEY not in job["error"]


def test_judge_result_is_cached_but_new_material_is_not(tmp_path):
    calls = []
    cfg = settings(tmp_path, llm_enabled=True, llm_base_url="http://model.test/v1",
                   llm_model="company-model", llm_rpm=6000)
    def handler(request):
        import json
        calls.append(request)
        content = json.loads(request.content)
        # Real prompt carries evidence IDs. The fixture chooses the submitted original snippet.
        user = content["messages"][-1]["content"]
        state = json.loads(user)
        ids = [item["id"] for item in state["evidence"]]
        answer = {"decisions": [{"business_domain": "教育教学", "business_line": "基础教育",
                  "target_score": 50, "scene_score": 20, "buyer_score": 10,
                  "tech_families": [], "negative_signals": [], "evidence_ids": ids[:1],
                  "reason": "采购内容包含摄像机。"}]}
        return httpx.Response(200, json={"model": "company-model", "choices": [{
            "message": {"content": json.dumps(answer, ensure_ascii=False)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100}})
    judge = JudgeClient(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(create_app(cfg, judge_client=judge)) as client:
        client.post("/knowledge/import", headers=HEADERS, json=KNOWLEDGE)
        def submit(notice):
            response = client.post("/jobs/import", headers=HEADERS,
                                   json={"mode": "judge", "notices": [notice]})
            assert response.status_code == 202
            return wait_job(client, response.json()["jobs"][0]["id"])
        first = submit(NOTICE)
        assert first["status"] == "completed", first
        assert first["result"]["judgment"]["decisions"][0]["score"] == 80
        again = submit(NOTICE)
        assert again["result"]["cache_hit"]
        assert again["result"]["metrics"]["llm_calls"] == 0
        assert len(calls) == 1
        changed = submit({**NOTICE, "body": "本次采购摄像机，并新增门禁设备。"})
        assert not changed["result"]["cache_hit"]
        assert len(calls) == 2
