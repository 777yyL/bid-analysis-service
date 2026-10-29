"""Independent business/product tags, with reusable text matches and original evidence."""

import hashlib
import json
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from functools import lru_cache

import ahocorasick

from analyzer.schemas import KnowledgeInput, NoticeInput, TextCache

MATCH_VERSION = "aho-nfkc-v1"
RULE_VERSION = "context-v2"
GENERIC = {"ai", "人工智能", "大数据", "视频", "视频监控", "物联网", "监控", "音频",
           "塔式", "工控", "准入", "黑板", "显示系统", "平台", "系统"}
EXPERIENCE = re.compile(r"业绩|类似项目|成功案例|供应商.*(?:经验|资格)|投标人.*(?:经验|资格)")
BACKGROUND = re.compile(r"背景介绍|历史沿革|产业园.*(?:绿化|养护)|发展规划")
PROCUREMENT = re.compile(r"采购|购置|购入|清单|技术要求|招标范围|服务范围|建设内容|供应|配置|数量|需求|标的|设备名称|货物名称")
NEGATION = re.compile(r"不(?:包含|含|涉及|采购|需要)|无需|排除|仅.*(?:土建|绿化|养护)")
CLAUSE = re.compile(r"[。；;\n\r]")


def normalize(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if not char.isspace())


def _normalized_offsets(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char).casefold():
            if not normalized.isspace():
                chars.append(normalized)
                offsets.append(index)
    return "".join(chars), offsets


@lru_cache(maxsize=8)
def _automaton(words: tuple[str, ...]):
    automaton = ahocorasick.Automaton()
    groups = defaultdict(list)
    for word in words:
        value = normalize(word)
        if value:
            groups[value].append(word)
    for value, originals in groups.items():
        automaton.add_word(value, (value, originals))
    if groups:
        automaton.make_automaton()
    return automaton, bool(groups)


def _ascii_word(char):
    return char.isascii() and (char.isalnum() or char == "_")


def match_terms(text: str, words, cache: TextCache | None = None) -> list[dict]:
    """Return all exact dictionary hits; start/end are offsets in the original text."""
    if not text:
        return []
    dictionary = tuple(sorted(set(words)))
    digest = hashlib.sha256(json.dumps([MATCH_VERSION, dictionary], ensure_ascii=False).encode()).hexdigest()
    text_digest = hashlib.sha256(text.encode()).hexdigest()
    key = f"matches:{digest}:{text_digest}"
    if cache is not None:
        cached = cache.get_text(key)
        if cached is not None:
            return json.loads(cached)
    value, offsets = _normalized_offsets(text)
    machine, populated = _automaton(dictionary)
    hits = []
    if populated:
        for last, (term, originals) in machine.iter(value):
            first = last - len(term) + 1
            # Latin abbreviations should not match inside email/English words.
            start, end = offsets[first], offsets[last] + 1
            if ((_ascii_word(term[0]) and start > 0 and _ascii_word(normalize(text[start - 1])))
                    or (_ascii_word(term[-1]) and end < len(text)
                        and _ascii_word(normalize(text[end])))):
                continue
            for original in originals:
                hits.append({"term": original, "start": start, "end": end})
    hits.sort(key=lambda item: (item["start"], item["end"], item["term"]))
    if cache is not None:
        cache.set_text(key, json.dumps(hits, ensure_ascii=False, separators=(",", ":")))
    return hits


def _context(text, start, end, boundaries):
    left_index = bisect_left(boundaries, start)
    right_index = bisect_right(boundaries, end - 1)
    left = boundaries[left_index - 1] + 1 if left_index else 0
    right = boundaries[right_index] if right_index < len(boundaries) else len(text)
    # Long extracted table rows should not make evidence span whole attachments.
    left, right = max(left, start - 180), min(right, end + 180)
    return text[left:right], left, right


def _sources(notice):
    yield "title", notice.title
    yield "buyer", notice.buyer
    yield "body", notice.body
    for index, lot in enumerate(notice.lots):
        yield f"lot:{index}:name", lot.lot_name
        yield f"lot:{index}:description", lot.description
    for attachment in notice.attachments:
        yield f"attachment:{attachment.att_id}", attachment.text


