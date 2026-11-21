"""Shortlist actionable software opportunities from current confirmed tags."""
from sqlalchemy import text


CARD_COLUMNS = {
    'buyer': ('buyer',),
    'source_url': ('source_url', 'notice_url', 'detail_url', 'url'),
    'budget': ('budget_amount', 'budget'),
    'deadline': ('bid_deadline', 'submission_deadline', 'deadline'),
}


def card_fields(columns, alias=''):
    """Whitelist optional source columns. Never fetch the full row to inspect JSON."""
    parts = []
    for name, choices in CARD_COLUMNS.items():
        present = [f"nullif(btrim({alias}{col}::text),'')" for col in choices if col in columns]
        expr = 'coalesce(' + ','.join(present + ['NULL::text']) + ')' if present else 'NULL::text'
        parts.append(f'{expr} AS {name}')
    return ', '.join(parts)


def opportunity_page(data, filters, page, page_size):
    prefix = data.schema_prefix
    clock = 'n.' + filters.time_basis
    conditions = [
        "n.revoke_status='active'", "n.channel IN ('采购公告','采购意向','更正公告')",
        "n.has_procurement_content IS TRUE",
        f'{clock} >= :start', f'{clock} < :end',
    ]
    params = dict(data.params, limit=page_size+1, offset=(page-1)*page_size)
    stage = "v.channel IN ('采购公告','采购意向','更正公告')"
    if filters.channel:
        stage += ' AND v.channel=:channel'
    if filters.q:
        conditions.append("strpos(lower(p.title || ' ' || p.project_key),lower(:q))>0")
    region = 'n.region_code' if data.region_supported else 'NULL::text'
    if filters.region_code:
        code = filters.region_code
        if len(code)==6 and code.isdigit() and code.endswith('0000'):
            conditions.append("n.region_code LIKE :region_prefix")
            params['region_prefix'] = code[:2]+'%'
        else:
            conditions.append('n.region_code=:region_code')
    # Start from confirmed, current-version tags. Carry notice keys through sorting;
    # look up card fields and the demand snippet only for the requested page.
    stmt = f"""WITH tags AS NOT MATERIALIZED ({data.tag_sql}),
        recent AS (
            SELECT DISTINCT ON (p.project_key) p.project_key,p.revision,p.title,p.channel AS analyzed_channel,
                v.channel,n.notice_id,{region} AS region_code,{clock} AS event_at
            FROM (SELECT DISTINCT project_key,revision FROM tags) t
            JOIN {prefix}sales_projects_v1 p ON p.project_key=t.project_key AND p.revision=t.revision
            JOIN public.mv_project v ON v.project_key=p.project_key AND {stage}
            JOIN public.notices n ON n.project_key_name=p.project_key
            WHERE p.status IN ('matched','needs_review') AND p.material->>'_source_revision'=:scope
              AND {' AND '.join(conditions)}
              AND length(btrim(n.procurement_content))>0
            ORDER BY p.project_key,{clock} DESC,n.notice_id DESC
        ), page AS MATERIALIZED (
            SELECT * FROM recent ORDER BY event_at DESC,project_key DESC LIMIT :limit OFFSET :offset
        )
        SELECT a.*,n.publish_at,n.channel AS notice_channel,{card_fields(data.notice_columns,'n.')},
            left(n.procurement_content,360) AS procurement_excerpt,
            (SELECT json_agg(row_to_json(m) ORDER BY score DESC,sales_item_id) FROM (
                SELECT sales_item_id,name,score,method,status,updated_at,categories,products FROM tags
                WHERE project_key=a.project_key AND revision=a.revision
                ORDER BY score DESC,sales_item_id LIMIT 30
            ) m) AS matches
        FROM page a JOIN public.notices n ON n.notice_id=a.notice_id
        ORDER BY a.event_at DESC,a.project_key DESC"""
    rows = list(data.conn.execute(text(stmt),params).mappings())
    return [dict(r) for r in rows[:page_size]], len(rows)>page_size
