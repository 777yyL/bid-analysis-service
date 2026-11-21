"""Run from the repository root with a disposable BID_MCP_TEST_DSN, using the normal HTTP app."""
import asyncio
from datetime import date
import logging
from pathlib import Path
import runpy
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import uvicorn

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.evaluate_presales_mcp import run


async def exercise():
    fixture=runpy.run_path(str(ROOT/'tests/test_agent_mcp.py'))['pg_app'].__wrapped__
    with TemporaryDirectory() as temp:
        gen=fixture(Path(temp))
        app,cfg=next(gen)
        try:
            with app.state.store.engine.begin() as conn:
                conn.exec_driver_sql("UPDATE public.source_projects SET channel='结果公告' WHERE project_key='p4'")
                conn.exec_driver_sql("""ALTER TABLE public.notices ADD COLUMN buyer text, ADD COLUMN source_url text,
                    ADD COLUMN budget numeric, ADD COLUMN deadline timestamp""")
                conn.exec_driver_sql("""UPDATE public.notices SET buyer='测试园区集团',budget=530000,
                    source_url='https://example.invalid/notices/'||notice_id,deadline='2026-11-20 09:00'
                    WHERE project_key_name IN ('p1','p4')""")
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0))
                port=sock.getsockname()[1]
                server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
                task=asyncio.create_task(server.serve(sockets=[sock]))
                try:
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError('MCP 启动失败')
                        await asyncio.sleep(.05)
                    args=SimpleNamespace(url=f'http://127.0.0.1:{port}/mcp/',end=date(2026,11,16),
                        cases=ROOT/'examples/presales-mcp-100.tsv',output=ROOT/'reports/presales-mcp-results.json',
                        project='p1',second_project='p4',item='visitor',second_item='parking',fixture=True)
                    await run(args,cfg.mcp_api_key.get_secret_value())
                    args.fixture=False
                    args.project=args.second_project=args.item=args.second_item=None
                    args.output=Path(temp)/'live-mode.json'
                    results=await run(args,cfg.mcp_api_key.get_secret_value())
                    assert len(results)==100 and args.project=='p1' and args.item
                finally:
                    server.should_exit=True
                    await task
        finally:
            gen.close()


if __name__=='__main__':
    logging.disable(logging.WARNING)
    asyncio.run(exercise())
