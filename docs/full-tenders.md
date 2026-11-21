**0.8.25 已调整为只读事实检索，请优先使用 [多维检索与升级说明](structured-facts.md)。下文仅对应 0.8.24。**

# 0.8.24：全量招采 MCP 和售前 Skill

## 升级和回退

保留数据库、runtime、原 .env.shared 和密钥，解压服务包覆盖程序文件。在 .env.shared 声明 MCP_DATASET_MODE=full，保留 SOURCE_MODE=shared、原 MCP_API_KEY、DATABASE_URL、SOURCE_FILE_ROOT=/data/bidinfo 和主数据目录只读挂载。确认没有需要保护的运行中分析任务后，在部署目录重建 analyzer：

```bash
cd /opt/bid-analysis-service
docker compose --env-file .env.shared -f compose.shared.yaml up -d --build --no-deps analyzer
docker compose --env-file .env.shared -f compose.shared.yaml exec -T analyzer python /app/tender-skill/scripts/tender_cli.py tools
docker compose --env-file .env.shared -f compose.shared.yaml exec -T analyzer python /app/tender-skill/scripts/tender_cli.py report --region 410000 --term 生态环境 --start 2026-08-19T00:00:00 --end 2026-11-20T00:00:00 --csv /app/runtime/henan-eco.csv
```

容器 CLI 使用当前 MCP_API_KEY 与 127.0.0.1:8100，不需要传密钥。CSV 为两张排行前 N 项，文件已存在时请换文件名。独立 Skill 包解压后把整个 tender-presales 目录交给 Agent；客户端不支持原生 Skill 时可将 SKILL.md 作为工具说明，连同一个 MCP 地址。外部 CLI 设置 MCP_URL 指向可达的完整 /mcp/ 地址。完整工具发现应恰为 get_data_coverage/search_tenders/aggregate_tenders/get_project_timeline/get_notice_evidence。

full 不执行迁移，不需要刷新 presales，不读取软件销售项、硬件标签、bid_chunks 或复核表。主工作台的设置、worker和任务逻辑不变。新工具只读 public.notices、public.attachments、public.lots，按主库事实即时查询。恢复旧工具：MCP_DATASET_MODE 改回 presales 或 legacy 后重新 up -d --no-deps analyzer，再刷新 Agent 的工具发现。若仍有旧客户端要同时使用旧工具，请先在独立端口验收，勿直接替换它们的服务入口。

## 河南问题的核验口径

“河南近三个月生态环境项目，谁采购谁中标”：默认招标/意向/更正公告时间和地区，用 terms 识别主题。服务端按买方实际软件及信息系统交付的词组规则初筛，不是销售项目录，不是 LLM 全库语义判定。地域可以选择多个省市；业务范围是查询条件，不按人员隔离。信息化语句缺乏依据的候选单列，**待核实不等于漏审任务，也包括纯设备、工程等没有交付信号的候选**。采购人按匹配公告的记录名称；中标人来自入选项目有效结果，标段优先按公告回退，不拆联合体；候选公示和终止结果不计入中标。不能断言中标单位提供了某个具体模块。查“本季度公布的结果”请切 events=awards，此时赢家限制在命中的结果公告。

关键词预筛字段：公告标题，加非空 procurement_content（为空用 scope_summary）；不是二者无条件混搜。历史概要可进入检索，但不冒充原文。若候选未通过数字化规则，有界地回查挂载正文和附件文本：一组最多30条候选、每条最多2份附件、整组最多20次附件检查；自动核验单文件至多2MB、读取成功合计8MB，每份检查前12万字符，仅辅助核验。只出现在未索引文件且标题/需求/概要不含查询词的公告不会召回，不报告为全行业总数。get_notice_evidence 可单独核对任一已知公告，用 att_id / notice_id 限制归属；读 notices.content_path、attachments.text_path，遇到缺文件、越界、超限时显式说明，不返回宿主路径。

覆盖统计是全库结构化计数，不套用信息化口径。要对账 21:00～09:00 入库量：用 get_data_coverage，clock=first_seen_at、events=all、active_only=false、regions=[]（不要求项目键）。文本路径数不代表成功解析数。

单个 search 使用 PostgreSQL 只读一致性快照，连同主体/中标记录一起冻结。结果集是当前 MCP 单进程内存中最近8组、最长15分钟，服务重启失效；同一共享密钥对应同一授权身份，不支持个人 ACL。仅入选且有项目键的项目进主体排行。宽查询超过5万候选或容量上限会显式报错，请缩小地区、日期或主题；禁止把区间拆分后直接相加当去重总数。生产时延取决于全量源表索引、文本规模及文件挂载，旧版快照的100题耗时不能套用。首次上线建议从河南单市一周到全省季度逐步测量；MCP 数据库查询保留已有的30秒语句超时。
