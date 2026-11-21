当前 full 工具见 [0.8.25 多维检索](structured-facts.md)。

0.8.24 全量实时查询与独立 Skill/CLI：[部署手册](full-tenders.md)。

售前数据集新版请看 [0.8.23 部署与查询](presales-snapshot.md)。以下为 legacy 模式。

# 招标分析 MCP 试用说明（0.8.22）

同库版本新增只读 MCP：`http://主服务器IP:8102/mcp/`，传输为 Streamable HTTP。直接读现有主库和分析 schema，无需迁移、重算或另建数据库。默认关闭，独立 `MCP_API_KEY` 配置后开启。管理接口仍用原 `ANALYZER_API_KEY`，两个密钥不得相同。

## 部署

使用新的 0.8.22 部署包替换程序文件，保留原 `.env.shared`、runtime 和密钥。更新包含新 Python 依赖，需要构建镜像；仅重启旧镜像不能获得 MCP。基础镜像、原有依赖仍使用内网源，不自动回退到公网。部署包附带新增 MCP 依赖的 wheels（Linux x86_64 / CPython 3.12），Dockerfile 自动通过本地 find-links 使用；文件 SHA256 已与 PyPI 对照，记录在 vendor/mcp-wheels/manifest.json。其他 CPU 架构需要内网提供对应 wheel。本机未执行内网 Docker 构建。

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

成功时会列出八个工具、销售项样例、进度、排名及项目清单。当前页有命中项目时继续测试详情和证据；无命中明确跳过，不算证据联调已验证。

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

## 售前商机与内网验收

售前询问近期跟进机会时，优先使用 `search_opportunities`。商机需同时满足当前来源的有效确认标签、主库早期阶段，以及查询时段内未撤销且已提取实际需求的采购公告/采购意向/更正公告。确认来源包括历史自动采纳，可用 `model_confirmed_only=true` 收紧。卡片区分主库当前阶段和上次分析阶段，需求摘录来自标明 notice_id 的区间公告。请按公告核对截止时间。公告可选字段缺失时对应值为 null。结果按时间倒序分页，每次最多 50 条，has_more 指示是否继续。省级六位编码包含省内地市。历史分析和市场统计仍可使用原工具。

部署时覆盖程序文件，保留 .env.shared 和 runtime。重建 analyzer 后可在部署目录执行以下命令。100 题验收脚本及题库已随镜像打包。默认查截至上海时间昨天的七天，自动选取项目及其真实标签；可追加 --end YYYY-MM-DD 或手动指定两组样本。无第二个标签的项目会产生“缺少现场样本”。题库保留部分当前工具未开放的参数，用来检验功能边界，能力限制与服务错误分列。

```bash
docker compose --env-file .env.shared -f compose.shared.yaml exec -T analyzer python scripts/evaluate_presales_mcp.py --output /tmp/presales-live.json
docker compose --env-file .env.shared -f compose.shared.yaml cp analyzer:/tmp/presales-live.json ./presales-live.json
```

若真实数据查询耗时高，先核对执行计划和表上现有索引。商机查询主要使用项目键、公告时间、当前阶段以及确认标签。压测中主库 notices 有 (project_key_name,publish_at DESC) 索引，项目视图的 project_key 可走索引；服务启动不自动给主库建索引。以 first_seen_at 做主要筛选时应另外核对该时间列的执行计划。

## 工具与边界

- `search_projects(filters, page, page_size)`：项目搜索，仅包含 matched/no_match/needs_review，总数和分页由数据库计算；按所选期间有公告的项目计数。
- `get_project_detail(project_key)`：当前采购需求、所选主公告/更正、公告列表、当前分析状态和标签。
- `get_match_evidence(project_key, sales_item_id)`：保存的判断、确认来源、公告/附件标识和原文片段。
- `list_sales_items(q, page, page_size)`：已启用销售项及描述、分类、产品。
- `aggregate_projects(filters, group_by, limit)`：按 sales_item（销售项）、category（分类）、product（产品）、channel（阶段）、region_code（地区编码）、analysis_status（处理状态）、date（日期）聚合整个筛选集合。
- `get_analysis_progress(filters)`：范围内已有分析结果的已完成、待复核、未匹配和命中统计；不扫描全库任务历史，不表示主库整体分析覆盖率。

