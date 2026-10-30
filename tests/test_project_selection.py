from analyzer.project_selection import select_project
from analyzer.schemas import Lot, NoticeInput


def notice(nid, stage, date="2026-10-01T09:00:00+08:00", **kwargs):
    return NoticeInput(notice_id=nid, title=f"项目{stage}", notice_type=stage,
                       project_key="p", publish_at=date, body="采购清单：交换机10台。", **kwargs)


def test_procurement_wins_over_intent_and_later_contract():
    rows = [notice(1, "采购意向"), notice(2, "采购公告", "2026-10-02"),
            notice(3, "合同公告", "2026-10-20")]
    plans = select_project(rows)
    assert len(plans) == 1
    assert plans[0]["main"].notice_id == 2
    assert plans[0]["corrections"] == []


def test_intent_is_used_only_without_procurement_and_missing_primary_is_explicit():
    assert select_project([notice(1, "采购意向")])[0]["main"].notice_id == 1
    result = select_project([notice(2, "结果公告")])[0]
    assert result["main"] is None
    assert result["status"] == "missing_primary"
    assert select_project([]) == []


def test_only_matching_substantive_corrections_are_selected():
    main = notice(1, "采购公告")
    specific = notice(2, "更正公告", "2026-10-03", related_notice_id=1)
    other = notice(3, "更正公告", "2026-10-03", related_notice_id=99)
    procedural = notice(4, "更正公告", "2026-10-04", related_notice_id=1).model_copy(
        update={"body": "仅将开标时间由10月5日延期至10月8日，其他内容不变。"})
    plan = select_project([main, specific, other, procedural])[0]
    assert [n.notice_id for n in plan["corrections"]] == [2]
    assert set(plan["ignored_correction_ids"]) == {3, 4}


def test_old_round_and_old_round_corrections_do_not_leak_into_current_round():
    rows = [notice(1, "采购公告", round_key="first"),
            notice(2, "更正公告", "2026-10-02", round_key="first", related_notice_id=1),
            notice(3, "采购意向", "2026-10-10", round_key="second")]
    plan = select_project(rows)[0]
    assert plan["round_key"] == "second"
    assert plan["main"].notice_id == 3
    assert plan["corrections"] == []


def test_explicit_lots_select_independent_primary_and_do_not_duplicate_shared_notice():
    a = notice(1, "采购公告", lot_key="A")
    b = notice(2, "采购公告", lot_key="B")
    plans = select_project([a, b])
    assert {p["main"].notice_id for p in plans} == {1, 2}
    shared = notice(3, "采购公告", "2026-10-10", lots=[Lot(lot_no="A"), Lot(lot_no="B")])
    plans = select_project([shared])
    assert len(plans) == 1
    assert plans[0]["warnings"]


def test_revoked_primary_not_selected_and_stage_is_not_guessed_from_title():
    revoked = notice(2, "采购公告", "2026-10-02", revoke_status="revoked")
    result = notice(3, "结果公告", "2026-10-03").model_copy(update={"title": "采购公告结果"})
    plan = select_project([notice(1, "采购意向"), revoked, result])[0]
    assert plan["main"].notice_id == 1


def test_same_project_inferred_correction_is_flagged_and_earlier_one_is_ignored():
    before = notice(2, "更正公告", "2026-09-30")
    after = notice(3, "更正公告", "2026-10-02")
    plan = select_project([notice(1, "采购公告"), before, after])[0]
    assert [n.notice_id for n in plan["corrections"]] == [3]
    assert any("关联" in warning for warning in plan["warnings"])


def test_different_projects_are_not_mixed():
    a = notice(1, "采购公告")
    b = notice(2, "更正公告", "2026-10-02").model_copy(update={"project_key": "another"})
    plans = select_project([a, b])
    assert next(p for p in plans if p["main"])["corrections"] == []


def test_ignored_procedural_correction_does_not_change_selection_warnings():
    main = notice(1, "采购公告")
    procedural = notice(2, "更正公告", "2026-10-02").model_copy(
        update={"body": "仅修改开标时间，其他内容不变。"})
    before = select_project([main])[0]
    after = select_project([main, procedural])[0]
    assert after["warnings"] == before["warnings"]
    assert after["ignored_correction_ids"] == [2]


def test_each_lot_chooses_its_own_current_round():
    rows = [notice(1, "采购公告", lot_key="A", round_key="A1"),
            notice(2, "采购公告", "2026-10-02", lot_key="B", round_key="B1"),
            notice(3, "采购公告", "2026-10-20", lot_key="A", round_key="A2")]
    plans = select_project(rows)
    assert {(p["lot_key"], p["main"].notice_id) for p in plans} == {("A", 3), ("B", 2)}
