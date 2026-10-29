#!/usr/bin/env bash
set -euo pipefail

# 在解压目录内创建独立虚拟环境，发布包已包含构建好的页面。
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "${project_dir}"
python_bin="${PYTHON_BIN:-python3.13}"
"${python_bin}" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -f .env ]]; then
    cp -- .env.example .env
fi
mkdir -p logs
echo '安装完成。请编辑 .env，配置独立分析库、源只读连接、文件根目录与模型。'
echo '然后执行 bash scripts/run.sh，浏览器访问 http://部署机:8100/ui/ 。'
