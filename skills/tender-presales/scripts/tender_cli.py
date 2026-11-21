#!/usr/bin/env python3
"""Tender MCP CLI for the stateless JSON HTTP endpoint. Standard library only."""
import argparse
import csv
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None

class Client:
    def __init__(self):
        preset=Path(__file__).resolve().parents[1]/'connection.private.json'
        if preset.exists():
            connection=json.loads(preset.read_text('utf-8'))
            self.url,key=connection['url'],connection['api_key']
        else:
            self.url=os.environ.get('MCP_URL','http://127.0.0.1:8100/mcp/')
            key=os.environ.get('MCP_API_KEY')
        if urlsplit(self.url).scheme not in ('http','https') or urlsplit(self.url).username:
            raise ValueError('MCP_URL 必须为 HTTP(S) MCP 服务地址')
        if not key: raise ValueError('请设置环境变量 MCP_API_KEY')
        self.headers={'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream',
                      'Content-Type':'application/json','MCP-Protocol-Version':'2025-11-25'}
        self.http=build_opener(ProxyHandler({}),NoRedirect())
        self.seq=0
        result=self.rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='tender-cli',version='1')))
        if result.get('protocolVersion')!='2025-11-25': raise ValueError('MCP 协议版本不兼容')
        self.rpc('notifications/initialized',{},True)

    def rpc(self,method,params,notification=False):
        self.seq+=1
        body=dict(jsonrpc='2.0',method=method,params=params)
        if not notification: body['id']=self.seq
        with self.http.open(Request(self.url,data=json.dumps(body).encode(),headers=self.headers),timeout=60) as response:
            if notification: return
            result=json.load(response)
        if 'error' in result: raise ValueError(str(result['error'].get('message','MCP error')))
        return result['result']

    def call(self,name,**args):
        r=self.rpc('tools/call',dict(name=name,arguments=args))
        if r.get('isError'): raise ValueError(str(r.get('content',[{}])[0].get('text','工具失败')))
        return r['structuredContent']

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    s=parser.add_subparsers(dest='command',required=True)
    s.add_parser('tools')
    raw=s.add_parser('call',help='以 JSON 文件传递任一工具参数；- 从 stdin 读')
    raw.add_argument('tool');raw.add_argument('file')
    rpt=s.add_parser('report',help='同一结果集生成采购人和中标人排行')
    rpt.add_argument('--start',required=True);rpt.add_argument('--end',required=True,help='不含终点，例如包含19日需填20日')
    rpt.add_argument('--region',action='append',default=[]);rpt.add_argument('--term',action='append',default=[])
    rpt.add_argument('--scope',choices=['all'],default='all')
    rpt.add_argument('--events',choices=['tenders','awards','all'],default='tenders')
    rpt.add_argument('--top',type=int,default=20,choices=range(1,51),metavar='1..50')
    rpt.add_argument('--csv',type=Path,help='可选，导出两张排行的前N项到同一个CSV')
    a=parser.parse_args();c=Client()
    if a.command=='tools': result=[t['name'] for t in c.rpc('tools/list',{})['tools']]
    elif a.command=='call':
        data=json.load(sys.stdin) if a.file=='-' else json.loads(Path(a.file).read_text('utf-8-sig'))
        result=c.call(a.tool,**data)
    else:
        result=c.call('search_tenders',filters=dict(start=a.start,end=a.end,terms=a.term,regions=a.region,business_scope=a.scope,events=a.events))
        for g in ('buyers','winners'):
            result[g]=c.call('aggregate_tenders',selection_id=result['selection_id'],group=g,page_size=a.top)
        if a.csv:
            with a.csv.open('x',encoding='utf-8-sig',newline='') as f:
                w=csv.writer(f);w.writerow(['角色','单位','项目数','示例项目','示例公告'])
                for g,role in [('buyers','采购人'),('winners','中标人')]:
                    for r in result[g]['items']:
                        cells=[role,r['name'],r['project_count'],r['example_project_key'],r['example_notice_id']]
                        w.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in cells])
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))

if __name__=='__main__':
    try: main()
    except (ValueError,KeyError,OSError,HTTPError,URLError) as e:
        print(f'查询失败：{e}',file=sys.stderr);sys.exit(1)
