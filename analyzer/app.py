import asyncio
import contextlib
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyHeader
from pydantic import Field

from analyzer import __version__
from analyzer.config import Settings
from analyzer.llm import JudgeClient
from analyzer.schemas import SCORING_VERSION, KnowledgeInput, NoticeInput, StrictModel
from analyzer.source import SourceClient, SourceError
from analyzer.store import Store
from analyzer.worker import Worker

Mode = Literal["prepare", "judge"]
JobStatus = Literal["queued", "running", "retry", "ready", "completed", "needs_review", "failed"]


class SubmitJobs(StrictModel):
    notice_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=500)
    mode: Mode = "prepare"
    knowledge_id: str | None = None


class ImportJobs(StrictModel):
    notices: list[NoticeInput] = Field(min_length=1, max_length=200)
    mode: Mode = "prepare"
    knowledge_id: str | None = None


class SourcePage(StrictModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    mode: Mode = "prepare"
    knowledge_id: str | None = None


DESCRIPTION = """
独立招标分析服务。源系统仅 GET/只读查询，结果写入独立 DATABASE_URL。

使用顺序：配置 .env → 在 Authorize 输入 ANALYZER_API_KEY →
POST /knowledge/sync（源库只读连接）或 /knowledge/import（完整 JSON）→
POST /jobs 或 /jobs/from-source-page → GET /jobs/{job_id}。

默认 prepare 仅生成候选业务线与原文证据，不生成业务等级。
judge 需要配置并开启公司模型。ready=筛选完成；completed=模型判定完成；
needs_review=材料或覆盖不完整等情况；failed=执行失败；retry=等待重试。
关键词漏召回不能记为 D，人工应抽查无候选记录。

第一版评分固定采用报告五维规则（50/20/20/10，负向封顶扣30），由代码汇总，
scoring_version 独立标识；导入的 source_rubric 留作追溯，不自动解释其任意 JSON。
完整知识也需人工确认与该评分口径相容，结果不等同于原平台已批准结论。

单实例、单 Uvicorn worker 部署；RPM/TPM 限速器在进程内，不能用增加副本扩大额度。
源 API 的分页、正文路径可配置；未识别的响应会明确失败。
"""


def create_app(settings: Settings | None = None, *, source_client: SourceClient | None = None,
               judge_client: JudgeClient | None = None) -> FastAPI:
    cfg = settings or Settings()
    store = Store(cfg.database_url)
    source = source_client or SourceClient(cfg, text_cache=store)
    judge = judge_client or JudgeClient(cfg)
    worker = Worker(cfg, store, source, judge)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        task = asyncio.create_task(worker.run()) if cfg.worker_enabled else None
        app.state.worker_task = task
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await source.aclose()
            await judge.aclose()
            store.close()

    app = FastAPI(title="招标业务相关度分析服务", version=__version__,
                  description=DESCRIPTION, lifespan=lifespan)
    app.state.store, app.state.worker = store, worker
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def auth(key: str | None = Depends(api_key_header)):
        expected = cfg.analyzer_api_key.get_secret_value()
        if not expected:
            raise HTTPException(503, "请先配置 ANALYZER_API_KEY")
        if key is None or not secrets.compare_digest(key, expected):
            raise HTTPException(401, "API key 无效")

    protected = [Depends(auth)]

    def knowledge_key(mode: str, requested: str | None) -> str:
        if mode == "judge" and not cfg.llm_enabled:
            raise HTTPException(409, "精判未开启；配置 LLM_* 后设置 LLM_ENABLED=true，或使用 prepare")
        key = requested or store.active_knowledge_id()
        if not key:
            raise HTTPException(409, "请先同步或导入业务知识")
        try:
            store.get_knowledge(key)
        except ValueError:
            raise HTTPException(404, "知识快照不存在") from None
        return key

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.get("/healthz", tags=["运行状态"])
    async def health():
        try:
            store.active_knowledge_id()
        except Exception:
            raise HTTPException(503, "分析数据库不可用") from None
        task = getattr(app.state, "worker_task", None)
        if task and task.done():
            raise HTTPException(503, "后台任务进程已退出")
        return {"status": "ok", "version": __version__, "worker_enabled": cfg.worker_enabled}

    @app.get("/config", dependencies=protected, tags=["运行状态"])
    async def public_config():
        return {"llm_enabled": cfg.llm_enabled, "llm_model": cfg.llm_model,
                "llm_rpm": cfg.llm_rpm, "llm_tpm": cfg.llm_tpm,
                "source_database_enabled": bool(cfg.source_db_dsn.get_secret_value()),
                "active_knowledge_id": store.active_knowledge_id(),
                "candidate_limit": cfg.candidate_limit, "evidence_max_chars": cfg.evidence_max_chars,
                "scoring_version": SCORING_VERSION}

    @app.get("/source/check", dependencies=protected, tags=["源数据"])
    async def source_check():
        try:
            ids = await source.list_notice_ids(1, 1)
            return {"ok": True, "sample_notice_ids": ids}
        except SourceError as error:
            raise HTTPException(502, str(error)) from None

    @app.post("/knowledge/import", dependencies=protected, tags=["业务知识"])
    async def import_knowledge(payload: KnowledgeInput):
        key = store.save_knowledge(payload)
        return {"knowledge_id": key, "version": payload.version, "quality": payload.quality,
                "line_count": len(payload.lines), "keyword_count": len(payload.keywords),
                "warnings": payload.warnings}

    @app.post("/knowledge/sync", dependencies=protected, tags=["业务知识"])
    async def sync_knowledge():
        try:
            knowledge = await source.fetch_knowledge()
        except SourceError as error:
            raise HTTPException(502, str(error)) from None
        return await import_knowledge(knowledge)

    @app.get("/knowledge", dependencies=protected, tags=["业务知识"])
    async def get_knowledge():
        key = store.active_knowledge_id()
        if not key:
            raise HTTPException(404, "尚无业务知识")
        return {"knowledge_id": key, "knowledge": store.get_knowledge(key).model_dump()}

    @app.post("/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=protected, tags=["分析任务"])
    async def submit_jobs(payload: SubmitJobs):
        key = knowledge_key(payload.mode, payload.knowledge_id)
        jobs = [store.enqueue(nid, key, payload.mode) for nid in dict.fromkeys(payload.notice_ids)]
        return {"jobs": jobs}

    @app.post("/jobs/import", status_code=202, dependencies=protected, tags=["分析任务"])
    async def import_jobs(payload: ImportJobs):
        key = knowledge_key(payload.mode, payload.knowledge_id)
        return {"jobs": [store.enqueue(n.notice_id, key, payload.mode, n.model_dump())
                         for n in payload.notices]}

    @app.post("/jobs/from-source-page", status_code=202, dependencies=protected, tags=["分析任务"])
    async def jobs_from_page(payload: SourcePage):
        key = knowledge_key(payload.mode, payload.knowledge_id)
        try:
            ids = await source.list_notice_ids(payload.page, payload.page_size)
        except SourceError as error:
            raise HTTPException(502, str(error)) from None
        return {"page": payload.page, "notice_ids": ids,
                "jobs": [store.enqueue(nid, key, payload.mode) for nid in dict.fromkeys(ids)]}

    @app.get("/jobs", dependencies=protected, tags=["分析任务"])
    async def jobs(status: JobStatus | None = None, notice_id: int | None = Query(None, gt=0),
                   limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return store.list_jobs(status=status, notice_id=notice_id, limit=limit, offset=offset)

    @app.get("/jobs/{job_id}", dependencies=protected, tags=["分析任务"])
    async def job(job_id: str):
        item = store.get_job(job_id)
        if item is None:
            raise HTTPException(404, "任务不存在")
        item.pop("owner", None)
        return item

    @app.get("/metrics", dependencies=protected, tags=["运行状态"])
    async def metrics():
        return store.metrics()

    return app
