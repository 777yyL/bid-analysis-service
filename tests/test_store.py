from analyzer.schemas import BusinessLine, KnowledgeInput, NoticeInput
from analyzer.store import Store


def knowledge():
    return KnowledgeInput(version="test-v1", lines=[BusinessLine(
        business_domain="教育", business_line="学校", body_md="学校安防设备采购"
    )])


def test_freezes_knowledge_and_deduplicates_only_active_jobs(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'jobs.db'}")
    store.initialize()
    kid = store.save_knowledge(knowledge())
    first = store.enqueue(10, kid, "prepare")
    assert store.enqueue(10, kid, "prepare")["id"] == first["id"]
    assert store.enqueue(10, kid, "judge")["id"] != first["id"]
    claimed = store.claim(now=100000000000)
    assert claimed["knowledge_id"] == kid
    assert store.complete(claimed, "ready", {"prepared": {}}, now=100000000001)
    assert store.enqueue(10, kid, "prepare")["id"] != first["id"]
    store.close()


def test_expired_lease_recovers_job_and_rejects_old_worker(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'jobs.db'}")
    store.initialize()
    kid = store.save_knowledge(knowledge())
    job = store.enqueue(10, kid, "prepare")
    old = store.claim(now=100000000000, lease_seconds=10)
    assert store.claim(now=100000000005) is None
    new = store.claim(now=100000000011)
    assert new["id"] == job["id"]
    assert new["attempts"] == 2
    assert not store.complete(old, "ready", {"stale": True})
    assert store.complete(new, "ready", {"correct": True})
    assert store.get_job(job["id"])["result"] == {"correct": True}
    store.close()


def test_retries_are_delayed_and_history_is_preserved(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'jobs.db'}")
    store.initialize()
    kid = store.save_knowledge(knowledge())
    item = store.enqueue(10, kid, "judge")
    job = store.claim(now=100000000000)
    store.fail(job, "model rate limit", retry_delay=60, metrics={"llm_calls": 1}, now=100000000001)
    assert store.claim(now=100000000020) is None
    retry = store.claim(now=100000000062)
    assert retry["attempts"] == 2
    store.fail(retry, "invalid response", retry_delay=None, metrics={"llm_calls": 1})
    result = store.get_job(item["id"])
    assert result["status"] == "failed"
    assert len(result["attempt_history"]) == 2
    assert result["attempt_history"][0]["metrics"]["llm_calls"] == 1
    store.close()


def test_caches_survive_restart_and_import_changes_create_new_jobs(tmp_path):
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    store = Store(url)
    store.initialize()
    kid = store.save_knowledge(knowledge())
    a = NoticeInput(notice_id=10, title="校园建设", body="摄像机采购")
    b = a.model_copy(update={"body": "教学软件采购"})
    assert store.enqueue(10, kid, "prepare", a.model_dump())["id"] != store.enqueue(
        10, kid, "prepare", b.model_dump())["id"]
    store.set_text("attachment-sha-v1", "采购技术要求")
    job = store.claim()
    store.complete(job, "completed", {"ok": True}, cache_key="fp", cache_value={"score": 75})
    store.close()
    again = Store(url)
    again.initialize()
    assert again.get_text("attachment-sha-v1") == "采购技术要求"
    assert again.get_cached_result("fp") == {"score": 75}
    assert again.get_knowledge(kid).version == "test-v1"
    again.close()
