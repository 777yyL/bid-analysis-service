from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCORING_VERSION = "report-20261028-v1"
PIPELINE_VERSION = "0.1.0"
TECH_FAMILIES = (
    "视觉感知类", "物联感知类", "人工智能与算法类", "平台与数据类", "专用系统与业务应用类"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Keyword(StrictModel):
    business_line: str = Field(min_length=1)
    word: str = Field(min_length=1)
    kind: Literal["core", "ext", "scene", "tech", "negative"] = "core"
    term_family: str | None = None
    enabled: bool = True


class BusinessLine(StrictModel):
    business_domain: str = Field(min_length=1)
    business_line: str = Field(min_length=1)
    body_md: str = ""


class ProductKeyword(StrictModel):
    category: str = Field(min_length=1, max_length=200)
    word: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    strength: Literal["strong", "weak"] = "strong"
    # Literal exclusions evaluated in the hit's clause, never across the document.
    exclude_patterns: list[str] = Field(default_factory=list, max_length=100)


class KnowledgeInput(StrictModel):
    version: str = Field(min_length=1, max_length=128)
    lines: list[BusinessLine] = Field(default_factory=list, max_length=500)
    keywords: list[Keyword] = Field(default_factory=list, max_length=50000)
    product_keywords: list[ProductKeyword] = Field(default_factory=list, max_length=50000)
    quality: Literal["full", "keywords_only"] = "full"
    source_rubric: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_lines(self):
        names = [line.business_line for line in self.lines]
        if len(set(names)) != len(names):
            raise ValueError("业务线不能重名")
        unknown = {kw.business_line for kw in self.keywords} - set(names)
        if unknown:
            raise ValueError(f"关键词引用未知业务线: {sorted(unknown)}")
        if self.quality == "full" and any(not line.body_md.strip() for line in self.lines):
            raise ValueError("full 知识必须包含每条业务线的 body_md；词表模式请使用 keywords_only")
        return self


class Attachment(StrictModel):
    att_id: int = Field(gt=0)
    filename: str = ""
    sha256: str | None = None
    text: str = ""
    text_truncated: bool = False
    summary: str = ""
    extract_status: str | None = None


class Lot(StrictModel):
    lot_no: str = ""
    lot_name: str = ""
    description: str = ""


class AnalysisDocument(StrictModel):
    notice_id: int = Field(gt=0)
    notice_title: str = ""
    notice_type: str = ""
    source: str
    text: str
    start: int = Field(default=0, ge=0)
    section_role: Literal["demand", "unknown"] = "unknown"


class NoticeInput(StrictModel):
    notice_id: int = Field(gt=0)
    title: str = Field(min_length=1)
    buyer: str = ""
    notice_type: str = ""
    project_key: str | None = None
    round_key: str | None = Field(default=None, max_length=200)
    lot_key: str | None = Field(default=None, max_length=200)
    related_notice_id: int | None = Field(default=None, gt=0)
    revoke_status: str = "active"
    material_loaded: bool = True
    publish_at: str | None = Field(default=None, max_length=80)
    body: str = ""
    scope_summary: str = ""
    lots: list[Lot] = Field(default_factory=list, max_length=1000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=1000)
    warnings: list[str] = Field(default_factory=list)
    analysis_documents: list[AnalysisDocument] = Field(default_factory=list, max_length=100000)
    analysis_scope: Literal["notice", "project"] = "notice"

    @field_validator("publish_at")
    @classmethod
    def canonical_publish_at(cls, value):
        if not value:
            return None
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return instant.astimezone(timezone.utc).isoformat()


class Candidate(StrictModel):
    business_domain: str
    business_line: str
    retrieval_score: float
    matched_terms: list[str]


class Evidence(StrictModel):
    id: str
    source: str
    text: str


class Prepared(StrictModel):
    notice_id: int
    title: str
    candidates: list[Candidate]
    evidence: list[Evidence]
    warnings: list[str] = Field(default_factory=list)
    review_required: bool = False
    material_complete: bool = True
    input_chars: int = 0
    selected_chars: int = 0
    material_hash: str


class Decision(StrictModel):
    business_domain: str
    business_line: str
    target_score: int = Field(ge=0, le=50, strict=True)
    scene_score: int = Field(ge=0, le=20, strict=True)
    buyer_score: int = Field(ge=0, le=10, strict=True)
    tech_families: list[str] = Field(default_factory=list)
    negative_signals: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2000)
    score: int = Field(ge=0, le=100)
    level: Literal["A", "B", "C", "D"]


class Judgment(StrictModel):
    decisions: list[Decision]
    model: str
    usage: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float = 0
    request_count: int = 1
    scoring_version: str = SCORING_VERSION


class TextCache(Protocol):
    def get_text(self, key: str) -> str | None: ...
    def set_text(self, key: str, value: str) -> None: ...
