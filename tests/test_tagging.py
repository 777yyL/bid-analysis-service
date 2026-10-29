from analyzer.schemas import (
    Attachment,
    BusinessLine,
    Keyword,
    KnowledgeInput,
    NoticeInput,
    ProductKeyword,
)
from analyzer.tagging import analyze_tags, match_terms


def kb():
    return KnowledgeInput(
        version="tag-v1", quality="keywords_only",
        lines=[BusinessLine(business_domain="教育教学", business_line="高等教育"),
               BusinessLine(business_domain="医疗卫生", business_line="医院")],
        keywords=[Keyword(business_line="高等教育", word="大学", kind="scene"),
                  Keyword(business_line="高等教育", word="AI", kind="tech"),
                  Keyword(business_line="医院", word="AI", kind="tech")],
        product_keywords=[ProductKeyword(category="网络产品", word="交换机"),
                          ProductKeyword(category="服务器", word="塔式", strength="weak")],
    )


def test_product_tag_does_not_require_a_business_domain_match():
    result = analyze_tags(NoticeInput(notice_id=1, title="设备采购", body="采购清单：交换机20台。"), kb())
    assert [(t["dimension"], t["category"], t["label"], t["status"]) for t in result["tags"]] == [
        ("product", "网络产品", "交换机", "confirmed")]
    assert result["material_complete"]


def test_shared_words_preserve_relationships_and_generic_words_remain_candidates():
    result = analyze_tags(NoticeInput(notice_id=2, title="AI设备采购", body="采购AI服务器，采用塔式。"), kb())
    assert {(t["dimension"], t["label"]) for t in result["tags"]} == {
        ("business", "高等教育"), ("business", "医院"), ("product", "塔式")}
    assert all(t["status"] == "candidate" for t in result["tags"])


def test_procurement_evidence_wins_over_separate_vendor_experience():
    body = "供应商须提供交换机项目业绩。\n采购清单：交换机20台。"
    result = analyze_tags(NoticeInput(notice_id=3, title="设备采购", body=body), kb())
    tag = result["tags"][0]
    assert tag["status"] == "confirmed"
    assert any("采购清单" in e["text"] for e in tag["evidence"])
    only_experience = analyze_tags(NoticeInput(notice_id=4, title="项目公告", body="供应商须提供交换机项目业绩。"), kb())
    assert only_experience["tags"][0]["status"] == "excluded"


def test_exclusion_rules_apply_to_the_local_evidence_not_entire_document():
    knowledge = kb()
    knowledge.product_keywords = [ProductKeyword(category="音频产品", word="广播", exclude_patterns=["广播电视台"])]
    body = "采购人为广播电视台。\n采购需求：校园广播系统一套。"
    result = analyze_tags(NoticeInput(notice_id=5, title="设备采购", body=body), knowledge)
    assert result["tags"][0]["status"] == "confirmed"


def test_matching_returns_original_offsets_and_ascii_token_boundaries():
    text = "联系 mail@example.com；采购ＡＩ服务器、GPU 服务器。"
    hits = match_terms(text, ["AI", "GPU服务器"])
    assert [(h["term"], text[h["start"]:h["end"]]) for h in hits] == [
        ("AI", "ＡＩ"), ("GPU服务器", "GPU 服务器")]


class MemoryCache:
    def __init__(self):
        self.values = {}
        self.writes = 0

    def get_text(self, key):
        return self.values.get(key)

    def set_text(self, key, value):
        self.values[key] = value
        self.writes += 1


def test_text_cache_reuses_matches_but_rebinds_attachment_source_and_vocabulary():
    cache = MemoryCache()
    first = analyze_tags(NoticeInput(notice_id=6, title="采购", body="详见附件。", attachments=[
        Attachment(att_id=60, text="采购交换机。", extract_status="ok")]), kb(), cache)
    writes = cache.writes
    second = analyze_tags(NoticeInput(notice_id=7, title="采购", body="详见附件。", attachments=[
        Attachment(att_id=70, text="采购交换机。", extract_status="ok")]), kb(), cache)
    assert cache.writes == writes
    assert first["tags"][0]["evidence"][0]["source"] == "attachment:60"
    assert second["tags"][0]["evidence"][0]["source"] == "attachment:70"
    changed = kb()
    changed.product_keywords.append(ProductKeyword(category="网络产品", word="路由器"))
    analyze_tags(NoticeInput(notice_id=8, title="采购", body="采购路由器。"), changed, cache)
    assert cache.writes > writes


def test_missing_material_and_no_match_are_distinct_and_product_only_knowledge_is_valid():
    knowledge = KnowledgeInput(version="products", lines=[], product_keywords=[ProductKeyword(category="网络产品", word="交换机")])
    result = analyze_tags(NoticeInput(notice_id=9, title="桌椅采购", body="采购桌椅。"), knowledge)
    assert result["tags"] == []
    assert result["material_complete"]
    missing = analyze_tags(NoticeInput(notice_id=10, title="交换机采购", attachments=[
        Attachment(att_id=80, extract_status="failed")]), knowledge)
    assert not missing["material_complete"]
    assert missing["warnings"]


def test_business_scene_cannot_by_itself_confirm_product_relevance():
    result = analyze_tags(NoticeInput(notice_id=11, title="某大学食堂采购", body="采购蔬菜和肉类。"), kb())
    assert [(t["dimension"], t["label"], t["status"]) for t in result["tags"]] == [
        ("business", "高等教育", "candidate")]


def test_shared_core_term_requires_line_specific_evidence_and_negative_signal_requires_review():
    knowledge = kb()
    knowledge.keywords = [Keyword(business_line="高等教育", word="校园安防"),
                          Keyword(business_line="医院", word="校园安防")]
    result = analyze_tags(NoticeInput(notice_id=12, title="采购", body="采购校园安防。"), knowledge)
    assert len(result["tags"]) == 2
    assert all(tag["status"] == "candidate" for tag in result["tags"])
    knowledge.keywords = [Keyword(business_line="高等教育", word="校园安防"),
                          Keyword(business_line="高等教育", word="仅土建", kind="negative")]
    result = analyze_tags(NoticeInput(notice_id=13, title="采购", body="采购校园安防。仅土建施工。"), knowledge)
    assert result["tags"][0]["status"] == "candidate"


def test_latin_word_boundaries_use_original_whitespace():
    assert len(match_terms("AI equipment采购", ["AI"])) == 1
    assert match_terms("mail equipment采购", ["AI"]) == []
def test_repetitive_material_keeps_bounded_evidence_and_reports_sampling():
    from analyzer.schemas import KnowledgeInput, NoticeInput, ProductKeyword
    from analyzer.tagging import analyze_tags
    knowledge = KnowledgeInput(version="v1", product_keywords=[
        ProductKeyword(category="网络产品", word="交换机")])
    result = analyze_tags(NoticeInput(notice_id=99, title="采购", body="采购交换机。" * 500), knowledge)
    tag = result["tags"][0]
    assert tag["hit_count"] == 500
    assert len(tag["evidence"]) <= 36
    assert tag["evidence_truncated"]
    assert "代表证据" in tag["reason"]
