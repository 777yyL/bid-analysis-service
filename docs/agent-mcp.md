# 招标分析 MCP 试用说明（0.8.20）

同库版本新增只读 MCP：`http://主服务器IP:8102/mcp/`，传输为 Streamable HTTP。直接读现有主库和分析 schema，无需迁移、重算或另建数据库。默认关闭，独立 `MCP_API_KEY` 配置后开启。管理接口仍用原 `ANALYZER_API_KEY`，两个密钥不得相同。

## 部署

使用新的 0.8.20 部署包替换程序文件，保留原 `.env.shared`、runtime 和密钥。更新包含新 Python 依赖，需要构建镜像；仅重启旧镜像不能获得 MCP。基础镜像、原有依赖仍使用内网源，不自动回退到公网。部署包附带新增 MCP 依赖的 wheels（Linux x86_64 / CPython 3.12），Dockerfile 自动通过本地 find-links 使用；文件 SHA256 已与 PyPI 对照，记录在 vendor/mcp-wheels/manifest.json。其他 CPU 架构需要内网提供对应 wheel。本机未执行内网 Docker 构建。

影响：重建分析容器会短暂中断请求。先在页面暂停并等待运行中任务结束；不改数据库结构和历史结果，不重启主服务或 PostgreSQL。回滚时停新版，用原 0.8.18 程序构建并启动，保留同一数据库、schema、runtime 和原配置。

在 `/opt/bid-analysis-service/.env.shared` 末尾添加独立密钥（可用 `openssl rand -hex 32` 生成，生成后不要发到聊天或日志）：

```dotenv
MCP_API_KEY='填写独立的随机密钥'
```

沿用 `SOURCE_MODE=shared`、`DATABASE_URL`、`ANALYSIS_SCHEMA` 和文件目录。然后执行：

```bash
cd /opt/bid-analysis-service
docker compose --env-file .env.shared -f compose.shared.yaml up -d --build analyzer
docker compose --env-file .env.shared -f compose.shared.yaml logs --tail=100 analyzer
curl -fsS http://127.0.0.1:8102/healthz
```

MCP 查询不要求解除调度暂停；先测 MCP 再按需恢复分析。MCP 只读密钥持有者可以读取本服务授权范围内的招标数据；第一版没有按部门/项目划分权限，也没有 OAuth 登录。适合可信内网 Agent 的服务端连接；跨网络请配置 HTTPS。浏览器 Origin 请求会被拒绝。

## 直接测试

包内脚本使用官方 MCP Python SDK，不经过 LLM，也不调用知识库/模型。默认测试北京时间昨天按发布时间的项目；可指定日期（一次最多 7 天）。输出含业务摘要与证据，按内部数据管理。

```bash
cd /opt/bid-analysis-service
docker compose --env-file .env.shared -f compose.shared.yaml exec -T analyzer \
  python - --start-date 2026-11-16 --end-date 2026-11-16 \
  < scripts/test_agent_mcp.py
```

容器内端口是 8100，因此脚本默认 URL 为 `http://127.0.0.1:8100/mcp/`；宿主 8102 仅用于外部客户端。脚本从容器环境读取 MCP_API_KEY，无需在命令里写密钥。需要核对某个项目时追加 `--project-key 实际项目键`；按入库时间查询追加 `--time-basis first_seen_at`。

成功时会列出六个工具、销售项样例、进度、排名及项目清单。当前页有命中项目时继续测试详情和证据；无命中明确跳过，不算证据联调已验证。

## 接入其他 Agent

在支持远程 MCP 和自定义请求头的客户端中配置：

| 设置 | 值 |
| --- | --- |
| 名称 | bid-analysis |
| 传输 | Streamable HTTP |
| URL | `http://主服务器IP:8102/mcp/` |
| 请求头 | `Authorization: Bearer 你的MCP_API_KEY` |

客户端字段名和环境变量引用语法各异，以上是连接参数，不是所有客户端通用的配置文件。不要将密钥存入 Skill。只支持 stdio 的客户端需要另配 HTTP MCP 桥接，不是把 URL 当作启动命令。

