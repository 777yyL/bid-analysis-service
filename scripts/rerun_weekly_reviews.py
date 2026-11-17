"""Preview or enqueue a full reanalysis of projects from the previous calendar week."""

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from analyzer.config import Settings
from analyzer.sales_service import SalesService
from analyzer.sales_sync import ALL, EARLY, SourceSync, time_expression
from analyzer.source import SourceError
from analyzer.source_tables import SourceProject
from analyzer.store import Store


def report(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def project_keys(sync, start, end, zone):
    with sync.remote_connection() as conn:
        tables, *_ = sync.schema(conn)
        first_seen, _ = time_expression(tables["notices"], "n", "first_seen_at", (), zone)
        rows = conn.execute(
            f"""SELECT n.project_key_name AS project_key, max({first_seen}) AS latest_seen
            FROM public.notices n
            JOIN public.mv_project p ON p.project_key = n.project_key_name
            WHERE {first_seen} >= %s AND {first_seen} < %s
              AND n.revoke_status = 'active'
              AND n.channel = ANY(%s) AND p.channel = ANY(%s)
            GROUP BY n.project_key_name
            ORDER BY latest_seen DESC, n.project_key_name""",
            (start, end, list(EARLY), list(ALL if sync.cfg["allow_later_stages"] else EARLY)),
        ).fetchall()
    return [row["project_key"] for row in rows]


def submit(bootstrap, keys, batch_size):
    failed = 0
    with httpx.Client(base_url="http://127.0.0.1:8100", timeout=120, trust_env=False,
                      headers={"X-API-Key": bootstrap.analyzer_api_key.get_secret_value()}) as client:
        for start in range(0, len(keys), batch_size):
            batch = keys[start:start + batch_size]
            number = start // batch_size + 1
            try:
                response = client.post("/api/v3/analysis/start", json={
                    "mode": "history", "project_keys": batch, "force": True
                })
                response.raise_for_status()
                jobs = response.json()["items"]
                if len(jobs) != len(batch):
                    raise ValueError("接口返回任务数与提交项目数不一致")
                for key, job in zip(batch, jobs, strict=True):
                    forced = job["payload"].get("force") is True
                    failed += not forced
                    report(event="task" if forced else "active_task_not_forced", batch=number,
                           project_key=key, job_id=job["id"], status=job["status"])
                print(f"已提交 {start + len(batch)}/{len(keys)}，批次 {number}", file=sys.stderr)
            except httpx.HTTPStatusError as error:
                report(event="batch_error", batch=number, http_status=error.response.status_code,
                       project_keys=batch)
                return 1
            except (httpx.RequestError, ValueError, KeyError, TypeError) as error:
                report(event="batch_unverified", batch=number, error=type(error).__name__, project_keys=batch)
                return 1
    return int(failed > 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, help="起始日期（包含）")
    parser.add_argument("--end", type=date.fromisoformat, help="截止日期（不包含）")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--execute", action="store_true", help="实际提交，默认只预览")
    args = parser.parse_args()
    if bool(args.start) != bool(args.end) or (args.start and args.start >= args.end):
        parser.error("--start / --end 必须成对提供，且起始日期早于截止日期")
    if not 1 <= args.batch_size <= 500:
        parser.error("--batch-size 必须在 1 到 500 之间")

    bootstrap = Settings()
    store = Store(bootstrap.database_url, bootstrap.analysis_schema)
    try:
        service = SalesService(store, bootstrap)
        cfg = service.settings()
        zone = ZoneInfo(cfg["source_timezone"])
        today = datetime.now(zone).date()
        end_day = args.end or today - timedelta(days=today.weekday())
        start_day = args.start or end_day - timedelta(days=7)
        sync = SourceSync(store, cfg)
        if not sync.status()["ready"]:
            raise ValueError("当前数据源尚未建立本地同步记录")
        keys = project_keys(sync, datetime.combine(start_day, time.min, zone),
                            datetime.combine(end_day, time.min, zone), zone.key)
        with store.session() as session:
            ready = set(session.scalars(select(SourceProject.project_key).where(
                SourceProject.source_scope == sync.scope, SourceProject.in_scope.is_(True)
            )))
        available = [key for key in keys if key in ready]
        report(event="summary", start=start_day.isoformat(), end=end_day.isoformat(),
               source_projects=len(keys), eligible_local=len(available), not_ready=len(keys)-len(available))
        for key in keys:
            if key not in ready:
                report(event="source_not_ready", project_key=key)
        if not args.execute:
            return 0
        if not available:
            return int(bool(keys))
        if not bootstrap.worker_enabled or not cfg["engine_enabled"] or cfg["paused"]:
            raise ValueError("请启用分析引擎和工作进程，并取消暂停调度")
        if not cfg["review_enabled"] or cfg["low_score_review_enabled"]:
            raise ValueError("请先开启模型复核并关闭低分复核，确保全部召回候选均送审")
        if not cfg["review_base_url"] or not cfg["review_model"]:
            raise ValueError("请先配置复核模型地址和模型名称")
        result = submit(bootstrap, available, args.batch_size)
        return int(bool(result or len(available) != len(keys)))
    except (SourceError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
