-- Execute on the analysis database only. These queries do not write data.
SELECT id, status, window_start AS "批次开始", window_end AS "同步截止",
       read_start AS "重叠读取起点", counts, attempts, error,
       to_timestamp(started_at) AS "开始时间", to_timestamp(completed_at) AS "结束时间"
FROM public.sales_sync_batches ORDER BY started_at DESC LIMIT 30;

SELECT key, value FROM public.sales_runtime_v1 WHERE key LIKE 'source_checkpoint:%';

SELECT batch_id, kind, count(*) FROM public.sales_sync_records
GROUP BY batch_id, kind ORDER BY batch_id DESC;
