"""Project-level hardware dictionary tags; deliberately independent of software analysis."""
from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from analyzer.schemas import StrictModel
from analyzer.source import SourceError

HardwareGroup = Literal['tag_l1', 'tag_l2', 'buyer', 'winner']


class HardwareFilter(StrictModel):
    tag_l1: str = Field(default='', max_length=200, description='硬件一级分类，精确匹配')
    tag_l2: str = Field(default='', max_length=200, description='硬件二级关键词，精确匹配')
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode='after')
    def period(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError('请同时填写起止日期，或都省略以查看全部硬件项目')
        if self.start_date and (not 0 <= (self.end_date-self.start_date).days < 366 or self.end_date == date.max):
            raise ValueError('起止日期均含，范围应为 1～366 天')
        return self


def hardware_report(engine, filters, group_by, limit):
    params = {'limit': limit}
    terms = []
    for field in ('tag_l1', 'tag_l2'):
        if value := getattr(filters, field).strip():
            terms.append(f't.{field}=:{field}')
            params[field] = value
    where = ' WHERE ' + ' AND '.join(terms) if terms else ''
    ctes = [f'tags AS (SELECT project_key,tag_l1,tag_l2 FROM public.project_product_tag t{where})',
            'tagged AS (SELECT DISTINCT project_key FROM tags)']
    period = ''
    if filters.start_date:
        params.update(start=filters.start_date, end=filters.end_date+timedelta(days=1))
        period = ' AND n.publish_at >= :start AND n.publish_at < :end'
    if group_by in ('buyer','winner') or period:
        cols = {'buyer': ',n.buyer', 'winner': ',n.notice_id,n.winner,n.channel,n.title,n.notice_type'}.get(group_by, '')
        ctes.append(f"""events AS (SELECT p.project_key{cols} FROM tagged p
            JOIN public.notices n ON n.project_key_name=p.project_key WHERE n.revoke_status='active'{period})""")
    ctes.append('cohort AS (SELECT DISTINCT project_key FROM events)' if period else 'cohort AS (SELECT project_key FROM tagged)')
    if group_by in ('tag_l1','tag_l2'):
        inner = 'SELECT t.project_key,t.tag_l1 AS parent,t.tag_l2 AS value' if group_by=='tag_l2' else 'SELECT t.project_key,NULL::text AS parent,t.tag_l1 AS value'
        ctes.append(f'pairs AS (SELECT DISTINCT * FROM ({inner} FROM tags t JOIN cohort USING(project_key)) x)')
    else:
        if group_by == 'winner':
            ctes.append("""results AS (SELECT * FROM events WHERE channel='结果公告'
                AND concat_ws(' ',title,notice_type) !~ '候选|入围候选|废标|流标|终止')""")
            # One left join: prefer meaningful lot winners for this notice, otherwise fall back once.
            ctes.append("""names AS (SELECT r.project_key,coalesce(l.name,btrim(r.winner)) AS value
                FROM results r LEFT JOIN LATERAL (SELECT DISTINCT btrim(winner) AS name FROM public.lots
                    WHERE notice_id=r.notice_id AND btrim(winner) NOT IN ('','/','-','无','暂无','详见附件','详见公告')) l ON true)""")
        elif group_by == 'buyer':
            ctes.append('names AS (SELECT project_key,btrim(buyer) AS value FROM events)')
        else:
            raise ValueError('未知硬件分析维度')
        ctes.append("""pairs AS (SELECT DISTINCT project_key,NULL::text AS parent,value FROM names
            WHERE value NOT IN ('','/','-','无','暂无','详见附件','详见公告'))""")
    stmt = 'WITH '+', '.join(ctes)+""", ranked AS (
        SELECT parent,value,count(*) AS project_count,min(project_key) AS example_project_key
        FROM pairs GROUP BY parent,value), top_groups AS (
        SELECT * FROM ranked ORDER BY project_count DESC,parent,value LIMIT :limit)
        SELECT (SELECT count(*) FROM cohort) AS total_projects,
            (SELECT count(DISTINCT project_key) FROM pairs) AS attributed_projects,
            (SELECT count(*) FROM ranked) AS total_groups,
            coalesce((SELECT json_agg(g ORDER BY project_count DESC,parent,value) FROM top_groups g),'[]') AS groups"""
    try:
        with engine.connect() as conn:
            result = dict(conn.execute(text(stmt),params).mappings().one())
    except DBAPIError as exc:
        if getattr(exc.orig, 'sqlstate', '') in ('42P01','42703','42501'):
            raise SourceError('硬件统计所需表/字段不可读。请检查 public.project_product_tag、public.notices 和中标人统计所需的 public.lots 及 SELECT 权限。') from None
        raise
    result.update(group_by=group_by, unattributed_projects=result['total_projects']-result['attributed_projects'],
                  has_more=result['total_groups']>limit, filters=filters.model_dump(mode='json'),
                  source='public.project_product_tag',
                  scope='当前硬件标签；不要求软件分析。指定日期仅取期间发布的有效公告及其主体，不代表首次命中日期。',
                  attribution='每组每项目只计一次；示例键可查详情。中标人为结果公告标段优先、公告兜底的记录名称；项目关联不证明中标人供应了该硬件。')
    return result
