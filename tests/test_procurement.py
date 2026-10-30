from analyzer.procurement import classify_correction, procurement_sections
from analyzer.schemas import KnowledgeInput, NoticeInput, ProductKeyword
from analyzer.tagging import analyze_tags


def knowledge():
    return KnowledgeInput(version="v1", product_keywords=[
        ProductKeyword(category="会议", word="视频会议"),
        ProductKeyword(category="PC", word="电脑"),
        ProductKeyword(category="网络", word="交换机")])


def test_demand_table_rows_inherit_heading_and_process_section_is_removed():
    text = "## 一、采购需求\n|名称|数量|\n|交换机|10台|\n## 二、投标人资格\n供应商须具有交换机项目业绩。"
    result = procurement_sections(text)
    assert result["has_demand"]
    assert "|交换机|10台|" in result["text"]
    assert "项目业绩" not in result["text"]
    for span in result["spans"]:
        assert text[span["start"]:span["end"]] == span["text"]
    tags = analyze_tags(NoticeInput(notice_id=1, title="设备招标", body=text), knowledge())["tags"]
    tag = next(t for t in tags if t["label"] == "交换机")
    assert tag["status"] == "confirmed"
    assert any(e["section_role"] == "demand" for e in tag["evidence"])
    assert all(e["notice_id"] == 1 for e in tag["evidence"])


def test_opening_video_and_supplier_self_equipment_are_not_procurement():
    text = "供应商须配置电脑参加远程开标。开标采用视频会议方式。"
    tags = analyze_tags(NoticeInput(notice_id=2, title="项目公告", body=text), knowledge())["tags"]
    assert tags
    assert all(t["status"] == "excluded" for t in tags)


def test_generic_configuration_and_supply_do_not_by_themselves_confirm():
    result = analyze_tags(NoticeInput(notice_id=3, title="公告", body="供应商配置交换机。"), knowledge())
    assert result["tags"][0]["status"] != "confirmed"


def test_real_installation_maintenance_and_training_remain_demand():
    text = "采购内容：视频会议系统的安装、维护和使用培训。"
    result = procurement_sections(text)
    assert result["has_demand"]
    assert "使用培训" in result["text"]
    assert analyze_tags(NoticeInput(notice_id=4, title="采购", body=text), knowledge())["tags"][0]["status"] == "confirmed"


def test_unknown_text_is_retained_and_mixed_context_is_not_confirmed():
    text = "交换机相关事项待说明。"
    assert text in procurement_sections(text)["text"]
    mixed = "采购范围与投标文件上传要求：视频会议相关事项。"
    tag = analyze_tags(NoticeInput(notice_id=5, title="公告", body=mixed), knowledge())["tags"][0]
    assert tag["status"] != "confirmed"


def test_correction_classification_distinguishes_deadline_and_missing_attachment():
    n = NoticeInput(notice_id=1, title="更正", notice_type="更正公告", body="仅修改投标截止时间，其他内容不变。")
    assert classify_correction(n) == "procedural"
    assert classify_correction(n.model_copy(update={"body": "采购清单更正：交换机数量调整为20台。"})) == "substantive"
    assert classify_correction(n.model_copy(update={"body": "需求附件以重新上传的附件为准。"})) == "uncertain"


def test_unclear_heading_does_not_erase_explicit_actual_purchase():
    text = "## 投标须知\n本次采购交换机10台，具体要求另附。"
    result = procurement_sections(text)
    assert "本次采购交换机10台" in result["text"]
    tag = analyze_tags(NoticeInput(notice_id=6, title="公告", body=text), knowledge())["tags"][0]
    assert tag["status"] == "candidate"


def test_video_meeting_used_to_obtain_documents_is_not_product_purchase():
    text = "采购文件需要通过视频会议形式领取。"
    tags = analyze_tags(NoticeInput(notice_id=7, title="项目公告", body=text), knowledge())["tags"]
    assert tags[0]["status"] == "excluded"
