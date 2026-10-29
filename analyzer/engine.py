import hashlib
import json
import re
import unicodedata
from collections import defaultdict

from analyzer.config import Settings
from analyzer.schemas import (
    PIPELINE_VERSION,
    SCORING_VERSION,
    Candidate,
    Evidence,
    KnowledgeInput,
    NoticeInput,
    Prepared,
)
from analyzer.tagging import match_terms

WEIGHTS = {"core": 6, "ext": 3, "scene": 1, "tech": 2, "negative": 0}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def prepare(notice: NoticeInput, knowledge: KnowledgeInput, settings: Settings) -> Prepared:
    originals = [("title", notice.title), ("buyer", notice.buyer), ("body", notice.body)]
    routing = [("summary:notice", notice.scope_summary)]
    lots = []
    for index, lot in enumerate(notice.lots):
        source = f"lot:{index}:{lot.lot_no}"
        originals.extend([(f"{source}:name", lot.lot_name), (f"{source}:description", lot.description)])
        if lot.lot_name.strip() or lot.description.strip():
            lots.append(source)
    warnings = [*notice.warnings, *knowledge.warnings]
    complete = bool(notice.body.strip()) and not notice.warnings
    if not notice.body.strip():
        warnings.append("缺少公告正文，不能视为完整材料")
    for attachment in notice.attachments:
        source = f"attachment:{attachment.att_id}:{attachment.filename}"
        originals.append((source, attachment.text))
        routing.append((f"summary:{source}", attachment.summary))
        if not attachment.text.strip() or attachment.extract_status not in (None, "ok"):
            complete = False
            warnings.append(f"附件 {attachment.att_id} 缺少完整抽取文本")
    size = sum(len(text) for _, text in [*originals, *routing])
    if size > settings.material_max_chars:
        raise ValueError("材料超过 MATERIAL_MAX_CHARS；未静默截断")

    review = not complete or knowledge.quality != "full" or bool(knowledge.warnings)
    if knowledge.quality != "full":
        warnings.append("知识仅含词表或内容不完整，需要人工复核")
    keywords = [kw for kw in knowledge.keywords if kw.enabled and _normalized(kw.word)]
    words = tuple({kw.word for kw in keywords})
    scores = defaultdict(float)
    matches = defaultdict(set)
    source_words = {}
    for source, text in [*originals, *routing]:
        found = {hit["term"] for hit in match_terms(text, words)}
        source_words[source] = found
        factor = 0.5 if source.startswith("summary:") or source == "buyer" else 1
        for kw in keywords:
            if WEIGHTS[kw.kind] and kw.word in found:
                scores[kw.business_line] += WEIGHTS[kw.kind] * factor
                matches[kw.business_line].add(kw.word)
    lines = {line.business_line: line for line in knowledge.lines}
    ranked = sorted(scores, key=lambda name: (-scores[name], name))
    if len(ranked) > settings.candidate_limit:
        review = True
        warnings.append(f"候选业务线 {len(ranked)} 条，超过上限 {settings.candidate_limit}；需扩大候选复核")
    candidates = [Candidate(business_domain=lines[name].business_domain, business_line=name,
                            retrieval_score=scores[name], matched_terms=sorted(matches[name]))
                  for name in ranked[:settings.candidate_limit]]
    if not candidates:
        review = True
        warnings.append("未召回候选业务线；不等同于业务不相关，需抽查或补充词表")
    for candidate in candidates:
        if len(lines[candidate.business_line].body_md) > settings.knowledge_max_chars_per_line:
            review = True
            warnings.append(f"业务线 {candidate.business_line} 知识超出单模块预算，精判仅使用前部内容，需复核")

    # The evidence is verbatim. Summaries participate in routing only.
    relevant_words = {
        kw.word: WEIGHTS[kw.kind] for kw in keywords
        if kw.business_line in {c.business_line for c in candidates}
    }
    negative_words = {kw.word for kw in keywords if kw.kind == "negative"
                      and kw.business_line in {c.business_line for c in candidates}}
    chunks = []
    width = settings.evidence_chunk_chars
    step = width - min(80, width // 5)
    for source, text in originals:
        for offset in range(0, len(text), step):
            fragment = text[offset:offset + width]
            if not fragment.strip():
                continue
            found = {hit["term"] for hit in match_terms(fragment, relevant_words)}
            score = sum(relevant_words[word] for word in found) + 10 * len(found & negative_words)
            if any(term in fragment for term in ("采购内容", "采购清单", "技术要求", "服务范围", "采购需求")):
                score += 2
            evidence = Evidence(id=_hash([source, offset, fragment])[:20], source=source, text=fragment)
            chunks.append((evidence, score, found))
            if offset + width >= len(text):
                break

    selected, selected_ids, used = [], set(), 0

    def take(item) -> bool:
        nonlocal used
        evidence = item[0]
        if evidence.id in selected_ids:
            return True
        if used + len(evidence.text) > settings.evidence_max_chars:
            return False
        selected.append(evidence)
        selected_ids.add(evidence.id)
        used += len(evidence.text)
        return True

    ordered = sorted(chunks, key=lambda item: (-item[1], item[0].source, item[0].id))
    # A negative condition must not disappear behind many repeated positive hits.
    for source, _ in originals:
        for word in sorted(negative_words):
            if word not in source_words[source]:
                continue
            item = next((item for item in ordered
                         if item[0].source == source and word in item[2]), None)
            if item is None or not take(item):
                review = True
                warnings.append(f"证据预算无法覆盖来源 {source} 的负向条件 {word}，需复核")
    # Cover each candidate with original procurement text, not just a school/hospital name.
    for candidate in candidates:
        evidence = next((item for item in ordered
                         if item[0].source not in ("title", "buyer")
                         and item[2].intersection(candidate.matched_terms)), None)
        if evidence is None or not take(evidence):
            review = True
            warnings.append(f"业务线 {candidate.business_line} 缺少预算内原文采购证据")
    for source in lots:
        item = next((item for item in ordered if item[0].source.startswith(source + ":")), None)
        if item is not None and not take(item):
            review = True
            warnings.append(f"证据预算无法覆盖标段 {source}，需扩大预算复核")
    # Include at least one matching fragment from every relevant attachment/body source.
    required_sources = {item[0].source for item in ordered if item[1] > 0 and item[2]
                        and (item[0].source == "body" or item[0].source.startswith("attachment:"))}
    for source in sorted(required_sources):
        item = next(item for item in ordered if item[0].source == source and item[2])
        if not take(item):
            review = True
            warnings.append(f"证据预算无法覆盖相关来源 {source}")
    for item in ordered:
        take(item)
    return Prepared(notice_id=notice.notice_id, title=notice.title, candidates=candidates,
                    evidence=selected, warnings=list(dict.fromkeys(warnings)), review_required=review,
                    material_complete=complete, input_chars=size, selected_chars=used,
                    material_hash=_hash(notice.model_dump()))


def analysis_fingerprint(prepared: Prepared, knowledge: KnowledgeInput, settings: Settings) -> str:
    config = {name: getattr(settings, name) for name in (
        "llm_model", "llm_base_url", "llm_json_mode", "llm_max_output_tokens",
        "llm_max_input_chars", "candidate_limit", "evidence_max_chars", "evidence_chunk_chars",
        "knowledge_max_chars_per_line", "material_max_chars",
    )}
    return _hash({"prepared": prepared.model_dump(), "knowledge": knowledge.model_dump(),
                  "config": config, "pipeline": PIPELINE_VERSION, "scoring": SCORING_VERSION})
