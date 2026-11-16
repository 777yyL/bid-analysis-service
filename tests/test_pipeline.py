import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import update

from analyzer.config import Settings
from analyzer.llm import JudgeClient
from analyzer.pipeline import Pipeline
from analyzer.schemas import KnowledgeInput, NoticeInput
from analyzer.store import Store
from analyzer.workspace import TaskRow, Workspace


@pytest.mark.asyncio
async def test_parallel_project_reviews_do_not_repeat_completed_tasks(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'parallel.db'}",
        llm_enabled=True, llm_model="test", llm_base_url="https://model/v1", llm_rpm=1000000,
        llm_concurrency=3)
    entered, release, calls = 0, asyncio.Event(), []
    async def respond(request):
        nonlocal entered
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(state["title"])
        entered += 1
        if entered == 3:
            release.set()
        await asyncio.wait_for(release.wait(), 3)
        body = {"tags": [{**tag, "status": "confirmed", "reason": "采购清单", "evidence_ids": ["e1"]}
                         for tag in state["rule_tags"]], "needs_more": False}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(body)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        store = Store(cfg.database_url)
        store.initialize()
        ws = Workspace(store)
        kb = KnowledgeInput.model_validate({"version": "v1", "product_keywords": [
            {"category": "网络产品", "word": "交换机"}]})
        key = store.save_knowledge(kb)
        pipeline = Pipeline(cfg, ws, None, JudgeClient(cfg, http))
        for nid in range(1, 4):
            await pipeline.project_flow.ingest([NoticeInput(notice_id=nid, title=f"采购交换机{nid}",
                body="采购交换机10台", notice_type="采购公告", project_key=f"p{nid}")], key, True)
            await pipeline.project_flow.reanalyze(f"p{nid}", True, force=True, backfill=False)
            assert await pipeline.run_once("rule")
        assert all(await asyncio.gather(*(pipeline.run_once("model") for _ in range(3))))
        assert len(set(calls)) == 3
        assert all(t["status"] == "completed" for t in ws.tasks(kind="project_model")["items"])
        assert not await pipeline.run_once("model")
        assert len(calls) == 3
        store.close()


@pytest.mark.asyncio
async def test_shared_request_gate_limits_concurrency_and_rpm():
    import time
    active, peak, started = 0, 0, []
    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(time.monotonic())
        try:
            await asyncio.sleep(0.15)
            return httpx.Response(200, json={})
        finally:
            active -= 1
    cfg = Settings(_env_file=None, llm_base_url="https://model/v1", llm_concurrency=2, llm_rpm=600)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        judge = JudgeClient(cfg, http)
        await asyncio.gather(*(judge.post_model({}, {}, 10) for _ in range(5)))
    assert peak == 2
    assert len(started) == 5
    assert all(b - a >= 0.09 for a, b in zip(started, started[1:]))


@pytest.mark.asyncio
async def test_model_queue_confirms_products_and_cache_retains_manual_override(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'pipeline.db'}",
                   llm_enabled=True, llm_base_url="https://model/v1", llm_model="test", llm_rpm=1000000)
    kb = KnowledgeInput.model_validate({"version": "products", "product_keywords": [
        {"category": "网络产品", "word": "交换机"}]})
    calls = []
    def respond(request):
        calls.append(request)
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        content = {"tags": [{**tag, "status": "confirmed", "reason": "采购清单",
                             "evidence_ids": ["e1"]} for tag in state["rule_tags"]], "needs_more": False}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(content)}}], "usage": {"prompt_tokens": 50}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    judge = JudgeClient(cfg, client)
    store = Store(cfg.database_url)
    store.initialize()
    ws = Workspace(store)
    key = store.save_knowledge(kb)
    notice = NoticeInput(notice_id=10, title="采购交换机", body="采购交换机10台")
    ws.ingest(notice, key, True)
    pipeline = Pipeline(cfg, ws, None, judge)
    assert await pipeline.run_once("rule")
    assert ws.detail(10)["rule_tags"][0]["status"] == "confirmed"
    assert await pipeline.run_once("model")
    assert ws.detail(10)["model_tags"][0]["status"] == "confirmed"
    ws.review(10, {"dimension": "product", "category": "网络产品", "label": "交换机",
                   "status": "excluded", "reason": "人工排除"})
    ws.enqueue(10, key, "model")
    assert await pipeline.run_once("model")
    assert len(calls) == 1
    assert ws.detail(10)["tags"][0]["source"] == "manual"
    assert ws.overview()["metrics"]["llm_calls"] == 1
    store.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_waiting_for_rules_does_not_exhaust_model_attempts(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'waiting.db'}",
                   llm_enabled=True, llm_model="test", llm_base_url="https://model/v1")
    store = Store(cfg.database_url)
    store.initialize()
    ws = Workspace(store)
    key = store.save_knowledge(KnowledgeInput(version="v1"))
    notice = NoticeInput(notice_id=8, title="采购", body="正文")
    ws.ingest(notice, key, True)
    ws.ingest(notice, key, True)
    pipeline = Pipeline(cfg, ws, None, None)
    for _ in range(5):
        with store.session.begin() as s:
            s.execute(update(TaskRow).where(TaskRow.kind == "model").values(available_at=0))
        assert await pipeline.run_once("model")
    tasks = ws.tasks(kind="model")["items"]
    assert tasks[0]["status"] == "retry"
    assert tasks[0]["attempts"] == 0
    store.close()


