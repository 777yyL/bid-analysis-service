#!/usr/bin/env bash
set -euo pipefail

# 前台运行便于进程管理器接管信号；模型调度要求单实例。
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "${project_dir}"
exec .venv/bin/python -m analyzer.serve