将 `skills/bid-opportunity-analysis/` 作为一个完整目录复制到目标 Agent 支持的技能目录（包含 SKILL.md）。不支持 Skill 的平台也能调用 MCP，并读取 `bid://analysis/glossary` 获取口径。

试用问题：

> 查询 2026-11-16 发布的项目，在已有分析结果中统计已完成、待复核及命中的数量；列出模型确认过的访客管理商机，附采购证据。请区分历史自动采纳和模型确认。

## 工具与边界

- `search_projects(filters, page, page_size)`：项目搜索，仅包含 matched/no_match/needs_review，总数和分页由数据库计算；按所选期间有公告的项目计数。
- `get_project_detail(project_key)`：当前采购需求、所选主公告/更正、公告列表、当前分析状态和标签。
- `get_match_evidence(project_key, sales_item_id)`：保存的判断、确认来源、公告/附件标识和原文片段。
- `list_sales_items(q, page, page_size)`：已启用销售项及描述、分类、产品。
- `aggregate_projects(filters, group_by, limit)`：按 sales_item（销售项）、category（分类）、product（产品）、channel（阶段）、region_code（地区编码）、analysis_status（处理状态）、date（日期）聚合整个筛选集合。
- `get_analysis_progress(filters)`：范围内已有分析结果的已完成、待复核、未匹配和命中统计；不扫描全库任务历史，不表示主库整体分析覆盖率。

`filters` 必填 `start_date`、`end_date`；`time_basis` 支持 publish_at（默认）和 first_seen_at。其他条件：analysis_status（matched/no_match/needs_review）、q、channel、region_code、sales_item_id、category、product、hit、model_confirmed_only、min_score。地区取所选日期范围内最新公告的 region_code；阶段和标题取已存分析结果，主库无该字段时筛选明确报错。相似度严格大于阈值，不是概率。

单次最多 7 天；每页最多 50，分组最多返回 100，截断会标记。跨日项目去重，但日期/销售项等多值分组不能相加当总项目数。SQL 从已有分析结果筛选，仅关联日期范围内公告的元数据，在数据库内分页和聚合。不再加载正文、重做需求提取或逐日扫描任务记录。同一工具的总数和清单使用一个只读事务快照，跨工具不保证同一快照；状态是当前处理状态，并非历史时点回放。

`matched`、`no_match` 计为结论完成，`needs_review` 单列待复核。未分析、缺材料、缺主公告、等待提取、未纳入范围均不在统计分母内。`hit=false` 表示筛选条件下无有效 confirmed 标签，不能直接解释为模型判定不相关。详情仍允许按已知 project_key 查看未分析项目。普通项目搜索不必先调用销售项工具。

生产耗时诊断使用 `scripts/diagnose_mcp_queries.sql`。先确认 ANALYSIS_SCHEMA，逐段执行两次并比较 Execution Time 与 Buffers。诊断 SQL 未加应用来源配置指纹筛选，统计是该 schema 的全部来源，用来定位瓶颈而非代替应用精确业务总数。应用日志增加 `MCP query completed tool=... elapsed_ms=...`，耗时含等待工具并发额度，不记录查询正文或密钥。

MCP 独立数据库连接池以只读事务执行，SQL 单次最长 30 秒、最多 4 个工具查询并发，不改变页面查询超时。没有任意 SQL、任意路径读取、模型调用、修改标签、任务启动或配置读取工具。密钥未配置返回 503，无效 token 返回 401，非同库模式返回 409。

## 验证边界

测试覆盖官方 MCP 客户端经真实 HTTP 调用、本地 PostgreSQL 13 的日期与去重口径、旧/禁用标签排除、认证隔离、只读事务和证据，也覆盖 2025-11-25 协议客户端兼容。内网 PostgreSQL 16、主库真实数据和其他厂商 Agent 客户端，需要部署后使用上述脚本及客户端继续验证；MCP 本身不验证模型标签准确性。