@pytest.mark.asyncio
async def test_incremental_sweep_preserves_cursor_and_freezes_lower_bound(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'sync.db'}")
    store = Store(cfg.database_url)
    store.initialize()
    ws = Workspace(store)
    store.save_knowledge(KnowledgeInput(version="v1"))
    calls = []
    class Source:
        async def list_notice_batch(self, after_id, limit, updated_since):
            assert ws.state("sync_progress:incremental")["phase"] == "reading_source"
            calls.append((after_id, updated_since))
            return [NoticeInput(notice_id=12, title="采购", body="正文")] if not after_id else []
    pipeline = Pipeline(cfg, ws, Source(), None)
    await pipeline.sync_batch("incremental")
    await pipeline.sync_batch("incremental")
    assert calls[0][0] == 0
    assert calls[1][0] == 12
    assert calls[0][1] == calls[1][1]
    assert ws.state("sync:incremental")["cursor"] == 0
    assert ws.state("sync_progress:incremental")["phase"] == "idle"
    assert ws.state("sync_progress:incremental")["last_count"] == 0
    store.close()


@pytest.mark.asyncio
async def test_reindex_transient_failure_recovers_and_clears_error(tmp_path, monkeypatch):
    import analyzer.pipeline as pipeline_module
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'recovery.db'}")
    store = Store(cfg.database_url)
    store.initialize()
    ws = Workspace(store)
    key = store.save_knowledge(KnowledgeInput(version="v1"))
    ws.set_state("reindex", {"knowledge_id": key, "cursor": 11, "enabled": True})
    clock, calls, sleeps = [1000.0], [], []
    original = ws.reindex_batch
    def reindex(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("temporary database outage")
        return original(*args)
    async def sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) == 1:
            assert ws.state("reindex")["cursor"] == 11
            assert ws.state("sync_error:reindex")
            clock[0] += 31
        else:
            raise asyncio.CancelledError
    monkeypatch.setattr(ws, "reindex_batch", reindex)
    monkeypatch.setattr(pipeline_module, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(pipeline_module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await Pipeline(cfg, ws, None, None).sync_loop()
    assert len(calls) == 2
    assert not ws.state("reindex")["enabled"]
    assert ws.state("sync_error:reindex") is None
    store.close()


@pytest.mark.asyncio
async def test_history_loop_uses_persisted_batch_size(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'batch.db'}")
    store = Store(cfg.database_url)
    store.initialize()
    ws = Workspace(store)
    store.save_knowledge(KnowledgeInput(version="v1"))
    ws.set_state("history_enabled", True)
    ws.set_state("history_page_size", 7)
    calls = []
    class Source:
        async def list_notice_batch(self, cursor, limit, since):
            calls.append(limit)
            return []
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr("analyzer.pipeline.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await Pipeline(cfg, ws, Source(), None)._sync_loop()
    assert calls == [7]
    assert ws.state("sync:history")["complete"]
    store.close()
