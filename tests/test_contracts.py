import pytest
from pydantic import ValidationError

from analyzer.schemas import KnowledgeInput


def test_rejects_keyword_for_unknown_business_line():
    with pytest.raises(ValidationError, match="未知业务线"):
        KnowledgeInput.model_validate({
            "version": "v1", "quality": "keywords_only",
            "lines": [{"business_domain": "教育", "business_line": "学校"}],
            "keywords": [{"business_line": "医院", "word": "病房"}],
        })


def test_full_knowledge_requires_actual_module_content():
    with pytest.raises(ValidationError, match="body_md"):
        KnowledgeInput.model_validate({
            "version": "v1", "lines": [{"business_domain": "教育", "business_line": "学校"}],
        })
