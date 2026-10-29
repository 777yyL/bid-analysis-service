
import pytest

from analyzer.schemas import NoticeInput
from analyzer.store import Store
from analyzer.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'v2.db'}")
    store.initialize()
    ws = Workspace(store)
    yield ws
    store.close()


def test_manual_decision_survives_rerun_and_filters_are_effective(workspace):
    n = NoticeInput(notice_id=1, title="交换机采购", body="采购交换机10台")
    workspace.ingest(n, "k1", use_model=True)
    tag = dict(dimension="product", category="网络产品", label="交换机",
               status="confirmed", source="rule", reason="采购清单", evidence=[])
    workspace.save_rules(1, "k1", [tag], True, [])
    workspace.review(1, {**tag, "status": "excluded", "reason": "人工确认非采购"})
    workspace.save_rules(1, "k1", [tag], True, [])
    detail = workspace.detail(1)
    assert detail["tags"][0]["source"] == "manual"
    assert detail["tags"][0]["status"] == "excluded"
    assert workspace.notices(tag_status="confirmed")["total"] == 0
    assert workspace.notices(tag_status="excluded")["total"] == 1
    assert len(detail["history"]) == 1


def test_changed_material_rejects_old_model_task(workspace):
    workspace.ingest(NoticeInput(notice_id=2, title="一期", body="原文"), "k", True)
    old = workspace.claim("rule")
    workspace.ingest(NoticeInput(notice_id=2, title="二期", body="更正原文"), "k", True)
    assert not workspace.task_is_current(old)
    assert workspace.detail(2)["notice"]["body"] == "更正原文"


def test_model_queue_does_not_block_rule_queue(workspace):
    workspace.ingest(NoticeInput(notice_id=3, title="采购", body="正文"), "k", True)
    rule = workspace.claim("rule")
    workspace.finish_task(rule, "completed")
    workspace.enqueue(3, "k", "model", priority=80)
    model = workspace.claim("model")
    assert model["kind"] == "model"
    workspace.ingest(NoticeInput(notice_id=4, title="采购2", body="正文2"), "k", True)
    assert workspace.claim("rule")["notice_id"] == 4


def test_identical_ingest_is_idempotent_but_knowledge_change_requeues(workspace):
    n = NoticeInput(notice_id=5, title="采购", body="原文")
    assert workspace.ingest(n, "k1", False)["changed"]
    assert not workspace.ingest(n, "k1", False)["changed"]
    assert workspace.ingest(n, "k2", False)["changed"]
    assert workspace.tasks()["total"] == 2


def test_result_database_cannot_be_source_database():
    from analyzer.config import Settings
    with pytest.raises(ValueError, match="独立"):
        Settings(_env_file=None,
                 database_url="postgresql+psycopg://writer:secret@db:5432/bidinfo",
                 source_db_dsn="postgresql://reader:other@db:5432/bidinfo")


def test_project_timeline_uses_original_publication_order(workspace):
    workspace.ingest(NoticeInput(notice_id=20, title="结果", body="正文", project_key="p",
                                publish_at="2026-10-20T12:00:00+08:00"), "k", False)
    workspace.ingest(NoticeInput(notice_id=21, title="采购", body="正文", project_key="p",
                                publish_at="2026-10-01T12:00:00+08:00"), "k", False)
    items = workspace.project("p")["notices"]
    assert [n["notice_id"] for n in items] == [21, 20]
    assert items[0]["publish_at"].startswith("2026-10-01")


def test_old_history_does_not_take_all_new_capacity(workspace):
    from sqlalchemy import update

    from analyzer.workspace import TaskRow
    workspace.ingest(NoticeInput(notice_id=100, title="历史", body="正文"), "k", False, priority=30)
    with workspace.store.session.begin() as s:
        s.execute(update(TaskRow).values(created_at=1))
    for nid in range(101, 106):
        workspace.ingest(NoticeInput(notice_id=nid, title="新增", body="正文"), "k", False, priority=100)
    picked = [workspace.claim("rule")["notice_id"] for _ in range(5)]
    assert 100 not in picked[:4]
    assert picked[4] == 100


def test_actual_late_writes_are_rejected(workspace):
    workspace.ingest(NoticeInput(notice_id=33, title="原公告", body="原文"), "k1", True)
    task = workspace.claim("rule")
    workspace.ingest(NoticeInput(notice_id=33, title="更正", body="更正文"), "k2", True)
    assert not workspace.save_rules(33, "k1", [], True, [], task)
    assert not workspace.save_model(task, [], False, {})
    assert workspace.detail(33)["analysis_status"] == "queued"