软件项目统计的 `filters` 必填 `start_date`、`end_date`；`time_basis` 支持 publish_at（默认）和 first_seen_at。其他条件：analysis_status（matched/no_match/needs_review）、q、channel、region_code、sales_item_id、category、product、hit、model_confirmed_only、min_score。地区取所选日期范围内最新公告的 region_code；阶段和标题取已存分析结果，主库无该字段时筛选明确报错。相似度严格大于阈值，不是概率。

单次最多 7 天；每页最多 50，分组最多返回 100，截断会标记。跨日项目去重，但日期/销售项等多值分组不能相加当总项目数。SQL 从已有分析结果筛选，仅关联日期范围内公告的元数据，在数据库内分页和聚合。不再加载正文、重做需求提取或逐日扫描任务记录。同一工具的总数和清单使用一个只读事务快照，跨工具不保证同一快照；状态是当前处理状态，并非历史时点回放。

`matched`、`no_match` 计为结论完成，`needs_review` 单列待复核。未分析、缺材料、缺主公告、等待提取、未纳入范围均不在统计分母内。`hit=false` 表示筛选条件下无有效 confirmed 标签，不能直接解释为模型判定不相关。详情仍允许按已知 project_key 查看未分析项目。普通项目搜索不必先调用销售项工具。

生产耗时诊断使用 `scripts/diagnose_mcp_queries.sql`。先确认 ANALYSIS_SCHEMA，逐段执行两次并比较 Execution Time 与 Buffers。诊断 SQL 未加应用来源配置指纹筛选，统计是该 schema 的全部来源，用来定位瓶颈而非代替应用精确业务总数。应用日志增加 `MCP query completed tool=... elapsed_ms=...`，耗时含等待工具并发额度，不记录查询正文或密钥。

MCP 独立数据库连接池以只读事务执行，SQL 单次最长 30 秒、最多 4 个工具查询并发，不改变页面查询超时。没有任意 SQL、任意路径读取、模型调用、修改标签、任务启动或配置读取工具。密钥未配置返回 503，无效 token 返回 401，非同库模式返回 409。

## 验证边界

测试覆盖官方 MCP 客户端经真实 HTTP 调用、本地 PostgreSQL 13 的日期与去重口径、旧/禁用标签排除、认证隔离、只读事务和证据，也覆盖 2025-11-25 协议客户端兼容。内网 PostgreSQL 16、主库真实数据和其他厂商 Agent 客户端，需要部署后使用上述脚本及客户端继续验证；MCP 本身不验证模型标签准确性。

## 绿色硬件关键词：采购人和中标人分析

调用 `aggregate_hardware_projects`，无需先查软件销售项。先用 `{"filters":{},"group_by":"tag_l1"}` 浏览真实一级分类，再传 `filters.tag_l1` 并按 `tag_l2` 展开。对目标分类或二级词分别按 `buyer`、`winner` 分组即可得到采购人和中标人排行。`limit` 默认 30，最多 100。每组带一个示例 `project_key`，用于定位项目。

口径：全集是 `public.project_product_tag` 当前已命中硬件关键词的去重项目（上游构建时已剔除弱词、非有效公告）。无日期时，分类统计只读此表。指定 `start_date` 和 `end_date` 时，按主库 `notices.publish_at` 在日期范围内有有效公告的项目筛选，主体也仅取该期间公告；不能据此断言某硬件词首次出现于该期间。日期均含，最多 366 天。`built_at` 是标签重建时间。

采购人按有效公告 `buyer`，中标人限有效结果公告，剔除标题或类型含候选、废标、流标、终止的记录。按公告优先取 `lots.winner`，没有有效标段名称时回退 `notices.winner`。按去除首尾空白的原名称统计，不拆联合体名称、不推断未披露主体。`total_projects` 是筛选到的硬件项目数，`attributed_projects` 是有当前分组值的项目数，`unattributed_projects` 是缺少值的项目数；这些数不受 Top N 截断影响。多主体、多标签可跨组重复，同组同项目只计一次。项目标签与中标人同时出现不能证明该中标人供应了指定设备。

部署无需建表、建索引或重新分析；共享库账号需要能 SELECT `public.project_product_tag`、`public.notices`、`public.lots`。首次可将不带日期的 `total_projects` 与 `SELECT count(DISTINCT project_key) FROM public.project_product_tag` 对账。
