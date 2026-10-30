"""Classify procurement evidence while retaining original source offsets."""

import re

from analyzer.schemas import NoticeInput

DEMAND_HEADING = re.compile(
    r"采购需求|采购清单|需求清单|货物清单|采购范围|招标范围|采购内容|采购标的|"
    r"技术参数|技术规格|技术要求|建设内容|服务内容|服务范围|服务需求|"
    r"货物名称|设备名称|需求一览|安装调试要求|维保服务|培训服务"
)
PROCESS_HEADING = re.compile(
    r"供应商资格|投标人资格|资格要求|资格条件|投标须知|投标文件|响应文件|"
    r"开标|评标|评分办法|获取采购文件|招标文件获取|报名|保证金|采购程序|采购方式"
)
PROCESS = re.compile(
    r"开标|投标文件|响应文件|电子交易|交易平台|保证金|投标人资格|供应商资格|"
    r"资格条件|类似(?:项目|业绩)|项目业绩|授权委托|营业执照|声明函|报名|"
    r"投标截止|响应截止|提交截止|递交截止|评审时间|磋商时间|答疑时间|"
    r"评标办法|评审办法|评分标准|磋商程序|上传(?:投标|响应|文件)|"
    r"投标人.*自备|供应商.*自备|须自备|"
    r"(?:采购|招标)文件.{0,20}(?:获取|下载|领取|售价|上传)|(?:获取|下载|领取)(?:采购|招标)文件"
)
DEMAND_ANCHOR = re.compile(
    r"采购清单|需求清单|采购范围|采购内容|采购需求|采购标的|招标范围|"
    r"技术参数|技术规格|建设内容|服务内容|服务范围|货物名称|设备名称|"
    r"本次采购|购置|购入|租赁|采购[^人方式程序公告文件代理项目]*?(?:设备|系统|产品|服务)"
)
DIRECT_PURCHASE = re.compile(r"采购|购置|购入|购货|租赁|建设内容|服务需求|需求清单|采购清单")
NUMBERED_HEADING = re.compile(r"^\s*(?:#{1,6}\s*|[一二三四五六七八九十]+[、．.]|\d+(?:\.\d+)*[、．.\s])")
CLAUSE = re.compile(r"[^。；;\n\r]+[。；;]?|[\n\r]+")
UNCHANGED = re.compile(r"(?:其他|其余|原|本次)?(?:采购需求|采购内容|采购范围|技术参数|内容|事项)(?:均|保持)?不变")
CHANGE = re.compile(r"更正|调整|变更|修改|替换|修订|为准|重新|延期|延长|取消")
PROCUREMENT_CHANGE = re.compile(
    r"采购清单|需求清单|采购范围|采购内容|采购需求|采购标的|技术参数|技术规格|"
    r"设备名称|货物名称|数量.{0,10}(?:调整|变更|修改|改为)|预算.{0,10}(?:调整|变更)|"
    r"产品.{0,12}(?:调整|变更|修改|改为)"
)
ATTACHMENT_CHANGE = re.compile(r"(?:需求|技术|清单)?附件.{0,20}(?:重新|替换|更新|变更|为准)|(?:重新|替换|更新|变更).{0,12}附件")


def _heading_role(line: str):
    stripped = line.strip()
    prefix = re.split(r"[:：]", stripped, maxsplit=1)[0]
    has_mark = bool(NUMBERED_HEADING.match(stripped))
    # A whole short heading or an explicit inline heading introduces a section.
    looks_heading = (len(stripped) <= 42 and not re.search(r"[。；;]", stripped)) or ":" in stripped or "：" in stripped
    if not looks_heading:
        return None
    demand, process = bool(DEMAND_HEADING.search(prefix)), bool(PROCESS_HEADING.search(prefix))
    if demand and process:
        return "unknown"
    if demand:
        return "demand"
    if process:
        return "process"
    if has_mark and len(stripped) <= 60:
        return "unknown"
    return None


def procurement_sections(text: str) -> dict:
    """Keep demand and uncertain clauses; remove definite process-only clauses.

    Spans refer to the original text. ``text`` is a filtered view for model input,
    not a replacement coordinate system for evidence offsets.
    """
    spans, kept = [], []
    section = "unknown"
    position = 0
    for line in text.splitlines(keepends=True):
        heading = _heading_role(line)
        if heading is not None:
            section = heading
        for match in CLAUSE.finditer(line):
            value = match.group()
            if not value.strip():
                continue
            process = bool(PROCESS.search(value))
            anchor = bool(DEMAND_ANCHOR.search(value))
            mixed = process and anchor
            if mixed:
                role = "unknown"
            elif process:
                role = "process"
            elif section == "process":
                # Mixed or poorly extracted headings must not erase an explicit
                # purchase requirement; keep it uncertain for semantic review.
                role = "unknown" if anchor or re.search(r"(?:采购|购置|购入).{0,25}\d+\s*(?:台|套|件|个|项|批)", value) else "process"
            elif section == "demand" or DIRECT_PURCHASE.search(value):
                role = "demand"
            else:
                role = "unknown"
            span = {"start": position + match.start(), "end": position + match.end(),
                    "role": role, "text": value, "mixed": mixed}
            spans.append(span)
            if role != "process":
                kept.append(value.strip())
        position += len(line)
    return {"text": "\n".join(kept), "spans": spans,
            "has_demand": any(s["role"] == "demand" for s in spans), "warnings": []}


def classify_correction(notice: NoticeInput) -> str:
    body = UNCHANGED.sub("", notice.body)
    attachments = [a.text for a in notice.attachments if a.text.strip()]
    if ATTACHMENT_CHANGE.search(body) and not attachments:
        return "uncertain"
    if PROCESS.search(body) and CHANGE.search(body) and not PROCUREMENT_CHANGE.search(body):
        if "仅" in body or re.search(r"其他内容不变|其余内容不变", notice.body) or not attachments:
            return "procedural"
    material = "\n".join([body, *attachments])
    if PROCUREMENT_CHANGE.search(material):
        return "substantive"
    if ATTACHMENT_CHANGE.search(material):
        return "substantive" if attachments else "uncertain"
    if PROCESS.search(material) and CHANGE.search(material):
        return "procedural"
    return "uncertain"
