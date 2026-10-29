"""Deployment checks: python -m analyzer.cli --help."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

TERMINAL = {"ready", "completed", "needs_review", "failed"}


def parser():
    root = argparse.ArgumentParser(description="独立招标分析服务验证工具；默认不调用公司模型")
    root.add_argument("--base-url", default="http://127.0.0.1:8100")
    root.add_argument("--api-key", default=os.getenv("ANALYZER_API_KEY", ""),
                      help="默认读取 ANALYZER_API_KEY；推荐环境变量方式")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="检查分析服务及源公告列表接口")
    commands.add_parser("sync-knowledge", help="同步真实业务知识，需要配置源库只读连接或完整词表API")
    commands.add_parser("metrics", help="查看任务数量和最近1000个已结束任务的开销")
    demo = commands.add_parser("demo", help="仅空库自检：导入演示知识和公告，会切换当前知识快照")
    demo.add_argument("--wait", action="store_true")
    demo.add_argument("--mode", choices=["prepare", "judge"], default="prepare")
    submit = commands.add_parser("submit", help="按真实公告ID提交")
    submit.add_argument("--ids", required=True, help="逗号分隔的公告ID")
    page = commands.add_parser("page", help="读取一页真实公告并提交；分页不是稳定增量游标")
    page.add_argument("--page", type=int, default=1)
    page.add_argument("--page-size", type=int, default=20)
    for command in (submit, page):
        command.add_argument("--mode", choices=["prepare", "judge"], default="prepare")
        command.add_argument("--wait", action="store_true")
    job = commands.add_parser("job", help="查询任务完整结果及原文证据")
    job.add_argument("id")
    return root


def run(args):
    headers = {"X-API-Key": args.api_key}
    with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=120,
                      trust_env=False) as client:
        def request(method, path, payload=None):
            response = client.request(method, path, json=payload)
            if response.is_error:
                try:
                    detail = response.json().get("detail", "请求失败")
                except (ValueError, AttributeError):
                    detail = "请求失败，响应不是JSON"
                raise RuntimeError(f"HTTP {response.status_code}: {detail}")
            return response.json()

        if args.command == "check":
            return {"analyzer": request("GET", "/healthz"), "source": request("GET", "/source/check")}
        if args.command == "sync-knowledge":
            return request("POST", "/knowledge/sync")
        if args.command == "metrics":
            return request("GET", "/metrics")
        if args.command == "job":
            return request("GET", f"/jobs/{args.id}")
        if args.command == "demo":
            folder = Path(__file__).resolve().parent.parent / "examples"
            knowledge = json.loads((folder / "knowledge.json").read_text(encoding="utf-8"))
            request("POST", "/knowledge/import", knowledge)
            body = json.loads((folder / "notices.json").read_text(encoding="utf-8"))
            body["mode"] = args.mode
            submitted = request("POST", "/jobs/import", body)
        elif args.command == "submit":
            try:
                ids = [int(value.strip()) for value in args.ids.split(",")]
            except ValueError:
                raise RuntimeError("--ids 必须是逗号分隔的正整数") from None
            submitted = request("POST", "/jobs", {"notice_ids": ids, "mode": args.mode})
        else:
            submitted = request("POST", "/jobs/from-source-page", {
                "page": args.page, "page_size": args.page_size, "mode": args.mode,
            })
        if not args.wait:
            return submitted
        pending = {job["id"] for job in submitted["jobs"]}
        finished = []
        deadline = time.monotonic() + 1800
        while pending and time.monotonic() < deadline:
            for job_id in list(pending):
                job = request("GET", f"/jobs/{job_id}")
                if job["status"] in TERMINAL:
                    result = job.get("result") or {}
                    prepared = result.get("prepared") or {}
                    finished.append({"id": job_id, "notice_id": job["notice_id"],
                                     "status": job["status"], "error": job.get("error"),
                                     "candidates": prepared.get("candidates"),
                                     "warnings": prepared.get("warnings"),
                                     "metrics": result.get("metrics"), "cache_hit": result.get("cache_hit")})
                    pending.remove(job_id)
            if pending:
                time.sleep(1)
        return {"finished": finished, "pending_job_ids": sorted(pending)}


def main():
    load_dotenv()
    args = parser().parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and (result.get("pending_job_ids") or any(
            item["status"] == "failed" for item in result.get("finished", [])
        )):
            return 1
        return 0
    except (RuntimeError, httpx.HTTPError, OSError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "连接或本地文件读取失败"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
