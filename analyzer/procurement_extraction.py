"""Extract procurement requirements before embedding, preserving clean-text offsets."""
import re

from analyzer.procurement import (
    CHANGE,
    DEMAND_ANCHOR,
    DEMAND_HEADING,
    PROCESS,
    PROCESS_HEADING,
    procurement_sections,
)

EXTRA_DEMAND = re.compile(r'主要功能(?:或目标)?|功能(?:模块|要求|描述)|标的(?:名称|信息)|需求描述|建设目标|实施内容|交付内容')
OUTSIDE = re.compile(r'联系方式|联系信息|采购人信息|采购代理机构|公告期限|其他补充事宜|商务要求|付款方式|合同条款|项目背景|背景介绍|历史沿革|业绩要求')
ADMIN = re.compile(r'^\s*(?:(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[、.．]?\s*)?(?:联系人|联系电话|联系方式|联系地址|地址|电话|采购人(?:名称|信息)?|采购代理机构|项目编号|项目名称|采购项目名称|采购项目资金落实情况|资金落实情况|预算金额|项目预算|最高限价|采购方式|公告期限)\s*[:：]')
FEATURE = re.compile(r'支持|提供|实现|具备|功能|模块|系统|平台|软件|设备|终端|集成|升级|改造|开发|管理|服务|监测|建设|数据|识别|预警|告警|控制|分析|查询|统计|记录|认证|授权|审批|预约|通行|监控|调度|采集|处理|传输|存储|展示')
FUNCTIONAL = re.compile(r'^\s*(?:[\d、.．（）()\s]+)?(?:支持|实现|提供|具备).{0,100}(?:功能|模块|系统|软件|平台|管理)')
PURCHASE_SUFFIX = re.compile(r'(?:系统|软件|平台|模块|设备|终端)(?:的)?(?:采购|建设|开发|升级|改造)')
EXPERIENCE = re.compile(r'(?:供应商|投标人|应答人|响应人).{0,60}(?:业绩|资格|资质)(?!管理|系统|功能|模块)|业绩证明|类似项目证明')
REFERENCE = re.compile(r'^\s*(?:(?:采购|服务|技术|具体)(?:需求|内容|要求|范围)\s*[:：]?\s*)?(?:详见|参见|见|具体详见)\s*(?:附件|采购文件|招标文件|需求文件).*[。；;]?\s*$')
NUMBERING = re.compile(r'^\s*(?:#{1,6}\s*)?(?:第[一二三四五六七八九十\d]+章\s*|[一二三四五六七八九十]+[、.．]\s*|\d+(?:\.\d+)*[、.．\s]\s*)?')
FLOW_SECTION = re.compile(r'(?:采购|招标|比选|磋商|谈判|询价)文件(?:的)?(?:获取|领取|下载|购买|发售)|(?:获取|领取|下载|购买|发售)(?:采购|招标|比选)文件|发布公告的媒介|公告发布媒介|供应商操作指引')
FLOW_ACTION = re.compile(r'(?:供应商|投标人|应答人|响应人).{0,60}(?:登录|登陆|注册|账号|账户|成为合作伙伴)|(?:登录|登陆).{0,30}(?:采购平台|交易平台|网站)|(?:下载|获取|领取|查阅).{0,35}(?:操作手册|操作指南)|平台服务热线|平台注册审核|平台操作流程')
EXPLICIT_PURCHASE = re.compile(r'(?<!参加)(?<!参与)(?:本次|本项目|本标段|本合同包)(?:拟|将|计划|需要|需)?(?:采购|购置|购入|租赁|建设|开发|升级|改造)\s*(?=[^\s。；;，、:：])|(?:采购范围|采购内容|采购需求|建设内容|招标范围)\s*[:：]')
FUNCTION_START = re.compile(r'^\s*(?:支持|实现|提供|具备|应支持|须支持|需支持|可支持|能够|系统(?:应|需|须)?(?:支持|提供))')
LOGICAL_START = re.compile(r'(?<=\n)|(?<!\S)#{1,6}\s+|(?<=\s\s)\d+(?:\.\d+)+[、.．]?\s*(?=[\u3400-\u9fff])')
EXTRACTION_VERSION = 'procurement-demand-v2'


