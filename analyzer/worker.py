import asyncio
import contextlib
import logging
import time

from pydantic import ValidationError

from analyzer.config import Settings
from analyzer.engine import analysis_fingerprint, prepare
from analyzer.llm import JudgeClient, JudgeError
from analyzer.schemas import PIPELINE_VERSION, SCORING_VERSION, NoticeInput
from analyzer.source import SourceClient, SourceError
from analyzer.store import Store

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, store: Store, source: SourceClient, judge: JudgeClient):
        self.settings, self.store, self.source, self.judge = settings, store, source, judge

    async def run(self):
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log source materials, DB DSNs or provider response bodies.
                logger.error("worker iteration failed; queued jobs remain durable")
                processed = False
            if not processed:
                await asyncio.sleep(self.settings.worker_poll_seconds)

    async def _heartbeat(self, job):
        while True:
            await asyncio.sleep(20)
            if not self.store.heartbeat(job):
                return

    async def run_once(self) -> bool:
        if getattr(self, 'sales_enabled', lambda: False)():
            return False
        job = self.store.claim()
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job))
        started = time.perf_counter()
        metrics = {"llm_calls": 0, "input_tokens": 0,
                   "queue_seconds": max(0, time.time() - job["created_at"])}
        try:
            if job["attempts"] > self.settings.job_max_attempts:
                self.store.fail(job, "任务多次中断，超过最大尝试次数", retry_delay=None, metrics=metrics)
                return True
            knowledge = self.store.get_knowledge(job["knowledge_id"])
            phase = time.perf_counter()
            notice = NoticeInput.model_validate(job["payload"]) if job["payload"] else (
                await self.source.fetch_notice(job["notice_id"]))
            metrics["fetch_seconds"] = time.perf_counter() - phase
            phase = time.perf_counter()
            prepared = await asyncio.to_thread(prepare, notice, knowledge, self.settings)
            metrics["prepare_seconds"] = time.perf_counter() - phase
            metrics["input_chars"] = prepared.input_chars
            metrics["selected_chars"] = prepared.selected_chars
            fingerprint = analysis_fingerprint(prepared, knowledge, self.settings)
            result = {"notice_id": notice.notice_id, "knowledge_id": job["knowledge_id"],
                      "knowledge_version": knowledge.version, "knowledge_quality": knowledge.quality,
                      "pipeline_version": PIPELINE_VERSION, "scoring_version": SCORING_VERSION,
                      "fingerprint": fingerprint, "prepared": prepared.model_dump(),
                      "judgment": None, "cache_hit": False, "metrics": metrics}
            status = "needs_review" if prepared.review_required else "ready"
            cache_value = None
            if (job["mode"] == "judge" and prepared.material_complete
                    and prepared.candidates and prepared.evidence):
                cached = self.store.get_cached_result(fingerprint) if prepared.material_complete else None
                if cached:
                    result["judgment"] = cached
                    result["cache_hit"] = True
                    # Usage in judgment records the original request; current-run metrics stay zero.
                else:
                    metrics["llm_calls"] = 1
                    judgment = await self.judge.judge(prepared, knowledge)
                    result["judgment"] = judgment.model_dump()
                    metrics["llm_calls"] = judgment.request_count
                    metrics["input_tokens"] = _input_tokens(judgment.usage)
                    metrics["model_seconds"] = judgment.elapsed_seconds
                    if prepared.material_complete and not prepared.review_required:
                        cache_value = judgment.model_dump()
                status = "needs_review" if prepared.review_required else "completed"
            metrics["processing_seconds"] = time.perf_counter() - started
            self.store.complete(job, status, result,
                                cache_key=fingerprint if cache_value is not None else None,
                                cache_value=cache_value)
        except asyncio.CancelledError:
            self.store.fail(job, "服务关闭，任务等待恢复", retry_delay=0, metrics=metrics)
            raise
        except (SourceError, JudgeError) as error:
            if isinstance(error, JudgeError):
                metrics["llm_calls"] = getattr(error, "request_count", metrics["llm_calls"])
                metrics["input_tokens"] = _input_tokens(getattr(error, "usage", {}))
            retry = error.retryable and job["attempts"] < self.settings.job_max_attempts
            delay = max(self.settings.job_retry_seconds * (2 ** (job["attempts"] - 1)),
                        getattr(error, "retry_after", None) or 0) if retry else None
            metrics["processing_seconds"] = time.perf_counter() - started
            self.store.fail(job, str(error), retry_delay=delay, metrics=metrics)
        except (ValueError, ValidationError):
            self.store.fail(job, "材料或判定输入校验失败；请检查材料大小、字段和知识配置",
                            retry_delay=None, metrics=metrics)
        except Exception:
            logger.error("analysis failed job_id=%s", job["id"])
            self.store.fail(job, "分析内部错误；请检查服务运行日志和数据库连接",
                            retry_delay=None, metrics=metrics)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True


def _input_tokens(usage: dict) -> int:
    value = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
