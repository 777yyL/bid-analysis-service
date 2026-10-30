import pytest

from analyzer.config import Settings
from analyzer.project_flow import ProjectFlow
from analyzer.schemas import KnowledgeInput, NoticeInput, ProductKeyword
from analyzer.store import Store
from analyzer.workspace import Workspace


@pytest.fixture
def flow(tmp_path):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'projects.db'}")
    store = Store(cfg.database_url)
    store.initialize()
    ws = Workspace(store)
    key = store.save_knowledge(KnowledgeInput(version="v1", product_keywords=[
        ProductKeyword(category="网络产品", word="交换机"), ProductKeyword(category="IPC", word="摄像机")]))
    service = ProjectFlow(cfg, ws, None, None, lambda: {"paused": False, "model_paused": False, "llm_rpm": 5})
    yield service, key
    store.close()


def notice(nid, stage, body, day=1, **extra):
    return NoticeInput(notice_id=nid, title=f"校园项目{stage}", notice_type=stage, project_key="campus",
                       publish_at=f"2026-10-{day:02d}T08:00:00+08:00", body=body, **extra)


@pytest.mark.asyncio
async def test_one_main_and_later_contract_do_not_requeue_analysis(flow):
    service, key = flow
    await service.ingest([notice(1, "采购意向", "采购摄像机"),
                          notice(2, "采购公告", "采购清单：交换机10台。", 2)], key, False)
    assert await service.run_once("rule")
    detail = service.projects.project("campus")
    assert detail["analyses"][0]["primary_notice_id"] == 2
    assert any(t["label"] == "交换机" for t in detail["tags"])
    assert not any(t["label"] == "摄像机" for t in detail["tags"])
    count = service.ws.tasks()["total"]
    await service.ingest([notice(3, "合同公告", "本合同采购摄像机", 10)], key, False)
    assert not await service.run_once("rule")
    assert service.ws.tasks()["total"] == count
    assert service.projects.project("campus")["current_stage"] == "合同公告"


@pytest.mark.asyncio
async def test_result_event_backtracks_to_procurement(flow):
    service, key = flow
    class Source:
        async def fetch_project_notices(self, keys):
            assert keys == ["campus"]
            return [notice(11, "采购公告", "采购交换机10台", 1),
                    notice(12, "结果公告", "成交结果", 5)]
    service.source = Source()
    await service.ingest([notice(12, "结果公告", "成交结果", 5)], key, False, backfill=True)
    assert service.projects.project("campus")["analyses"][0]["primary_notice_id"] == 11
    assert await service.run_once("rule")
    assert service.ws.tasks()["total"] == 1


@pytest.mark.asyncio
async def test_missing_main_is_material_pending_not_unrelated(flow):
    service, key = flow
    await service.ingest([notice(50, "验收公告", "验收交换机", 20)], key, True)
    result = service.projects.project("campus")
    assert result["analyses"][0]["analysis_status"] == "missing_primary"
    assert not result["tags"]
    assert service.ws.tasks()["total"] == 0


@pytest.mark.asyncio
async def test_date_only_correction_does_not_requeue(flow):
    service, key = flow
    await service.ingest([notice(1, "采购公告", "采购清单：交换机10台。")], key, False)
    assert await service.run_once("rule")
    before = service.projects.project("campus")["analyses"][0]["revision"]
    count = service.ws.tasks()["total"]
    await service.ingest([notice(2, "更正公告", "开标时间更正为2026年11月1日，其他内容不变。", 2,
                                 related_notice_id=1)], key, False)
    assert service.projects.project("campus")["analyses"][0]["revision"] == before
    assert service.ws.tasks()["total"] == count


@pytest.mark.asyncio
async def test_moved_notice_invalidates_previous_project_and_updates_budget(flow):
    service, key = flow
    await service.ingest([notice(1, "采购公告", "采购交换机10台")], key, False)
    old = service.ws.claim("project_rule")
    moved = notice(1, "采购公告", "采购交换机10台").model_copy(update={"project_key": "new-campus"})
    await service.ingest([moved], key, False)
    assert not service.projects.current(old)
    with pytest.raises(KeyError):
        service.projects.project("campus")
    service.scheduler = lambda: {"paused": False, "model_paused": False, "llm_rpm": 2}
    assert await service.run_once("rule")
    assert service.cfg.llm_rpm == 2


@pytest.mark.asyncio
async def test_explicit_rule_only_rerun_disables_pending_model(flow):
    service, key = flow
    await service.ingest([notice(1, "采购公告", "采购交换机10台")], key, True)
    assert await service.run_once("rule")
    old_model = service.ws.claim("project_model")
    await service.reanalyze("campus", use_model=False, force=True, backfill=False)
    assert not service.projects.current(old_model)
    assert await service.run_once("rule")
    assert service.ws.claim("project_model") is None


@pytest.mark.asyncio
async def test_substantive_correction_invalidates_and_stale_rule_cannot_write(flow):
    service, key = flow
    await service.ingest([notice(1, "采购公告", "采购交换机10台")], key, False)
    old_task = service.ws.claim("project_rule")
    await service.ingest([notice(2, "更正公告", "采购清单更正：交换机改为摄像机。", 2,
                                 related_notice_id=1)], key, False)
    assert not service.projects.save_rules(old_task, {"tags": [], "material_complete": True, "warnings": []})
    assert await service.run_once("rule")
    result = service.projects.project("campus")
    assert result["analyses"][0]["correction_notice_ids"] == [2]
    assert result["analyses"][0]["analysis_status"] == "needs_review"
