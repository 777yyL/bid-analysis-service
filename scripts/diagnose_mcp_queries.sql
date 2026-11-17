-- MCP 查询耗时诊断：只读，不创建索引、不修改业务数据。
-- 在主数据库执行。将 bid_analysis 替换成 .env.shared 的 ANALYSIS_SCHEMA。
-- 每段单独执行两次，保留 Execution Time、Buffers 和完整执行计划。
-- 默认最近七个自然日（含今天）、按公告发布时间筛选。
-- 此诊断未加应用的 _source_revision 配置指纹过滤，统计是该 schema 的全部来源。
-- 用于定位瓶颈，不应当作生产 MCP 的精确业务总数。
SET TIME ZONE 'Asia/Shanghai';

-- 0. 定位结果表和已有索引；确认 schema 后再执行下面各段。
SELECT schemaname, tablename FROM pg_tables
WHERE tablename IN ('sales_projects_v1','sales_matches_v1','sales_items_v1');
SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes
WHERE (schemaname='public' AND tablename='notices')
   OR (schemaname='bid_analysis' AND tablename IN ('sales_projects_v1','sales_matches_v1','sales_items_v1'))
ORDER BY schemaname,tablename,indexname;

-- 1. 主库：日期范围内的公告和去重项目（包含尚未分析的项目）。
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS notices, count(DISTINCT project_key_name) AS projects
FROM public.notices
WHERE publish_at >= CURRENT_DATE - INTERVAL '6 days'
  AND publish_at < CURRENT_DATE + INTERVAL '1 day';

-- 2. 结果库：仅已有匹配结果的项目总数，不读取采购正文/任务历史。
-- matched=已匹配，no_match=未匹配，needs_review=已召回但仍待复核。
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.status,count(*) AS projects
FROM bid_analysis.sales_projects_v1 p
WHERE p.status IN ('matched','no_match','needs_review')
  AND EXISTS (
    SELECT 1 FROM public.notices n
    WHERE n.project_key_name=p.project_key
      AND n.publish_at >= CURRENT_DATE - INTERVAL '6 days'
      AND n.publish_at < CURRENT_DATE + INTERVAL '1 day'
  )
GROUP BY p.status;

-- 3. 项目分页：仅查询上述范围的前 20 条。
-- 公告日期用于圈定范围，此诊断分页按分析结果更新时间倒序。
EXPLAIN (ANALYZE, BUFFERS)
SELECT p.project_key,p.title,p.channel,p.status,p.updated_at
FROM bid_analysis.sales_projects_v1 p
WHERE p.status IN ('matched','no_match','needs_review')
  AND EXISTS (
    SELECT 1 FROM public.notices n
    WHERE n.project_key_name=p.project_key
      AND n.publish_at >= CURRENT_DATE - INTERVAL '6 days'
      AND n.publish_at < CURRENT_DATE + INTERVAL '1 day'
  )
ORDER BY p.updated_at DESC,p.project_key DESC
LIMIT 20;

-- 4. 标签统计：同一项目多个标签只计一次；排除旧结果、旧销售项版本和禁用项。
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS analyzed_projects,
       count(*) FILTER (WHERE EXISTS (
           SELECT 1
           FROM bid_analysis.sales_matches_v1 m
           JOIN bid_analysis.sales_items_v1 i ON i.id=m.sales_item_id
           WHERE m.project_key=p.project_key AND m.revision=p.revision
             AND m.status='confirmed' AND i.enabled=true AND m.item_version=i.version
       )) AS hit_projects
FROM bid_analysis.sales_projects_v1 p
WHERE p.status IN ('matched','no_match','needs_review')
  AND EXISTS (
    SELECT 1 FROM public.notices n
    WHERE n.project_key_name=p.project_key
      AND n.publish_at >= CURRENT_DATE - INTERVAL '6 days'
      AND n.publish_at < CURRENT_DATE + INTERVAL '1 day'
  );

-- 5. 销售项排名：数据库聚合，不下载全量项目到 Agent。
EXPLAIN (ANALYZE, BUFFERS)
SELECT i.id,i.name,count(DISTINCT p.project_key) AS project_count
FROM bid_analysis.sales_projects_v1 p
JOIN bid_analysis.sales_matches_v1 m ON m.project_key=p.project_key AND m.revision=p.revision
JOIN bid_analysis.sales_items_v1 i ON i.id=m.sales_item_id AND i.version=m.item_version
WHERE p.status IN ('matched','no_match','needs_review')
  AND m.status='confirmed' AND i.enabled=true
  AND EXISTS (
    SELECT 1 FROM public.notices n
    WHERE n.project_key_name=p.project_key
      AND n.publish_at >= CURRENT_DATE - INTERVAL '6 days'
      AND n.publish_at < CURRENT_DATE + INTERVAL '1 day'
  )
GROUP BY i.id,i.name
ORDER BY project_count DESC,i.id
LIMIT 20;

-- 6. 可选：查询卡住时，在另一个数据库窗口执行，查看是否在等待锁或 IO。
-- 不返回 query 原文，避免包含业务文本或密钥。
SELECT pid,application_name,state,wait_event_type,wait_event,
       now()-query_start AS elapsed,pg_blocking_pids(pid) AS blocking_pids
FROM pg_stat_activity
WHERE datname=current_database() AND state='active' AND pid<>pg_backend_pid()
ORDER BY query_start;
