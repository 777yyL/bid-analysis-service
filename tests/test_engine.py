import pytest

from analyzer.config import Settings
from analyzer.engine import analysis_fingerprint, prepare
from analyzer.schemas import Attachment, BusinessLine, Keyword, KnowledgeInput, Lot, NoticeInput


def knowledge():
    return KnowledgeInput(
        version="v1",
        lines=[
            BusinessLine(business_domain="教育", business_line="学校", body_md="校园安防建设"),
            BusinessLine(business_domain="医疗", business_line="医院", body_md="病房呼叫建设"),
        ],
        keywords=[
            Keyword(business_line="学校", word="安防"),
            Keyword(business_line="学校", word="校园安防"),
            Keyword(business_line="医院", word="病房呼叫"),
            Keyword(business_line="医院", word="学校", enabled=False),
        ],
    )


def test_scans_attachments_and_lots_for_multiple_lines_with_overlapping_keywords():
    notice = NoticeInput(
        notice_id=1, title="综合采购", body="本次公开采购详见各标包。",
        lots=[Lot(lot_no="1", lot_name="校园安防")],
        attachments=[Attachment(att_id=10, text="采购清单：病房呼叫系统 20 套。")],
    )
    result = prepare(notice, knowledge(), Settings(_env_file=None))
    assert {x.business_line for x in result.candidates} == {"学校", "医院"}
    school = next(x for x in result.candidates if x.business_line == "学校")
    assert set(school.matched_terms) == {"安防", "校园安防"}
    assert any(e.source.startswith("lot:") for e in result.evidence)
    assert any("病房呼叫" in e.text for e in result.evidence)
    assert not result.review_required


def test_unknown_and_disabled_keywords_do_not_become_irrelevant_decisions():
    result = prepare(
        NoticeInput(notice_id=2, title="学校采购", body="采购桌椅。"), knowledge(),
        Settings(_env_file=None),
    )
    assert result.candidates == []
    assert result.review_required


def test_missing_attachment_text_is_incomplete_even_when_summary_matches():
    result = prepare(
        NoticeInput(notice_id=3, title="采购", body="见附件",
                    attachments=[Attachment(att_id=11, summary="校园安防", extract_status="skipped")]),
        knowledge(), Settings(_env_file=None),
    )
    assert result.candidates[0].business_line == "学校"
    assert not result.material_complete
    assert result.review_required
    assert all(e.text != "校园安防" for e in result.evidence)


def test_candidate_limit_is_explicit_review_and_does_not_change_business_scores():
    result = prepare(
        NoticeInput(notice_id=4, title="校园安防及病房呼叫", body="采购校园安防及病房呼叫"),
        knowledge(), Settings(_env_file=None, candidate_limit=1),
    )
    assert len(result.candidates) == 1
    assert result.review_required
    assert any("候选" in warning for warning in result.warnings)


def test_evidence_budget_preserves_original_text_and_marks_lost_lot_coverage():
    notice = NoticeInput(
        notice_id=5, title="校园安防", body="安防采购。",
        lots=[Lot(lot_no=str(i), lot_name="校园安防" + "设备" * 80) for i in range(12)],
    )
    result = prepare(notice, knowledge(), Settings(
        _env_file=None, evidence_max_chars=500, evidence_chunk_chars=200,
    ))
    assert result.selected_chars <= 500
    assert result.review_required
    originals = [notice.body, notice.title] + [lot.lot_name for lot in notice.lots]
    assert all(any(e.text in source for source in originals) for e in result.evidence)


def test_hash_invalidates_on_actual_attachment_text_and_knowledge_change():
    settings = Settings(_env_file=None)
    notice = NoticeInput(notice_id=6, title="安防", body="安防采购",
                         attachments=[Attachment(att_id=12, sha256="same", text="设备一")])
    first = prepare(notice, knowledge(), settings)
    notice.attachments[0].text = "设备二"
    second = prepare(notice, knowledge(), settings)
    assert first.material_hash != second.material_hash
    key = analysis_fingerprint(first, knowledge(), settings)
    changed = knowledge()
    changed.lines[0].body_md = "更改产品能力"
    assert key != analysis_fingerprint(first, changed, settings)
    assert key != analysis_fingerprint(first, knowledge(), settings.model_copy(
        update={"llm_model": "other-model"},
    ))
    assert key == analysis_fingerprint(first, knowledge(), settings)


def test_material_limit_fails_instead_of_silently_truncating():
    with pytest.raises(ValueError, match="材料"):
        prepare(NoticeInput(notice_id=7, title="安防", body="设备" * 1000), knowledge(),
                Settings(_env_file=None, material_max_chars=1000))


def test_partial_knowledge_and_long_knowledge_require_review():
    kb = knowledge()
    kb.quality = "keywords_only"
    result = prepare(NoticeInput(notice_id=8, title="安防", body="安防采购"), kb,
                     Settings(_env_file=None))
    assert result.review_required
    kb.quality = "full"
    kb.lines[0].body_md = "知识" * 500
    result = prepare(NoticeInput(notice_id=8, title="安防", body="安防采购"), kb,
                     Settings(_env_file=None, knowledge_max_chars_per_line=200))
    assert result.review_required
    assert any("知识" in warning for warning in result.warnings)


def test_negative_procurement_conditions_are_not_lost_behind_repeated_positive_hits():
    kb = knowledge()
    kb.keywords.append(Keyword(business_line="学校", word="仅土建", kind="negative"))
    body = "安防设备采购。\n" * 100 + "本项目仅土建施工，不含设备供应。"
    result = prepare(NoticeInput(notice_id=9, title="安防建设", body=body), kb,
                     Settings(_env_file=None, evidence_max_chars=500, evidence_chunk_chars=100))
    assert any("仅土建" in item.text for item in result.evidence) or result.review_required


def test_negative_evidence_coverage_loss_requires_review():
    kb = knowledge()
    kb.keywords.append(Keyword(business_line="学校", word="仅土建", kind="negative"))
    attachments = [Attachment(att_id=20 + i, text="仅土建" + "施工" * 45, extract_status="ok")
                   for i in range(10)]
    result = prepare(NoticeInput(notice_id=10, title="安防", body="安防采购", attachments=attachments),
                     kb, Settings(_env_file=None, evidence_max_chars=500, evidence_chunk_chars=100))
    assert result.review_required
