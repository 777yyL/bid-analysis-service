"""Sales-item analysis application. Legacy workbench endpoints and workers are retired."""
import asyncio
import contextlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from analyzer import __version__
from analyzer.config import Settings
from analyzer.sales_api import router as sales_router
from analyzer.sales_service import SalesService
from analyzer.store import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    store = Store(cfg.database_url, cfg.analysis_schema)
    sales = SalesService(store, cfg)

    @asynccontextmanager
    async def lifespan(app):
        store.initialize(sales_only=True)
        from analyzer.source_migration import migrate_local_sources
        migrate_local_sources(sales)
        tasks = []
        if cfg.worker_enabled:
            tasks.extend(asyncio.create_task(sales.run(i), name=f'sales-worker-{i}') for i in range(32))
            tasks.append(asyncio.create_task(sales.scan_loop(), name='sales-scan'))
        app.state.sales_tasks = tasks
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await sales.close()
            store.close()

    app = FastAPI(title='招标销售项分析服务', version=__version__, lifespan=lifespan)
    app.state.store, app.state.sales = store, sales
    header = APIKeyHeader(name='X-API-Key', auto_error=False)

    async def auth(key=Depends(header)):
        expected = cfg.analyzer_api_key.get_secret_value()
        if not expected:
            raise HTTPException(503, '请先配置 ANALYZER_API_KEY')
        if key is None or not secrets.compare_digest(key.encode(), expected.encode()):
            raise HTTPException(401, 'API key 无效')

    ui = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'

    @app.get('/', include_in_schema=False)
    def root():
        return RedirectResponse('/ui/' if ui.is_dir() else '/docs')

    @app.get('/healthz')
    def health():
        try:
            with store.session() as session:
                session.execute(text('SELECT 1'))
        except Exception:
            raise HTTPException(503, '分析数据库不可用') from None
        if any(task.done() for task in getattr(app.state, 'sales_tasks', [])):
            raise HTTPException(503, '销售项后台进程已退出')
        return {'status':'ok', 'version':__version__, 'worker_enabled':cfg.worker_enabled}

    app.include_router(sales_router(sales, auth, cfg.worker_enabled))
    if ui.is_dir():
        app.mount('/ui', StaticFiles(directory=ui, html=True), name='ui')
    return app
