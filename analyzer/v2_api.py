import asyncio
import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import Field

from analyzer.importer import import_knowledge_files
from analyzer.schemas import KnowledgeInput, NoticeInput, StrictModel
from analyzer.source import SourceError
from analyzer.tagging import analyze_tags


class ReviewInput(StrictModel):
    dimension: Literal["business", "product"]
    category: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)
    status: Literal["confirmed", "candidate", "excluded"]
    reason: str = Field(min_length=1, max_length=2000)


class ReanalyzeInput(StrictModel):
    use_model: bool = True


class Controls(StrictModel):
    paused: bool | None = None
    model_paused: bool | None = None
    llm_rpm: int | None = Field(default=None, ge=1, le=10000)


class SyncInput(StrictModel):
    mode: Literal["page", "history", "incremental"] = "page"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)
    limit: int | None = Field(default=None, ge=1, le=500)
    use_model: bool = True
    enabled: bool = True


class TrialInput(StrictModel):
    title: str = Field(min_length=1, max_length=2000)
    buyer: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=200000)


class Imports(StrictModel):
    notices: list[NoticeInput] = Field(min_length=1, max_length=200)
    use_model: bool = True


def router(cfg, ws, pipeline, auth):
    api = APIRouter(prefix="/api/v2", dependencies=[Depends(auth)], tags=["可视化分析"])

    def knowledge():
        key = ws.store.active_knowledge_id()
        if not key:
            raise HTTPException(409, "请先导入或同步词库")
        return key, ws.store.get_knowledge(key)

    @api.get("/overview")
    async def overview():
        return {**ws.overview(), "scheduler": pipeline.scheduler(),
                "knowledge_id": ws.store.active_knowledge_id(),
                "sync": {mode: {**ws.state("sync:" + mode, {}),
                                "enabled": ws.state(mode + "_enabled", False),
                                "error": ws.state("sync_error:" + mode)}
                         for mode in ("history", "incremental", "compensation", "reindex")}}

    @api.get("/notices/export.csv")
    async def export(q: str = "", dimension: str | None = None, category: str | None = None,
                     tag_status: str | None = None, analysis_status: str | None = None,
                     page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=5000)):
        items = ws.notices(q, dimension, category, tag_status, analysis_status, page, page_size)["items"]
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["公告ID", "标题", "采购人", "阶段", "业务标签", "产品标签", "分析状态"])
        def cell(value):
            value = str(value)
            return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
        for item in items:
            def labels(dim):
                return "；".join(f'{t["category"]}/{t["label"]}({t["status"]})'
                                for t in item["tags"] if t["dimension"] == dim)
            writer.writerow([cell(v) for v in [item["notice_id"], item["title"], item["buyer"],
                             item["notice_type"], labels("business"), labels("product"), item["analysis_status"]]])
        return Response(content=("\ufeff" + output.getvalue()).encode(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="notices.csv"'})

    @api.get("/notices")
    async def notices(q: str = "", dimension: str | None = None, category: str | None = None,
                      tag_status: str | None = None, analysis_status: str | None = None,
                      business_category: str | None = None, product_category: str | None = None,
                      page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return ws.notices(q, dimension, category, tag_status, analysis_status, page, page_size,
                          business_category, product_category)

    @api.post("/notices/import")
    async def import_notices(payload: Imports):
        key, _ = knowledge()
        return {"items": [ws.ingest(n, key, payload.use_model) for n in payload.notices]}

    @api.get("/notices/{notice_id}")
    async def detail(notice_id: int):
        try:
            return ws.detail(notice_id)
        except KeyError:
            raise HTTPException(404, "公告不存在") from None

    @api.post("/notices/{notice_id}/review")
    async def review(notice_id: int, payload: ReviewInput):
        _, kb = knowledge()
        valid = {(line.business_domain, line.business_line) for line in kb.lines} if payload.dimension == "business" else {
            (p.category, p.word) for p in kb.product_keywords}
        try:
            existing = ws.detail(notice_id)
            valid |= {(t["category"], t["label"]) for t in existing["tags"]
                      if t["dimension"] == payload.dimension}
            if (payload.category, payload.label) not in valid:
                raise HTTPException(422, "新标签不在当前词库中，请先维护词库")
            ws.review(notice_id, payload.model_dump())
            return ws.detail(notice_id)
        except KeyError:
            raise HTTPException(404, "公告不存在") from None

    @api.post("/notices/{notice_id}/reanalyze")
    async def reanalyze(notice_id: int, payload: ReanalyzeInput):
        key, _ = knowledge()
        try:
            item = ws.detail(notice_id)
            # Rebind snapshot while preserving manual overrides.
            ws.ingest(NoticeInput.model_validate(item["notice"]), key, False)
            return ws.enqueue(notice_id, key, "rule", 100, payload.use_model)
        except KeyError:
            raise HTTPException(404, "公告不存在") from None

    @api.get("/projects")
    async def projects(q: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return ws.projects(q, page, page_size)

    @api.get("/projects/{project_key:path}")
    async def project(project_key: str):
        try:
            return ws.project(project_key)
        except KeyError:
            raise HTTPException(404, "项目不存在") from None

    @api.get("/tasks")
    async def tasks(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                    status: str | None = None, kind: str | None = None):
        return {**ws.tasks(page, page_size, status, kind), "scheduler": pipeline.scheduler()}

    @api.post("/tasks/control")
    async def control(payload: Controls):
        state = ws.state("scheduler", {})
        state.update(payload.model_dump(exclude_none=True))
        ws.set_state("scheduler", state)
        return pipeline.scheduler()

    @api.post("/tasks/{task_id}/retry")
    async def retry(task_id: str):
        try:
            return ws.retry(task_id)
        except KeyError:
            raise HTTPException(404, "任务不存在") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @api.post("/sync")
    async def sync(payload: SyncInput):
        knowledge()
        if payload.mode != "page":
            if not (cfg.source_file_root and cfg.source_db_dsn.get_secret_value()):
                raise HTTPException(409, "历史/增量同步需要 SOURCE_DB_DSN 与 SOURCE_FILE_ROOT；也可分页同步")
            ws.set_state(payload.mode + "_enabled", payload.enabled)
            ws.set_state(payload.mode + "_use_model", payload.use_model)
            if payload.enabled and payload.mode == "history":
                state = ws.state("sync:history", {})
                if state.get("complete"):
                    ws.set_state("sync:history", {"cursor": 0})
            return {"mode": payload.mode, "enabled": payload.enabled}
        try:
            return await pipeline.sync_batch("page", payload.page, payload.limit or payload.page_size,
                                             payload.use_model)
        except (SourceError, ValueError) as error:
            raise HTTPException(502, str(error)) from None

    @api.get("/lexicon")
    async def lexicon():
        _, kb = knowledge()
        return kb.model_dump()

    @api.put("/lexicon")
    async def save_lexicon(payload: KnowledgeInput):
        key = ws.store.save_knowledge(payload)
        # Durable sweep rebinds old notices to the new snapshot without touching original data.
        ws.set_state("reindex", {"cursor": 0, "knowledge_id": key, "enabled": True})
        return {"knowledge_id": key, "version": payload.version}

    @api.post("/lexicon/import-files")
    async def import_files():
        if not cfg.knowledge_files_dir:
            raise HTTPException(409, "请配置 KNOWLEDGE_FILES_DIR 为三份 TXT 所在目录")
        try:
            kb = await asyncio.to_thread(import_knowledge_files, cfg.knowledge_files_dir)
            return {**await save_lexicon(kb), "warnings": kb.warnings,
                    "business_keywords": len(kb.keywords), "product_keywords": len(kb.product_keywords)}
        except (OSError, ValueError):
            raise HTTPException(422, "导入失败，请检查配置目录、文件编码及 SQL 导出格式") from None

    @api.post("/lexicon/trial")
    async def trial(payload: TrialInput):
        _, kb = knowledge()
        return await asyncio.to_thread(analyze_tags, NoticeInput(notice_id=1, **payload.model_dump()), kb)

    @api.post("/demo")
    async def demo():
        # Demo must never replace the user's established taxonomy.
        key = ws.store.active_knowledge_id()
        if key:
            raise HTTPException(409, "已有词库。为避免混入真实结果，请在独立空分析库中使用演示数据")
        kb = KnowledgeInput.model_validate({"version": "demo-v2", "quality": "full",
            "lines": [{"business_domain": "教育教学", "business_line": "高等教育", "body_md": "高校数字化与安防采购"}],
            "keywords": [{"business_line": "高等教育", "word": "大学", "kind": "scene"}],
            "product_keywords": [{"category": "网络产品", "word": "交换机"},
                                 {"category": "IPC、球机", "word": "摄像机"}]})
        key = ws.store.save_knowledge(kb)
        samples = [NoticeInput(notice_id=900000001, title="【演示】某大学网络与安防采购", buyer="某大学",
                               project_key="demo-campus", notice_type="采购公告",
                               body="采购清单：交换机10台。摄像机100台。"),
                   NoticeInput(notice_id=900000002, title="【演示】校园安防项目更正", project_key="demo-campus",
                               notice_type="更正公告", body="采购清单更正：摄像机调整为80台。"),
                   NoticeInput(notice_id=900000003, title="【演示】园林养护采购", notice_type="采购公告",
                               project_key="demo-garden", body="采购范围：绿化养护。投标业绩中包含摄像机项目。")]
        for notice in samples:
            ws.ingest(notice, key, use_model=False)
        return {"notice_count": 3, "knowledge_id": key, "demo_only": True}

    return api
