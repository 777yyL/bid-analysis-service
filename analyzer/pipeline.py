"""Separate rule/model loops: provider waiting never stalls local text matching."""

import asyncio
import contextlib
import logging
import time

from analyzer.llm import JudgeError
from analyzer.schemas import NoticeInput
from analyzer.source import SourceError
from analyzer.store import digest
from analyzer.tag_model import TagJudge
from analyzer.tagging import analyze_tags

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, cfg, ws, source, judge):
        self.cfg, self.ws, self.source = cfg, ws, source
        self.judge = TagJudge(judge)
        self.sync_lock = asyncio.Lock()

    def scheduler(self):
        control = self.ws.state("scheduler", {})
        return {"paused": control.get("paused", False),
                "model_paused": control.get("model_paused", False),
                "llm_enabled": self.cfg.llm_enabled,
                "llm_rpm": control.get("llm_rpm", self.cfg.llm_rpm)}

    async def run(self, kind):
        while True:
            try:
                if not await self.run_once(kind):
                    await asyncio.sleep(self.cfg.worker_poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("v2 %s loop failed; durable tasks retained", kind)
                await asyncio.sleep(self.cfg.worker_poll_seconds)

    async def _heartbeat(self, task):
        while True:
            await asyncio.sleep(20)
            if not self.ws.heartbeat(task):
                return

    async def run_once(self, kind):
        scheduler = self.scheduler()
        if scheduler["paused"] or (kind == "model" and (
                scheduler["model_paused"] or not self.cfg.llm_enabled)):
            return False
        self.cfg.llm_rpm = scheduler["llm_rpm"]
        task = self.ws.claim(kind)
        if task is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(task))
        started = time.perf_counter()
        metrics = {"llm_calls": 0}
        try:
            if not self.ws.task_is_current(task):
                self.ws.finish_task(task, "stale", "公告或词库已更新")
                return True
            if task["attempts"] > self.cfg.job_max_attempts:
                self.ws.finish_task(task, "failed", "任务多次中断，超过重试次数")
                return True
            detail = self.ws.detail(task["notice_id"])
            notice = NoticeInput.model_validate(detail["notice"])
            knowledge = self.ws.store.get_knowledge(task["knowledge_id"])
            if kind == "rule":
                if len(notice.body) + sum(len(a.text) for a in notice.attachments) > self.cfg.material_max_chars:
                    raise ValueError("材料超过配置上限，请提高 MATERIAL_MAX_CHARS")
                result = await asyncio.to_thread(analyze_tags, notice, knowledge, self.ws.store)
                revoked = any("已撤销" in warning for warning in notice.warnings)
                tags = [] if revoked else result["tags"]
                saved = self.ws.save_rules(notice.notice_id, task["knowledge_id"], tags,
                                           result["material_complete"], result["warnings"], task,
                                           metadata={k: result.get(k) for k in ("rule_version", "match_version", "input_chars")})
                if not saved:
                    self.ws.finish_task(task, "stale", "匹配期间公告或词库已更新")
                    return True
                if saved and task["use_model"] and not revoked:
                    # Explore no-hit announcements too, at lower priority; never mark unrelated.
                    priority = 90 if any(t["status"] == "candidate" for t in tags) else 40 if tags else 20
                    priority += task["priority"] - 70
                    self.ws.enqueue(notice.notice_id, task["knowledge_id"], "model", priority)
                metrics.update(input_chars=result["input_chars"], rule_seconds=time.perf_counter() - started)
            else:
                # Wait until the current rule pass is saved, without consuming provider quota.
                if detail["analysis_status"] == "queued":
                    self.ws.finish_task(task, "retry", "等待规则分析", retry_delay=2, consume_attempt=False)
                    return True
                key = digest(["tag-model-v2", notice.model_dump(), knowledge.model_dump(),
                              detail["rule_tags"], self.cfg.llm_model, self.cfg.llm_base_url,
                              self.cfg.llm_max_input_chars, self.cfg.llm_max_output_tokens])
                cached = self.ws.store.get_cached_result(key)
                if cached:
                    result = cached
                    metrics["cache_hit"] = True
                else:
                    result = await self.judge.judge_all(notice, knowledge, detail["rule_tags"], self.ws.store)
                    metrics.update(result["metrics"])
                needs_more = result["needs_more"] or not detail["material_complete"]
                saved = self.ws.save_model(task, result["tags"], needs_more, metrics)
                if not saved:
                    self.ws.finish_task(task, "stale", "分析期间公告或词库已更新", metrics=metrics)
                    return True
                if not cached and not needs_more:
                    # Model cache is revision-bound and never reuses manual decisions.
                    self.ws.store.set_cached_result(key, result)
            metrics["processing_seconds"] = time.perf_counter() - started
            self.ws.finish_task(task, "completed", metrics=metrics)
        except asyncio.CancelledError:
            self.ws.finish_task(task, "retry", "服务关闭，任务保留", retry_delay=0, metrics=metrics)
            raise
        except (JudgeError, SourceError) as error:
            retry = error.retryable and task["attempts"] < self.cfg.job_max_attempts
            delay = max(self.cfg.job_retry_seconds * 2 ** (task["attempts"] - 1),
                        getattr(error, "retry_after", 0) or 0) if retry else None
            metrics["llm_calls"] = getattr(error, "request_count", 0)
            metrics["rate_limits"] = int("429" in str(error))
            metrics["input_tokens"] = getattr(error, "usage", {}).get("prompt_tokens", 0)
            self.ws.finish_task(task, "failed", str(error), retry_delay=delay, metrics=metrics)
        except ValueError as error:
            self.ws.finish_task(task, "failed", str(error)[:300], metrics=metrics)
        except Exception:
            logger.error("v2 task failed id=%s", task["id"])
            self.ws.finish_task(task, "failed", "内部错误，请检查分析库及服务日志", metrics=metrics)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def sync_batch(self, mode="history", page=1, page_size=100, use_model=True):
        async with self.sync_lock:
            key = self.ws.store.active_knowledge_id()
            if not key:
                raise ValueError("请先导入或同步词库")
            if mode == "page":
                ids = await self.source.list_notice_ids(page, page_size)
                notices = await self.source.fetch_notice_batch(ids)
                cursor = None
            else:
                state = self.ws.state("sync:" + mode, {"cursor": 0})
                cursor = state["cursor"]
                # Freeze the lower bound for a complete keyset update sweep.
                since = state.get("since") if mode == "incremental" else None
                if mode == "incremental" and since is None:
                    since = time.time() - self.cfg.incremental_lookback_seconds
                    state["since"] = since
                    state["sweep_started"] = time.time()
                notices = await self.source.list_notice_batch(cursor, page_size, since)
            changed = 0
            for notice in notices:
                priority = 100 if mode in ("page", "incremental") else 30
                changed += int(self.ws.ingest(notice, key, use_model, priority)["changed"])
            if mode != "page":
                state["cursor"] = max([cursor, *[n.notice_id for n in notices]])
                state["last_batch_at"] = time.time()
                state["last_count"] = len(notices)
                if not notices:
                    if mode == "incremental":
                        state = {"cursor": 0, "since": state.get("sweep_started", time.time())
                                 - self.cfg.incremental_lookback_seconds, "sweep_started": time.time()}
                    else:
                        state["complete"] = True
                        self.ws.set_state("history_enabled", False)
                self.ws.set_state("sync:" + mode, state)
            return {"received": len(notices), "changed": changed, "cursor": cursor,
                    "mode": mode, "knowledge_id": key}

    async def sync_loop(self):
        while True:
            try:
                await self._sync_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("source sync iteration failed; durable cursors retained")
                await asyncio.sleep(30)

    async def _sync_loop(self):
        next_incremental = 0.0
        next_retry = {}
        while True:
            if not self.scheduler()["paused"]:
                reindex = self.ws.state("reindex", {})
                if reindex.get("enabled") and time.time() >= next_retry.get("reindex", 0):
                    try:
                        result = self.ws.reindex_batch(reindex["knowledge_id"], reindex.get("cursor", 0),
                                                       self.cfg.sync_batch_size)
                        self.ws.set_state("reindex", {**reindex, "cursor": result["cursor"],
                                                       "enabled": bool(result["received"])})
                        self.ws.set_state("sync_error:reindex", None)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        next_retry["reindex"] = time.time() + 30
                        self.ws.set_state("sync_error:reindex", "词库重算暂时失败，保留游标并自动重试")
                for mode in ("incremental", "history", "compensation"):
                    enabled = self.ws.state(mode + "_enabled", self.cfg.incremental_enabled if mode == "incremental" else False)
                    if mode == "compensation":
                        incremental = self.ws.state("incremental_enabled", self.cfg.incremental_enabled)
                        state = self.ws.state("sync:compensation", {})
                        enabled = incremental and not self.ws.state("history_enabled", False)
                        if state.get("complete") and time.time() - state.get("last_batch_at", 0) >= self.cfg.compensation_interval_seconds:
                            self.ws.set_state("sync:compensation", {"cursor": 0})
                            state = {}
                        enabled = enabled and not state.get("complete")
                    if time.time() < next_retry.get(mode, 0):
                        continue
                    if mode == "incremental" and time.time() < next_incremental:
                        continue
                    if enabled:
                        try:
                            use_model = self.ws.state(mode + "_use_model", self.ws.state("incremental_use_model", True)
                                                      if mode == "compensation" else True)
                            result = await self.sync_batch(mode, page_size=self.cfg.sync_batch_size, use_model=use_model)
                            if mode == "incremental" and not result["received"]:
                                next_incremental = time.time() + self.cfg.sync_interval_seconds
                            self.ws.set_state("sync_error:" + mode, None)
                            if mode == "history" and result["received"]:
                                await asyncio.sleep(0.05)
                        except asyncio.CancelledError:
                            raise
                        except (SourceError, ValueError) as error:
                            self.ws.set_state("sync_error:" + mode, str(error))
                            next_retry[mode] = time.time() + max(30, self.cfg.sync_interval_seconds)
                        except Exception:
                            self.ws.set_state("sync_error:" + mode, "同步失败，请检查源连接及服务日志")
                            next_retry[mode] = time.time() + max(30, self.cfg.sync_interval_seconds)
            reindex = self.ws.state("reindex", {})
            busy = self.ws.state("history_enabled", False) or reindex.get("enabled")
            delay = 0.2 if busy and not self.scheduler()["paused"] else self.cfg.sync_interval_seconds
            await asyncio.sleep(min(delay, 5))
