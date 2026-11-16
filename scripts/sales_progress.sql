-- Run against bid_analysis. All statements in this file are read-only.
SELECT kind AS task_type, status, count(*) AS task_count
FROM public.sales_jobs_v1 GROUP BY kind,status ORDER BY kind,status;

SELECT status, count(*) AS projects
FROM public.sales_projects_v1 GROUP BY status ORDER BY projects DESC;

-- One row per currently running job; timestamps are Unix seconds.
SELECT id, kind, payload->>'project_key' AS project_key,
       progress->>'phase' AS phase, progress->>'sales_item' AS sales_item,
       attempts, round(extract(epoch FROM now())-updated_at) AS seconds_since_heartbeat,
       to_timestamp(lease_until) AS lease_expires_at, progress
FROM public.sales_jobs_v1 WHERE status='running' ORDER BY updated_at;

-- Throughput is completed task attempts, not distinct projects.
SELECT date_trunc('minute',to_timestamp(updated_at)) AS minute,
       count(*) AS completed_analysis_tasks
FROM public.sales_jobs_v1
WHERE kind='analyze' AND status='completed'
  AND updated_at>=extract(epoch FROM now()-interval '30 minutes')
GROUP BY 1 ORDER BY 1 DESC;

SELECT status,error,count(*) AS tasks
FROM public.sales_jobs_v1 WHERE status IN ('failed','retry')
GROUP BY status,error ORDER BY tasks DESC;

-- processed is scan progress, not completed analyses.
SELECT value->>'phase' AS phase, value->>'processed' AS scanned_projects,
       value->>'cursor' AS cursor, value->>'error' AS error
FROM public.sales_runtime_v1 WHERE key='history';

SELECT pid,state,wait_event_type,wait_event,
       now()-query_start AS elapsed,pg_blocking_pids(pid) AS blocked_by,
       left(query,600) AS query
FROM pg_stat_activity
WHERE datname=current_database() AND pid<>pg_backend_pid()
  AND (state<>'idle' OR cardinality(pg_blocking_pids(pid))>0)
ORDER BY query_start;