def _logical_lines(text):
    starts = sorted({0, len(text), *[match.start() for match in LOGICAL_START.finditer(text)]})
    return [(start, end, text[start:end]) for start, end in zip(starts, starts[1:])]


def requirement_chunks(extraction, size):
    for segment in extraction['segments']:
        for offset in range(0, len(segment['text']), size - min(200, size // 5)):
            value = segment['text'][offset:offset+size]
            start = segment['start'] + offset
            yield {'content':value, 'char_start':start, 'char_end':start+len(value),
                   'section_role':segment['role']}


def extract_requirements(text, correction=False):
    classified, context = [], 'unknown'
    for start, end, line in _logical_lines(text):
        stripped = re.sub(r'\\([*#_])', r'\1', line.strip())
        prefix = re.split(r'[:：]', stripped, maxsplit=1)[0]
        functional_heading = FUNCTION_START.search(NUMBERING.sub('', prefix).lstrip(' *_'))
        looks_heading = (not functional_heading and len(prefix) <= 70 and not re.search(r'[。；;]', prefix)
                         and (len(stripped) <= 100 or ':' in stripped or '：' in stripped))
        if looks_heading:
            demand = bool(DEMAND_HEADING.search(prefix) or EXTRA_DEMAND.search(prefix))
            outside = bool(PROCESS_HEADING.search(prefix) or OUTSIDE.search(prefix) or FLOW_SECTION.search(prefix))
            if demand and outside:
                context = 'uncertain'
            elif outside:
                context = 'outside'
            elif demand:
                context = 'demand'
        for span in procurement_sections(line)['spans']:
            classified.append(({**span, 'start': start+span['start'], 'end': start+span['end']}, context))

    selected, warnings = [], []
    for span, context in classified:
        value = span['text']
        cleaned = NUMBERING.sub('', value, count=1).strip(' #：:。；;\t\r\n')
        if not cleaned or re.fullmatch(r'[\s|:\-+]+', cleaned):
            continue
        if REFERENCE.fullmatch(cleaned):
            warnings.append('采购需求仅引用附件或采购文件，未提取到具体内容；可检查附件采集开关及原文')
            continue
        if DEMAND_HEADING.fullmatch(cleaned) or EXTRA_DEMAND.fullmatch(cleaned):
            continue
        if ADMIN.search(value) or EXPERIENCE.search(value):
            continue
        process = bool(PROCESS.search(value))
        anchor = bool(DEMAND_ANCHOR.search(value) or PURCHASE_SUFFIX.search(value))
        functional = bool(FUNCTIONAL.search(value) or FUNCTION_START.search(cleaned.lstrip(' *_')))
        explicit = bool(EXPLICIT_PURCHASE.search(value))
        flow = bool(FLOW_ACTION.search(value))
        if context == 'outside' and not explicit:
            continue
        if flow and not explicit and not (context == 'demand' and functional):
            continue
        if process and not anchor and not (context == 'demand' and functional):
            continue
        if flow and explicit or context == 'outside':
            role = 'uncertain'
        elif process and anchor:
            role = 'uncertain'
        elif context == 'uncertain':
            if not (anchor or FEATURE.search(value) or correction and CHANGE.search(value)):
                continue
            role = 'uncertain'
        elif context == 'demand' or span['role'] == 'demand':
            role = 'demand'
        elif anchor:
            role = 'demand'
        elif FEATURE.search(value) or correction and CHANGE.search(value):
            role = 'uncertain'
        else:
            continue
        start, end = span['start'], span['end']
        # Only merge across whitespace. Removed clauses must never be reintroduced.
        if selected and selected[-1]['role'] == role and not text[selected[-1]['end']:start].strip():
            selected[-1]['end'] = end
            selected[-1]['text'] = text[selected[-1]['start']:end]
        else:
            selected.append({'start': start, 'end': end, 'text': text[start:end], 'role': role})
    kept = sum(len(s['text']) for s in selected)
    return {'version': EXTRACTION_VERSION, 'segments': selected, 'original_chars': len(text),
            'extracted_chars': kept, 'removed_chars': len(text)-kept,
            'demand_chars': sum(len(s['text']) for s in selected if s['role']=='demand'),
            'uncertain_chars': sum(len(s['text']) for s in selected if s['role']=='uncertain'),
            'warnings': list(dict.fromkeys(warnings))}
