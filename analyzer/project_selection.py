"""Select one current primary notice per explicit procurement lot."""

import re
from collections import defaultdict

from analyzer.procurement import classify_correction
from analyzer.schemas import NoticeInput

PRIMARY_STAGES = ("采购公告", "采购意向")
ANALYSIS_STAGES = frozenset((*PRIMARY_STAGES, "更正公告"))
SELECTION_VERSION = "project-primary-v1"


def _date_key(notice):
    return (notice.publish_at or "", notice.notice_id)


def _lot(notice):
    if notice.lot_key:
        return notice.lot_key
    lots = {lot.lot_no for lot in notice.lots if lot.lot_no}
    return next(iter(lots)) if len(lots) == 1 else None


def _missing(project_key, warnings=None):
    return {"unit_key": "project", "main": None, "corrections": [],
            "ignored_correction_ids": [], "warnings": warnings or ["缺少采购公告或采购意向，采购材料待补齐"],
            "selection_reason": "项目进展不作为采购需求输入", "status": "missing_primary",
            "round_key": None, "lot_key": None, "project_key": project_key,
            "selection_version": SELECTION_VERSION}


def _select_group(project_key, notices):
    valid = [n for n in notices if n.revoke_status == "active"]
    primaries = [n for n in valid if n.notice_type in PRIMARY_STAGES]
    if not primaries:
        return [_missing(project_key)]
    warnings = []
    if any(not n.publish_at for n in primaries):
        warnings.append("部分主公告缺少发布日期；同阶段按已有日期及公告ID稳定选择，需复核顺序")
    units = defaultdict(list)
    for notice in primaries:
        units[_lot(notice)].append(notice)
    plans = []
    for lot_key, candidates in sorted(units.items(), key=lambda item: item[0] or ""):
        local_warnings = list(warnings)
        current_round = None
        explicit = [n for n in candidates if n.round_key]
        if explicit:
            current_round = max(explicit, key=_date_key).round_key
            if any(n.round_key is None for n in candidates):
                local_warnings.append("有主公告缺少轮次关联，未混入已明确的当前采购轮次")
            candidates = [n for n in candidates if n.round_key == current_round]
        elif any(re.search(r"重新招标|重新采购|二次|三次|第[二三四五六七八九十2-9]次", n.title)
                 for n in candidates):
            local_warnings.append("标题包含重新采购或多轮次线索，但来源未提供轮次关联，需复核")
        preferred = [n for n in candidates if n.notice_type == "采购公告"]
        main = max(preferred or candidates, key=_date_key)
        if len({lot.lot_no for lot in main.lots if lot.lot_no}) > 1:
            local_warnings.append("主公告共用多个标包，保留标包列表按同一份公告分析，不重复投递")
        corrections, ignored = [], []
        for correction in sorted((n for n in valid if n.notice_type == "更正公告"), key=_date_key):
            association_warnings = []
            if correction.related_notice_id is not None:
                associated = correction.related_notice_id == main.notice_id
            else:
                associated = bool(project_key and correction.project_key == main.project_key)
                if current_round:
                    associated = associated and correction.round_key == current_round
                elif correction.round_key:
                    associated = False
                correction_lot = _lot(correction)
                if lot_key:
                    associated = associated and correction_lot == lot_key
                elif correction_lot:
                    associated = False
                associated = associated and bool(main.publish_at and correction.publish_at
                                                  and correction.publish_at >= main.publish_at)
                if associated:
                    association_warnings.append(f"更正公告 {correction.notice_id} 由项目、轮次和时间推断关联，需复核")
            if not associated:
                ignored.append(correction.notice_id)
                continue
            if (correction.related_notice_id == main.notice_id and correction.publish_at
                    and main.publish_at and correction.publish_at < main.publish_at):
                association_warnings.append(f"更正公告 {correction.notice_id} 引用了主公告但发布日期更早，需核验日期")
            # Explicit target is authoritative, but never cross an explicitly different round/lot.
            if (correction.round_key and current_round and correction.round_key != current_round
                    or _lot(correction) and lot_key and _lot(correction) != lot_key):
                ignored.append(correction.notice_id)
                continue
            correction_type = classify_correction(correction)
            if correction_type == "procedural":
                ignored.append(correction.notice_id)
            else:
                corrections.append(correction)
                local_warnings.extend(association_warnings)
                if correction_type == "uncertain":
                    local_warnings.append(f"更正公告 {correction.notice_id} 内容或附件不完整，实质变更待核验")
        plans.append({"unit_key": f"lot:{lot_key}" if lot_key else "project", "main": main,
                      "corrections": corrections, "ignored_correction_ids": ignored,
                      "warnings": list(dict.fromkeys(local_warnings)),
                      "selection_reason": f"采购公告优先于采购意向；已选有效{main.notice_type}，同阶段按发布日期选择最新一份",
                      "status": "selected", "round_key": current_round, "lot_key": lot_key,
                      "project_key": project_key, "selection_version": SELECTION_VERSION})
    return plans


def select_project(notices: list[NoticeInput]) -> list[dict]:
    """Group by real project key; an unlinked notice never joins another project."""
    groups = defaultdict(list)
    for notice in notices:
        groups[notice.project_key or f"notice:{notice.notice_id}"].append(notice)
    result = []
    for key, group in sorted(groups.items()):
        result.extend(_select_group(key, group))
    return result
