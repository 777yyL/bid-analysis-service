import asyncio
import contextlib
import logging
import time

from analyzer.llm import JudgeError
from analyzer.project_store import ProjectStore, project_key
from analyzer.schemas import NoticeInput
from analyzer.source import SourceClient, SourceError
from analyzer.store import digest
from analyzer.tag_model import TagJudge
from analyzer.tagging import RULE_VERSION, analyze_tags
from analyzer.workspace import NoticeRow

logger = logging.getLogger(__name__)


def merge_rules(notices, knowledge, store, corrected=False):
    tags, warnings, complete, chars = {}, [], True, 0
    rank = {"confirmed": 3, "candidate": 2, "excluded": 1}
    for notice in notices:
        result = analyze_tags(notice, knowledge, store)
        complete &= result["material_complete"] and notice.material_loaded
        chars += result["input_chars"]
        warnings.extend(f"公告 {notice.notice_id}：{w}" for w in result["warnings"])
        for tag in result["tags"]:
            key = (tag["dimension"], tag["category"], tag["label"])
            for evidence in tag["evidence"]:
                evidence.update(notice_id=notice.notice_id, notice_title=notice.title)
            previous = tags.get(key)
            if previous is None:
                tags[key] = {**tag, "evidence": list(tag["evidence"])}
            else:
                previous["evidence"] = [*previous["evidence"], *tag["evidence"]][:72]
                if rank[tag["status"]] > rank[previous["status"]]:
                    previous.update(status=tag["status"], reason=tag["reason"])
    if corrected:
        for tag in tags.values():
            if tag["status"] == "confirmed":
                tag.update(status="candidate", reason="主公告存在采购更正，需要结合更正确认当前有效需求")
    return {"tags": list(tags.values()), "material_complete": complete,
            "warnings": list(dict.fromkeys(warnings)), "input_chars": chars, "rule_version": RULE_VERSION}


