import pytest

from analyzer.importer import import_knowledge_files, parse_insert_rows


def test_sql_parser_preserves_quoted_apostrophe_and_newlines():
    rows = parse_insert_rows("INSERT INTO public.biz_knowledge (business_domain,business_line,body_md) "
                             "VALUES ('教育','高校','Bob''s\n采购'),('交通','路网','安全');",
                             "biz_knowledge")
    assert rows[0]["body_md"] == "Bob's\n采购"
    assert rows[1]["business_line"] == "路网"


def test_sql_parser_rejects_commands():
    with pytest.raises(ValueError):
        parse_insert_rows("DELETE FROM public.biz_keyword;", "biz_keyword")


def test_file_import_keeps_product_dimension_independent(tmp_path):
    (tmp_path / "业务域.txt").write_text(
        "INSERT INTO public.biz_knowledge (business_domain,business_line,body_md,rubric_version) "
        "VALUES ('教育','高校','校园安防','v1');", encoding="utf-8")
    (tmp_path / "关键词.txt").write_text(
        "INSERT INTO public.biz_keyword (business_line,word,kind,term_family,enabled,is_product) "
        "VALUES ('高校','大学','scene',NULL,true,false);", encoding="utf-8")
    (tmp_path / "产品关键词.txt").write_text(
        "【网络产品】交换机、路由器\n【音频产品】广播（排除广播电视台）、音频\n", encoding="utf-8")
    result = import_knowledge_files(str(tmp_path))
    assert result.lines[0].business_domain == "教育"
    assert len(result.keywords) == 1
    assert len(result.product_keywords) == 4
    broadcast = next(p for p in result.product_keywords if p.word == "广播")
    assert broadcast.exclude_patterns == ["广播电视台"]
    assert all(not hasattr(p, "business_domain") for p in result.product_keywords)
    assert next(p for p in result.product_keywords if p.word == "音频").strength == "weak"
