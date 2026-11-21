# 售前数据集与 MCP · 0.8.23

**用途**：给区域售前一份已筛选、可追溯的业务数据。工作台继续使用原库；新增模式的 MCP 只查询同库 presales schema，不读任务日志、模型配置或原始公告表，不调用模型。旧客户端保持 legacy 模式不变。切到 presales 后同一地址只发布 analyze_presales，旧工具名不再可用，请在 Agent 客户端刷新工具发现。

## 数据进入和退出

源项目必须在 public.mv_project 中；有有效采购需求公告、有当前有效 confirmed 软件项、或有 project_product_tag 的项目进入 project。notice 保留这些项目的有效公告（包括中标结果），仅在源字段确认有需求时复制需求文本。software 排除停用、旧版本、候选、排除、旧来源项目；保留确认方式。project_hardware 为项目级标签；hardware 取 public.notice_product_tag 的 notice_id、tag_l1、tag_l2、hits、evidence、tagged_at，过滤弱词、无效公告，并与当前项目硬件标签核对。标签没有标题/正文来源字段，不能说“已核实来自正文”。winner 取有效结果公告，排除标题/类型包含候选、废标、流标、终止的记录；有效标段中标人优先，缺失时按公告回退，保留公告 ID、标段 ID/编号。主体按原名称去除首尾空格，不自动拆联合体或做企业归并；不能代表最终合同履约方。

v1 采用事务内全量重建：同一源库快照生成临时表，再一次提交；原有可用版本在构建失败时保留。删除、撤销、改标会在下次成功刷新后生效。读响应含 refreshed_at；不会因 Agent 查询而刷新。不是同步到远端，也没有自动启动定时任务。首次运行需要足够空间和 WAL 容量；先在低峰试一次、记录耗时和数据库负载，勿高频重复全刷。例行刷新应排在主服务数据采集、项目物化视图和项目硬件标签刷新成功之后。

## 内网部署

1. 保留原 .env.shared、runtime、密钥与数据库卷，使用发布包更新程序。首次先保持 MCP_DATASET_MODE=legacy。在主库中由 DBA 为刷新账号预建并授权 presales schema（或使其能首次创建）、允许临时表、读取源表和分析结果。运行 MCP 的账号至少可读 presales。刷新所需源表：public.mv_project、notices、lots、project_product_tag、notice_product_tag，以及 ANALYSIS_SCHEMA 的 sales_runtime_v1 / sales_projects_v1 / sales_matches_v1 / sales_items_v1。无需修改主服务表。
2. 在已有部署目录执行以下命令，先构建镜像，并用一次性同配置容器跑首次刷新（不会启动 worker）。确认计数，特别是 hardware、project_hardware、winner 和 snapshot=1：

```bash
vim
```

3. 首刷成功后，设置 .env.shared 中 MCP_DATASET_MODE=presales，保留 MCP_API_KEY。重建 analyzer 容器会短暂中断服务，请安排窗口；无需重启主库：

```bash
docker compose --env-file .env.shared -f compose.shared.yaml up -d --no-deps analyzer
docker compose --env-file .env.shared -f compose.shared.yaml exec -T analyzer python -m scripts.validate_presales --url http://127.0.0.1:8100/mcp/ --end 2026-11-18 --output /app/runtime/presales-live.json
```

脚本从容器环境读 MCP_API_KEY。--end 改为现场验收日。live_observed 表示真实查询返回并满足基本约束，不是业务准确率。用 questions/arguments 与实际 Agent 对照时，请让它自己选择参数。例行手动或上游调度执行 `docker compose --env-file .env.shared -f compose.shared.yaml exec -T analyzer python -m scripts.refresh_presales`。回滚仅需改回 legacy 并再次 up -d，无需删表。两种模式共用原有认证；共享 key 不能强制按个人辖区隔离。若组织要求个人权限，不要将共享 key 当作授权隔离方案。

## 给 Agent 的调用契约

先读 bid://analysis/glossary。日期包含两端，最多366天。regions 为空是全国；420000 为湖北全省，含仅省级记录；420100 和 420200 为武汉和黄石的并集，不含仅省级记录。地区是公告地区，不是实施地或中标企业注册地。机构名称、销售项和硬件词精确匹配；不知道硬件词时先用 hardware_types 探索。按项目统计时各组可重叠，不能跨组相加。查询失败不可解释为零条。刷新时点以前的事实才可见。CRM 跟进人、成交预测、项目具体设备供应关系等问题，应明确数据不足。

例如在 2026-11-18，用户如指截至当天的三个日历月（含截止日、从 8 月 19 日算起），查湖北招标中硬件相关项目的中标人：

```json
{"filters":{"start_date":"2026-08-19","end_date":"2026-11-18","regions":["420000"],"scope":"hardware"},"mode":"winners","page_size":30}
```

导出列：value 中标人、project_count 去重项目数、hardware_types 各一级/二级类型和涉及项目数。total_projects 为招标样本，represented_projects 为有可统计主体的项目；unrepresented_projects 披露缺口。排名分页，has_more 为真继续读。类型列表最多50项，实际数量见 hardware_type_count。引用 example_project_key、example_award_notice_id 核验；需要明细时，同样参数加 winner 或 project_key，mode 改 awards，返回公告与标段。招标公告日期、地区、硬件词必须同一公告满足；取关联中标事实时不要求结果公告也位于该查询周期或同一城市。软件标签为项目级，不能解释为该公告产生的标签。stage=early 是项目当前阶段，不代表投标截止日尚未过去。

业务发现用 projects；查公告依据用 notices（需求摘录最多500字，硬件证据最多10条、每条250字，卡片类型和软件名各最多30项）；找客户用 buyers，市场结构用 regions、sales_items、hardware_types。scope=demand 看有需求的公告，untagged_demand 看有需求但整个项目没有业务标签的线索；均非主库全量新增。可指定 buyer、winner、sales_item、model_confirmed_only 和 project_key。投标资格、中标价、预算、精确归属于标段的商品等本版不做推断。

## 复现和对账

本地 100 轮为 10 类问题 × 10 种地区/日期边界；经官方 MCP 客户端、HTTP 和一次性 PostgreSQL 执行，根据独立手工标注的项目、公告、主体、硬件类型核对。覆盖同一供应商多标段、多市并集、省级未定城市、同项目跨省、弱词、撤销公告、候选人公示、空区域、无标签真实需求。另测分页、刷新撤标和刷新失败保留原快照。测试没有向外部 LLM 发送数据。详见 reports/presales-snapshot-100.json。100轮不是100种独立业务意图，也不衡量第三方模型自主选参的准确率。

内网验收建议：先按一个城市和一周人工逐条核对 10 个招标公告；用原始 notice_product_tag 反查一个关键词、用 lots 核对一个多标段项目，再扩大到全省季度。对无项目/无中标记录的范围要区分“零命中”和“缺披露”。同一自然语言让 WorkBuddy 自主调用，核对 regions、日期、scope 和 mode，而非仅检查输出是张表。
