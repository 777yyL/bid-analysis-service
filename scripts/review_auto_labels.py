"""Review existing automatically accepted project labels and persist the verdicts."""

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, cast, func, select

from analyzer.config import Settings
from analyzer.sales_models import ModelError, SalesModelClient, review_system_prompt
from analyzer.sales_service import SalesService
from analyzer.sales_store import SalesAudit, SalesItem, SalesMatch, SalesProject, record
from analyzer.sales_sync import EARLY
from analyzer.source_tables import SourceNotice
from analyzer.store import Store, digest


AUTO_METHODS = ("knowledge_auto", "model_auto")
RUN_LOCK = 715020261117
GUARDED_FIELDS = ("revision", "item_version", "method", "status", "score", "updated_at")


def report(**fields):
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


class RecordingReviewer(SalesModelClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.responses = []

    async def review(self, candidates, sources):
        decisions = await super().review(candidates, sources)
        self.responses.append({
            "input_evidence_keys": [source["key"] for source in sources],
            "decisions": decisions,
        })
        return decisions


def matches(scope):
    return (
        select(SalesMatch).join(SalesProject, SalesProject.project_key == SalesMatch.project_key)
        .join(SalesItem, SalesItem.id == SalesMatch.sales_item_id)
        .where(
            SalesMatch.status == "confirmed", SalesMatch.method.in_(AUTO_METHODS),
            SalesMatch.revision == SalesProject.revision,
            SalesMatch.item_version == SalesItem.version, SalesItem.enabled.is_(True),
            SalesProject.status != "stage_filtered",
            SalesProject.material["_source_revision"].as_string() == scope,
        )
    )


def load_target(service, scope, match_id):
    with service.store.session() as session:
        match = session.scalar(matches(scope).where(SalesMatch.id == match_id))
        if not match:
            return None
        project = session.get(SalesProject, match.project_key)
        item = session.get(SalesItem, match.sales_item_id)
        return record(match), dict(project.material), item.name, item.description


def save_result(service, scope, old, candidate, audit):
    with service.store.session.begin() as session:
        project = session.get(SalesProject, old["project_key"], with_for_update=True)
        match = session.get(SalesMatch, old["id"], with_for_update=True)
        item = session.get(SalesItem, old["sales_item_id"], with_for_update=True)
        if (
            not project or not match or not item or not item.enabled
            or project.status == "stage_filtered" or project.revision != old["revision"]
            or project.material.get("_source_revision") != scope or item.version != old["item_version"]
            or any(getattr(match, field) != old[field] for field in GUARDED_FIELDS)
        ):
            return False
        match.status = candidate["status"]
        match.method = "model_backfill"
        match.reason = candidate["reason"]
        match.evidence = candidate["evidence"]
        project.updated_at = time.time()
        session.add(SalesAudit(
            id=str(uuid.uuid4()), project_key=project.project_key, created_at=time.time(),
            payload={"event": "automatic_label_model_review", "previous": old, **audit},
        ))
        session.flush()
        service.update_project_status(session, project)
    return True


async def run(service, cfg, args):
    scope = digest(cfg["source_dsn"])
    end = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    start = end - timedelta(days=args.published_days) if args.published_days else None
    with service.store.session() as session:
        eligible = matches(scope)
        order = [SalesMatch.updated_at.desc(), SalesProject.updated_at.desc(), SalesMatch.id]
        if start is not None:
            published = cast(func.nullif(func.trim(SourceNotice.publish_at), ""), DateTime)
            recent = select(
                SourceNotice.project_key_name.label("project_key"),
                func.max(published).label("latest_publish_at"),
            ).where(
                SourceNotice.source_scope == scope, SourceNotice.channel.in_(EARLY),
                SourceNotice.revoke_status == "active", published >= start, published < end,
            ).group_by(SourceNotice.project_key_name).subquery()
            eligible = eligible.join(recent, recent.c.project_key == SalesMatch.project_key)
            order.insert(0, recent.c.latest_publish_at.desc())
        total = session.scalar(select(func.count()).select_from(eligible.subquery()))
        ids = session.scalars(
            eligible.with_only_columns(SalesMatch.id)
            .order_by(*order)
            .limit(args.limit)
        ).all()
    prompt = review_system_prompt(cfg.get("review_system_prompt", ""))
    run_id = str(uuid.uuid4())
    report(event="start", run_id=run_id, eligible=total, selected=len(ids), execute=args.execute,
           published_from=start, published_before=end if start is not None else None,
           model=cfg["review_model"], rpm=cfg["review_rpm"], evaluation_prompt=prompt)
    if not args.execute:
        return 0

    reviewer = service.client(cfg)
    totals = {"confirmed": 0, "excluded": 0, "missing_evidence": 0, "changed": 0, "failed": 0}
    consecutive_failures = 0
    for number, match_id in enumerate(ids, 1):
        target = load_target(service, scope, match_id)
        info = {"run_id": run_id, "index": number, "total": len(ids), "match_id": match_id}
        if target is None:
            totals["changed"] += 1
            report(event="changed", **info)
            continue
        old, material, name, description = target
        info.update(project_key=old["project_key"], sales_item_id=old["sales_item_id"],
                    sales_item_name=name, original_method=old["method"], similarity=old["score"])
        if not old["evidence"]:
            totals["missing_evidence"] += 1
            report(event="missing_evidence", **info)
            continue
        candidate = {
            "sales_item_id": old["sales_item_id"], "name": name, "description": description,
            "status": "candidate", "method": "model", "reason": "", "evidence": old["evidence"],
        }
        reviewer.responses.clear()
        try:
            await service.review_candidate(
                candidate, cfg, digest(["automatic-label-review", old["revision"]]),
                material.get("correction_notice_ids", []), material.get("units"),
            )
            if candidate["status"] not in ("confirmed", "excluded"):
                raise ValueError("复核未返回有效的二选一结论")
            audit = {
                "run_id": run_id, "model": cfg["review_model"], "prompt_digest": digest(prompt),
                "sales_item_name": name, "capability_description": description,
                "verdict": candidate["status"], "reason": candidate["reason"],
                "evidence_keys": [e.get("key") for e in candidate["evidence"]],
                "model_responses": list(reviewer.responses),
            }
            saved = save_result(service, scope, old, candidate, audit)
            event = candidate["status"] if saved else "changed"
            totals[event] += 1
            report(event=event, saved=saved, **info,
                   **{key: value for key, value in audit.items() if key not in info})
            consecutive_failures = 0
        except (ModelError, ValueError) as error:
            totals["failed"] += 1
            consecutive_failures += 1
            report(event="failed", model=cfg["review_model"], error=str(error), **info)
            if consecutive_failures >= 5:
                report(event="stopped", reason="连续五条复核失败", run_id=run_id)
                break
            if isinstance(error, ModelError) and error.retryable:
                await asyncio.sleep(min(60, max(1, error.retry_after or 5)))
    report(event="summary", run_id=run_id, **totals)
    return int(bool(totals["failed"] or totals["missing_evidence"] or totals["changed"]))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="实际调用模型并回填，默认只统计")
    parser.add_argument("--limit", type=int, help="最多处理指定条数")
    parser.add_argument("--published-days", type=int, help="只复核这些天内发布的公告涉及的项目")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.published_days is not None and not 1 <= args.published_days <= 365:
        parser.error("--published-days 必须在 1 到 365 之间")
    bootstrap = Settings()
    store = Store(bootstrap.database_url, bootstrap.analysis_schema)
    service = SalesService(store, bootstrap)
    service.model_factory = RecordingReviewer
    try:
        cfg = service.settings()
        if not cfg["source_dsn"]:
            raise ValueError("请先配置分析服务的主库连接")
        if args.execute and not (cfg["review_enabled"] and cfg["review_base_url"] and cfg["review_model"]):
            raise ValueError("请先启用并配置模型复核")
        if not args.execute:
            return await run(service, cfg, args)
        if store.engine.dialect.name != "postgresql":
            raise ValueError("批量回填需要 PostgreSQL 分析库")
        with store.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
            if not lock.exec_driver_sql("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK,)).scalar_one():
                raise ValueError("已有一个自动标签复核脚本在运行")
            try:
                return await run(service, cfg, args)
            finally:
                lock.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (RUN_LOCK,))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        await service.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
