"""Zipapp entrypoint. Reads live MCP key without putting it in an argument or log."""
import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from zipfile import ZipFile, ZIP_DEFLATED

FILES=('SKILL.md','agents/openai.yaml','scripts/tender_cli.py','references/business-scope.md')

def main():
    p=argparse.ArgumentParser(description='生成可直接分发给内网售前的招采雷达 Skill 包')
    p.add_argument('--url',required=True,help='从售前电脑可访问的完整 MCP 地址')
    p.add_argument('--key-source',choices=['compose','prompt'],default='compose')
    p.add_argument('--output',type=Path,default=Path('招采雷达-内网专用.zip'))
    args=p.parse_args()
    u=urlsplit(args.url.strip())
    if u.scheme not in ('http','https') or not u.hostname or u.username is not None or u.password is not None or u.query or u.fragment or not u.path.rstrip('/').endswith('/mcp'):
        p.error('请提供不含认证信息、参数或片段的 MCP URL（以 /mcp/ 结尾）')
    url=args.url.strip().rstrip('/')+'/'
    if args.key_source=='compose':
        r=subprocess.run(['docker','compose','--env-file','.env.shared','-f','compose.shared.yaml','exec','-T','analyzer','python','-c',
            "import os,sys; sys.stdout.write(os.environ.get('MCP_API_KEY',''))"],capture_output=True)
        if r.returncode: p.exit(1,'无法从 analyzer 读取 MCP_API_KEY，请在部署目录运行，或使用 --key-source prompt。\n')
        key=r.stdout.decode('utf8').strip()
    else:
        key=getpass.getpass('MCP_API_KEY: ').strip()
    if not key or any(c.isspace() for c in key): p.error('MCP_API_KEY 为空或格式异常')
    with ZipFile(sys.argv[0]) as template:
        entries={f'tender-presales/{n}':template.read(f'tender-presales/{n}') for n in FILES}
    entries['tender-presales/connection.private.json']=json.dumps(dict(url=url,api_key=key)).encode()
    # Private output only, never overwrite existing files.
    fd=os.open(args.output, os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'wb') as f,ZipFile(f,'w',ZIP_DEFLATED) as z:
            for name,data in entries.items(): z.writestr(name,data)
    except Exception:
        args.output.unlink(missing_ok=True)
        raise
    print('已生成内网专用包：',args.output)

if __name__=='__main__':
    main()
