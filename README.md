# Bid Analysis Service

招标项目销售项分析服务，提供采购需求召回、模型复核、项目工作台和只读 MCP 查询。

- 后端：Python / FastAPI / PostgreSQL。
- 前端：React / TypeScript / Vite。
- 同库模式：读取 `public` 中的公告与采购需求，分析结果写入独立 schema。
- MCP：Streamable HTTP，独立 Bearer token；按已有分析结果查询、分页与聚合，支持证据追溯。

## 部署与使用

- [同库部署与迁移](docs/shared-workbench-deployment.md)
- [MCP 配置、测试及统计口径](docs/agent-mcp.md)
- [MCP 查询耗时诊断 SQL](scripts/diagnose_mcp_queries.sql)
- [Agent Skill](skills/bid-opportunity-analysis/SKILL.md)

使用 `.env.shared.example` 配置同库运行环境。实际配置、数据库备份、运行数据和密钥不应提交到 Git。
当前 Docker 配置使用内网镜像与包源，其他环境需要按部署条件调整。

MCP 默认关注已匹配、未匹配和待复核项目，不将未分析项目纳入统计分母。标签相似度不是概率，历史自动采纳与模型确认分别展示。

## 本地验证

后端安装 `requirements.txt` 与 `requirements-dev.txt` 后运行 `python -m pytest`。
PostgreSQL 集成测试需要显式配置专用测试库连接，缺少环境的用例会跳过。
前端在 `frontend` 目录安装锁文件指定的依赖后执行 `npm run build`。
构建前端后可运行 `python scripts/build_release.py` 生成部署包。
