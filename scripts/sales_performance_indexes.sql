-- Run each CREATE separately with auto-commit enabled, never inside BEGIN/COMMIT.
-- These indexes consume disk and I/O while building; application tables are retained.
-- Rollback: DROP INDEX CONCURRENTLY public.idx_sales_jobs_created_desc;
--           DROP INDEX CONCURRENTLY public.idx_sales_jobs_available;
--           DROP INDEX CONCURRENTLY public.idx_sales_project_source_updated;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sales_jobs_created_desc
ON public.sales_jobs_v1 (created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sales_jobs_available
ON public.sales_jobs_v1 (status,available_at,created_at)
WHERE status IN ('queued','retry','running');

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sales_project_source_updated
ON public.sales_projects_v1 ((material->>'_source_revision'),updated_at DESC)
WHERE status<>'stage_filtered';

ANALYZE public.sales_jobs_v1;
ANALYZE public.sales_projects_v1;
