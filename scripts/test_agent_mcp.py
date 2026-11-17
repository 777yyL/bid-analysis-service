"""Read-only MCP smoke test. Uses MCP_API_KEY; never prints credentials."""
import argparse
import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


async def run(args):
    token=os.getenv('MCP_API_KEY','')
    if not token:
        raise ValueError('请设置 MCP_API_KEY；在 analyzer 容器内运行时从 .env.shared 注入')
    parsed=urlsplit(args.url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError('MCP URL 必须为不含账号、密码、查询参数的 HTTP(S) 地址')
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+token},timeout=120,trust_env=False) as http:
        try:
            response=await http.post(args.url,
                headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'},
                json={'jsonrpc':'2.0','id':0,'method':'ping'})
        except httpx2.HTTPError as error:
            print('MCP 网络连接失败：'+type(error).__name__+'；检查 URL、端口、路由和证书。')
            raise
        if response.status_code!=200:
            hint={401:'MCP_API_KEY 不匹配',403:'请求被拒绝',404:'地址错误或部署的还是旧版',
                  409:'服务不是 shared 同库模式',503:'MCP 未启用或服务不可用',
                  307:'使用以 /mcp/ 结尾的完整地址'}.get(response.status_code,'检查服务日志')
            print(f'MCP 连接检查失败：HTTP {response.status_code}，{hint}')
            raise RuntimeError('连接检查失败')
        async with Client(streamable_http_client(args.url,http_client=http),read_timeout_seconds=120) as client:
            listing=await client.list_tools()
            print('连接成功，工具：'+', '.join(t.name for t in listing.tools),flush=True)
            await client.read_resource('bid://analysis/glossary')

            async def call(name,arguments):
                result=await client.call_tool(name,arguments)
                data=result.structured_content
                if result.is_error:
                    # Tool errors are sanitized server-side; do not print transport exception reprs.
                    print(json.dumps({'tool':name,'error':[getattr(v,'text','') for v in result.content]},ensure_ascii=False),flush=True)
                    raise RuntimeError('工具调用失败：'+name)
                print(json.dumps({'tool':name,'result':data},ensure_ascii=False,default=str),flush=True)
                return data

            await call('list_sales_items',{'page_size':3})
            filters={'start_date':args.start_date,'end_date':args.end_date,'time_basis':args.time_basis}
            await call('get_analysis_progress',{'filters':filters})
            await call('aggregate_projects',{'filters':filters,'group_by':'sales_item','limit':10})
            projects=await call('search_projects',{'filters':filters,'page_size':3})
            key=args.project_key or next((p['project_key'] for p in projects['items'] if p['matches']),None)
            if key:
                detail=await call('get_project_detail',{'project_key':key})
                if detail['matches']:
                    await call('get_match_evidence',{'project_key':key,'sales_item_id':detail['matches'][0]['sales_item_id']})
                else:
                    print('该项目无当前判断，跳过证据测试。')
            else:
                print('当前页没有命中项目，跳过详情/证据；可用 --project-key 指定已知项目。')
            print('只读测试完成；没有启动分析、模型调用或修改数据。')


def main():
    yesterday=(datetime.now(ZoneInfo('Asia/Shanghai')).date()-timedelta(days=1)).isoformat()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default=os.getenv('MCP_URL','http://127.0.0.1:8100/mcp/'))
    parser.add_argument('--start-date',default=yesterday)
    parser.add_argument('--end-date',default=yesterday)
    parser.add_argument('--time-basis',choices=['publish_at','first_seen_at'],default='publish_at')
    parser.add_argument('--project-key',default='')
    args=parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except Exception:
        print('测试未通过。检查上方工具错误、MCP_API_KEY、服务日志与网络；不输出可能携带凭据的底层异常。')
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