def analyze_tags(notice: NoticeInput, knowledge: KnowledgeInput,
                 cache: TextCache | None = None) -> dict:
    """Generate independent rule tags. No match never means business irrelevant."""
    relationships = defaultdict(list)
    lines = {line.business_line: line.business_domain for line in knowledge.lines}
    shared_terms = defaultdict(set)
    for keyword in knowledge.keywords:
        if keyword.enabled and keyword.kind != "negative":
            shared_terms[normalize(keyword.word)].add(keyword.business_line)
    for keyword in knowledge.keywords:
        if keyword.enabled and normalize(keyword.word):
            relationships[keyword.word].append({
                "dimension": "business", "category": lines[keyword.business_line],
                "label": keyword.business_line, "kind": keyword.kind, "exclusions": [],
                "weak": (keyword.kind in ("scene", "tech", "negative")
                         or normalize(keyword.word) in GENERIC
                         or len(shared_terms[normalize(keyword.word)]) > 1),
            })
    for keyword in knowledge.product_keywords:
        if keyword.enabled and normalize(keyword.word):
            relationships[keyword.word].append({
                "dimension": "product", "category": keyword.category, "label": keyword.word,
                "kind": "product", "exclusions": keyword.exclude_patterns,
                "weak": keyword.strength == "weak" or normalize(keyword.word) in GENERIC,
            })
    observations = defaultdict(list)
    hit_counts = defaultdict(int)
    sample_counts = defaultdict(int)
    input_chars = 0
    for source, text in _sources(notice):
        input_chars += len(text)
        boundaries = [boundary.start() for boundary in CLAUSE.finditer(text)]
        for hit in match_terms(text, relationships, cache):
            context, excerpt_start, excerpt_end = _context(text, hit["start"], hit["end"], boundaries)
            for relation in relationships[hit["term"]]:
                reason = "关键词命中，采购关系或业务归属待确认"
                state = "candidate"
                excluded = (EXPERIENCE.search(context) or BACKGROUND.search(context)
                            or any(normalize(pattern) in normalize(context)
                                   for pattern in relation["exclusions"] if normalize(pattern)))
                if excluded:
                    state, reason = "excluded", "命中业绩、背景或该产品的局部排除条件"
                elif NEGATION.search(context):
                    # A broad negation may refer to another item in the same clause.
                    state, reason = "candidate", "同一上下文含否定表达，需确认否定范围"
                elif relation["kind"] == "negative":
                    state, reason = "candidate", "命中业务负向条件，需要结合采购范围复核"
                elif (not relation["weak"] and source != "buyer"
                      and PROCUREMENT.search(context)):
                    state, reason = "confirmed", "明确采购上下文命中具体关键词"
                key = (relation["dimension"], relation["category"], relation["label"])
                hit_counts[key] += 1
                bucket = (key, state, relation["kind"] == "negative")
                if sample_counts[bucket] >= 12:
                    continue
                sample_counts[bucket] += 1
                evidence = {"source": source, "text": context, **hit,
                            "excerpt_start": excerpt_start, "excerpt_end": excerpt_end,
                            "negative_signal": relation["kind"] == "negative"}
                observations[key].append((state, reason, evidence))
    tags = []
    priority = {"confirmed": 0, "candidate": 1, "excluded": 2}
    for (dimension, category, label), hits in sorted(observations.items()):
        ordered = sorted(hits, key=lambda item: priority[item[0]])
        selected = ordered[0]
        if dimension == "business" and any(hit[2]["negative_signal"] for hit in ordered):
            selected = ("candidate", "同时命中业务负向条件，需要结合采购范围复核", selected[2])
        evidence, seen = [], set()
        for _, _, item in ordered:
            key = (item["source"], item["start"], item["end"], item["term"])
            if key not in seen:
                evidence.append(item)
                seen.add(key)
        count = hit_counts[(dimension, category, label)]
        truncated = count > len(evidence)
        reason = selected[1]
        if truncated:
            reason += f"；原文命中 {count} 次，展示 {len(evidence)} 条代表证据"
        tags.append({"dimension": dimension, "category": category, "label": label,
                     "status": selected[0], "source": "rule", "reason": reason,
                     "evidence": evidence, "hit_count": count, "evidence_truncated": truncated})
    warnings = list(notice.warnings)
    complete = bool(notice.body.strip()) and not notice.warnings
    if not notice.body.strip():
        warnings.append("缺少公告正文，不能视为完整材料")
    for attachment in notice.attachments:
        if not attachment.text.strip() or attachment.extract_status not in (None, "ok"):
            complete = False
            warnings.append(f"附件 {attachment.att_id} 缺少完整抽取文本")
    return {"tags": tags, "material_complete": complete,
            "warnings": list(dict.fromkeys(warnings)), "input_chars": input_chars,
            "rule_version": RULE_VERSION, "match_version": MATCH_VERSION}
