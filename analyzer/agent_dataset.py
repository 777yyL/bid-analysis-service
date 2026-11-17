"""SQL-backed MCP cohorts: stored analysis results, never daily source reconstruction."""
from contextlib import contextmanager
from time import time

from sqlalchemy import text

from analyzer.sales_categories import category_path
from analyzer.sales_reporting import day_bounds
from analyzer.sales_store import vector_profile
from analyzer.source import SourceError
from analyzer.store import digest


@contextmanager
def dataset(queries, filters):
    cfg = queries.settings()
    with queries.store.engine.connect() as conn:
        conn.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        yield AnalyzedDataset(conn, cfg, queries.store.schema, filters)


class AnalyzedDataset:
    def __init__(self, conn, cfg, schema, filters):
        self.conn, self.cfg, self.filters = conn, cfg, filters
        columns = dict(conn.execute(text("""SELECT column_name,data_type
            FROM information_schema.columns WHERE table_schema='public' AND table_name='notices'""")).all())
        required = {'notice_id', 'project_key_name', 'publish_at', filters.time_basis}
        if not required <= columns.keys():
            raise SourceError('MCP 查询缺少 notices 字段：' + ', '.join(sorted(required-columns.keys())))
        kind = columns[filters.time_basis]
        if kind not in ('timestamp with time zone', 'timestamp without time zone'):
            raise SourceError('所选时间字段必须是 timestamp 类型')
        self.region_supported = 'region_code' in columns
        if filters.region_code and not self.region_supported:
            raise SourceError('主库未提供 notices.region_code，不能按地区统计或筛选')
        start, _ = day_bounds(filters.start_date, kind, cfg['source_timezone'])
        _, end = day_bounds(filters.end_date, kind, cfg['source_timezone'])
        self.params = dict(start=start, end=end, scope=digest(cfg['source_dsn']), timezone=cfg['source_timezone'])
        prefix = conn.dialect.identifier_preparer.quote_schema(schema or 'public') + '.'
        # Identifiers below are fixed or validated enums; user values remain bound parameters.
        clock = 'n.' + filters.time_basis
        local_date = f"({clock} AT TIME ZONE :timezone)::date" if kind == 'timestamp with time zone' else f'{clock}::date'
        region = "coalesce((array_agg(n.region_code ORDER BY " + clock + " DESC,n.notice_id DESC))[1],'')" if self.region_supported else "''::text"
        project_conditions = ["p.status IN ('matched','no_match','needs_review')", "p.material->>'_source_revision' = :scope"]
        if filters.analysis_status:
            project_conditions.append('p.status=:analysis_status')
            self.params['analysis_status'] = filters.analysis_status
        if filters.channel:
            project_conditions.append('p.channel=:channel')
            self.params['channel'] = filters.channel
        if filters.q:
            project_conditions.append("strpos(lower(coalesce(p.title,'') || ' ' || p.project_key),lower(:q))>0")
            self.params['q'] = filters.q
        tag_conditions = ["m.status='confirmed'", 'i.enabled=true', 'm.item_version=i.version']
        for attr, condition in (
            ('sales_item_id', 'm.sales_item_id=:sales_item_id'),
            ('product', 'EXISTS (SELECT 1 FROM json_array_elements_text(i.products) v WHERE v.value=:product)'),
        ):
            if getattr(filters, attr):
                tag_conditions.append(condition)
                self.params[attr] = getattr(filters, attr)
        if filters.category:
            # Match the same explicit hierarchy separators as the UI, including parent paths.
            parts = category_path(filters.category)
            tag_conditions.append(r"""EXISTS (SELECT 1 FROM json_array_elements_text(i.categories) c
                WHERE (ARRAY(SELECT btrim(segment) FROM unnest(regexp_split_to_array(c.value,
                    '\s*(?:/|／|>|＞|›|→)\s*')) segment WHERE btrim(segment)<>''))[1:category_depth]
                    = CAST(:category_path AS text[]))""")
            # A slice colon would otherwise be interpreted as a SQLAlchemy bind name.
            tag_conditions[-1] = tag_conditions[-1].replace('[1:category_depth]', '[1 : CAST(:category_depth AS integer)]')
            self.params.update(category_path=parts, category_depth=len(parts))
            if not parts:
                tag_conditions.append('false')
        if filters.model_confirmed_only:
            tag_conditions.append("m.method IN ('model','model_backfill','model_auto')")
        if filters.min_score is not None:
            tag_conditions.append('m.score>:min_score')
            self.params['min_score'] = filters.min_score
        if cfg['retrieval_backend'] == 'local':
            tag_conditions.extend(['i.vector_profile=:vector_profile', 'i.vector_version=i.version'])
            self.params['vector_profile'] = vector_profile(cfg)
        selected_conditions = []
        if filters.region_code:
            selected_conditions.append('b.region_code=:region_code')
            self.params['region_code'] = filters.region_code
        has_tag = 'EXISTS (SELECT 1 FROM tags t WHERE t.project_key=b.project_key)'
        tag_filter = bool(filters.sales_item_id or filters.category or filters.product or filters.model_confirmed_only or filters.min_score is not None)
        if filters.hit is False:
            selected_conditions.append('NOT ' + has_tag)
        elif filters.hit is True or tag_filter:
            selected_conditions.append(has_tag)
        self.cte = f"""WITH analyzed AS NOT MATERIALIZED (
            SELECT p.project_key,p.title,p.channel,p.status,p.revision,p.updated_at
            FROM {prefix}sales_projects_v1 p WHERE {' AND '.join(project_conditions)}
        ), period AS MATERIALIZED (
            SELECT n.project_key_name AS project_key,count(*) AS notices,
                array_agg(DISTINCT {local_date}) AS dates,max(n.publish_at) AS publish_at,
                max({clock}) AS sort_at,{region} AS region_code
            FROM public.notices n JOIN analyzed a ON a.project_key=n.project_key_name
            WHERE {clock}>=:start AND {clock}<:end GROUP BY n.project_key_name
        ), base AS MATERIALIZED (
            SELECT a.*,d.notices,d.dates,d.publish_at,d.sort_at,d.region_code
            FROM analyzed a JOIN period d USING(project_key)
        ), tags AS NOT MATERIALIZED (
            SELECT m.project_key,m.sales_item_id,m.score,m.method,m.status,m.updated_at,
                i.name,i.categories,i.products
            FROM {prefix}sales_matches_v1 m JOIN base b ON b.project_key=m.project_key AND b.revision=m.revision
            JOIN {prefix}sales_items_v1 i ON i.id=m.sales_item_id
            WHERE {' AND '.join(tag_conditions)}
        ), selected AS NOT MATERIALIZED (
            SELECT b.*, {has_tag} AS hit FROM base b
            WHERE {' AND '.join(selected_conditions) or 'true'}
        ) """

    def execute(self, query, **params):
        return self.conn.execute(text(self.cte + query), {**self.params, **params}).mappings()

    def counts(self):
        return dict(self.execute("""SELECT count(*) AS all,coalesce(sum(notices),0)::bigint AS notices,
            count(*) FILTER(WHERE status IN ('matched','no_match')) AS analyzed,
            count(*) FILTER(WHERE status='needs_review') AS review_pending,
            count(*) FILTER(WHERE hit) AS matched,
            count(*) FILTER(WHERE status IN ('matched','no_match') AND NOT hit) AS without_confirmed_tags,
            count(*) FILTER(WHERE status='no_match') AS no_match
            FROM selected""").one())

    def metadata(self, counts):
        return {'filters':self.filters.model_dump(mode='json'), 'timezone':self.cfg['source_timezone'],
            'time_source':'public.notices.'+self.filters.time_basis, 'count_unit':'distinct project_key',
            'scope':'analyzed_projects', 'included_statuses':['matched','no_match','needs_review'],
            'region_supported':self.region_supported, 'filtered':counts, 'generated_at':time(),
            'consistency':'本次工具内使用同一只读快照；结果为当前已存分析状态，不是历史状态回放',
            'scope_note':'只包含已有匹配结论或待复核的项目；日期范围内至少有一条公告。未分析、缺材料、等待提取不在分母内。',
            'region_note':'地区取日期范围内按所选时间字段排序最新公告的 region_code；阶段和标题取已存分析结果。',
            'score_note':'标签条件作用于同一标签，min_score 为严格大于；相似度不是概率'}

    def search(self, page, page_size):
        return [dict(row) for row in self.execute("""SELECT s.*,
            (SELECT count(*) FROM tags t WHERE t.project_key=s.project_key) AS match_total,
            coalesce((SELECT json_agg(row_to_json(t)) FROM (
                SELECT * FROM tags WHERE project_key=s.project_key
                ORDER BY score DESC,sales_item_id LIMIT 30
            ) t),'[]'::json) AS matches
            FROM selected s ORDER BY sort_at DESC,project_key DESC LIMIT :limit OFFSET :offset""",
            limit=page_size, offset=(page-1)*page_size)]

    def aggregate(self, group_by, limit):
        if group_by == 'region_code' and not self.region_supported:
            raise SourceError('主库未提供 notices.region_code，不能按地区统计')
        if group_by == 'sales_item':
            source = 'SELECT s.project_key,t.sales_item_id AS value,t.name AS label FROM selected s JOIN tags t USING(project_key)'
        elif group_by in ('category', 'product'):
            column = 'categories' if group_by == 'category' else 'products'
            source = f'SELECT s.project_key,v.value,v.value AS label FROM selected s JOIN tags t USING(project_key) CROSS JOIN LATERAL json_array_elements_text(t.{column}) v'
        elif group_by == 'date':
            source = 'SELECT s.project_key,d::text AS value,d::text AS label FROM selected s CROSS JOIN LATERAL unnest(s.dates) d'
        else:
            column = {'channel':'channel', 'region_code':'region_code', 'analysis_status':'status'}[group_by]
            source = f"SELECT project_key,coalesce(nullif({column},''),'未提供') AS value,coalesce(nullif({column},''),'未提供') AS label FROM selected"
        rows = list(self.execute(f""", groups AS (
            SELECT value,label,count(DISTINCT project_key) AS project_count FROM ({source}) entries GROUP BY value,label
            ) SELECT *,count(*) OVER() AS total_groups FROM groups ORDER BY project_count DESC,value LIMIT :limit""", limit=limit))
        total = rows[0]['total_groups'] if rows else 0
        return [{k:r[k] for k in ('value','label','project_count')} for r in rows], total