class ProjectFlow:
    def __init__(self, cfg, ws, source, judge, scheduler):
        self.cfg, self.ws, self.source = cfg, ws, source
        self.judge, self.scheduler = TagJudge(judge), scheduler
        self.projects = ProjectStore(ws, cfg.model_no_hit_sample_percent)
        ws.project_service = self.projects
        self.ingest_lock = asyncio.Lock()

    async def ingest(self, notices, knowledge_id, use_model=True, priority=70, backfill=False, progress=None):
        async with self.ingest_lock:
            members = {n.notice_id: n for n in notices}
            keys = sorted({n.project_key for n in notices if n.project_key})
            warnings = []
            fetch = getattr(self.source, "fetch_project_notices", None)
            configured = not isinstance(self.source, SourceClient) or bool(
                self.cfg.source_db_dsn.get_secret_value() and self.cfg.source_file_root)
            if backfill and keys and callable(fetch) and configured:
                try:
                    kwargs = {"progress": progress} if isinstance(self.source, SourceClient) else {}
                    expanded = await fetch(keys, **kwargs)
                    members.update({n.notice_id: n for n in expanded})
                except SourceError as error:
                    # Fail the synchronization batch: its cursor must not advance past a failed backtrack.
                    raise SourceError(f"项目回溯失败：{error}", retryable=error.retryable) from None
            elif backfill and keys:
                warnings.append("来源未提供项目回溯通道，仅依据分析库已有公告选择主公告")
            changed, affected = 0, set()
            if progress:
                progress(phase="saving_notices", expanded=len(members), processed=0)
            for index, notice in enumerate(members.values(), 1):
                with self.ws.store.session() as session:
                    previous = session.get(NoticeRow, notice.notice_id)
                    if previous and previous.project_key:
                        affected.add(previous.project_key)
                key = project_key(notice)
                notice = notice.model_copy(update={"project_key": key})
                affected.add(key)
                item = await asyncio.to_thread(self.ws.ingest, notice, knowledge_id, False, priority, record_only=True)
                changed += int(item["changed"])
                if progress and (index % 10 == 0 or index == len(members)):
                    progress(processed=index)
            plans = []
            if progress:
                progress(phase="queueing_projects", projects=len(affected), projects_processed=0)
            for index, key in enumerate(sorted(affected), 1):
                plans.extend(await asyncio.to_thread(self.projects.refresh,
                    key, knowledge_id, use_model, priority, extra_warnings=warnings))
                if progress:
                    progress(projects_processed=index)
            return {"received": len(notices), "expanded": len(members), "changed": changed,
                    "projects": len(affected), "analyses": plans}

    async def reanalyze(self, key, use_model=True, force=False, backfill=True):
        knowledge_id = self.ws.store.active_knowledge_id()
        if not knowledge_id:
            raise ValueError("请先导入或同步词库")
        local = self.projects.notices(key)
        fetch = getattr(self.source, "fetch_project_notices", None)
        if backfill and callable(fetch) and self.cfg.source_db_dsn.get_secret_value() and self.cfg.source_file_root:
            expanded = await fetch([key])
            if expanded:
                await self.ingest(expanded, knowledge_id, use_model, 100)
                local = self.projects.notices(key)
        if not local:
            raise KeyError(key)
        for n in local:
            self.ws.ingest(n, knowledge_id, False, record_only=True)
        return {"project_key": key, "analyses": self.projects.refresh(
            key, knowledge_id, use_model, priority=100, force=force)}

    async def heartbeat(self, task):
        while True:
            await asyncio.sleep(20)
            if not self.ws.heartbeat(task):
                return

    async def run_once(self, kind):
        control = self.scheduler()
        self.cfg.llm_rpm = control["llm_rpm"]
        if control["paused"] or kind == "model" and (control["model_paused"] or not self.cfg.llm_enabled):
            return False
        task = self.ws.claim("project_" + kind)
        if task is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(task))
        started, metrics = time.perf_counter(), {"llm_calls": 0}
        try:
            if not self.projects.current(task):
                self.ws.finish_task(task, "stale", "项目主公告或有效采购需求已更新")
                return True
            if task["attempts"] > self.cfg.job_max_attempts:
                self.projects.mark_failed(task)
                self.ws.finish_task(task, "failed", "超过最大重试次数")
                return True
            plan = self.projects.internal(task["metrics"]["project_plan_id"])
            material = NoticeInput.model_validate(plan["input_payload"])
            knowledge = self.ws.store.get_knowledge(plan["knowledge_id"])
            if kind == "rule":
                size = sum(len(d.text) for d in material.analysis_documents)
                if size > self.cfg.material_max_chars:
                    raise ValueError("项目采购材料超过 MATERIAL_MAX_CHARS；未截断分析")
                members = {n.notice_id: n for n in self.projects.notices(plan["project_key"])}
                ids = [plan["primary_notice_id"], *plan["selection"].get("correction_notice_ids", [])]
                selected = [members[nid] for nid in ids]
                result = await asyncio.to_thread(merge_rules, selected, knowledge, self.ws.store, len(ids) > 1)
                result["material_complete"] &= bool(material.analysis_documents)
                if not material.analysis_documents:
                    result["warnings"].append("未识别到可用采购需求片段，不能判为业务无关")
                if not self.projects.save_rules(task, result):
                    self.ws.finish_task(task, "stale", "匹配期间项目采购需求已更新")
                    return True
                metrics.update(input_chars=size, rule_seconds=time.perf_counter() - started)
            else:
                if not self.projects.route_pending_model(task):
                    self.ws.finish_task(task, "skipped", "规则分流：本项目无需自动模型复核")
                    return True
                if plan["rule_revision"] != plan["revision"]:
                    self.ws.finish_task(task, "retry", "等待项目规则分析", retry_delay=2, consume_attempt=False)
                    return True
                if not material.analysis_documents:
                    self.ws.finish_task(task, "completed", "没有可用采购需求片段，保留待补齐状态")
                    return True
                detail = self.ws.detail(plan["primary_notice_id"])
                # Definite process-only matches remain excluded locally and do not consume model input.
                candidates = [t for t in detail["rule_tags"] if not t["evidence"] or any(
                    e.get("section_role") != "process" for e in t["evidence"])]
                cache_key = digest(["project-model-v1", plan["revision"], knowledge.model_dump(), candidates,
                    self.cfg.llm_model, self.cfg.llm_base_url, self.cfg.llm_max_input_chars,
                    self.cfg.llm_max_output_tokens])
                result = self.ws.store.get_cached_result(cache_key)
                if result is None:
                    result = await self.judge.judge_all(material, knowledge, candidates, self.ws.store)
                    metrics.update(result["metrics"])
                    if not result["needs_more"]:
                        self.ws.store.set_cached_result(cache_key, result)
                else:
                    metrics["cache_hit"] = True
                result = {**result, "metrics": metrics}
                if not self.projects.save_model(task, result):
                    self.ws.finish_task(task, "stale", "模型执行期间项目采购需求已更新", metrics=metrics)
                    return True
            metrics["processing_seconds"] = time.perf_counter() - started
            self.ws.finish_task(task, "completed", metrics=metrics)
        except asyncio.CancelledError:
            self.ws.finish_task(task, "retry", "服务关闭，保留项目任务", retry_delay=0, metrics=metrics)
            raise
        except (SourceError, JudgeError) as error:
            retry = error.retryable and task["attempts"] < self.cfg.job_max_attempts
            metrics.update(llm_calls=getattr(error, "request_count", 0),
                           input_tokens=getattr(error, "usage", {}).get("prompt_tokens", 0),
                           rate_limits=int("429" in str(error)))
            delay = max(self.cfg.job_retry_seconds * 2 ** (task["attempts"] - 1),
                        getattr(error, "retry_after", 0) or 0) if retry else None
            if not retry:
                self.projects.mark_failed(task)
            self.ws.finish_task(task, "failed", str(error), retry_delay=delay, metrics=metrics)
        except ValueError as error:
            self.projects.mark_failed(task)
            self.ws.finish_task(task, "failed", str(error)[:300], metrics=metrics)
        except Exception:
            logger.error("project task failed id=%s", task["id"])
            self.projects.mark_failed(task)
            self.ws.finish_task(task, "failed", "项目分析失败，请检查分析库及服务日志", metrics=metrics)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True
