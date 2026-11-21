# 招采雷达 0.8.25：全量业务事实检索

升级前保留现有 .env.shared、runtime、密钥；保持 MCP_DATASET_MODE=full，替换程序后在 /opt/bid-analysis-service 执行 `docker compose --env-file .env.shared -f compose.shared.yaml up -d --build --no-deps analyzer`。无需刷新快照、变更主库结构或回填历史标签。更新客户端工具发现，新增 list_fact_fields 和 query_facts。旧调用显式指定 digitalization 将收到参数错误，请用新版 Skill/CLI 或省略该参数。主题搜索已移除服务端默认扩词、软件采购规则和自动文件判读；原文仍可按ID核查。

## 先看现场字段，再生成查询

list_fact_fields({"entity":"lot"}) 返回标段及可关联的公告和项目字段、PG 类型、源库列注释及支持的过滤操作。表范围固定在 public.notices、mv_project、lots、attachments。读取 PG catalog，包含物化视图和当前账号可 SELECT 的列；没有某字段就不猜数据。暴露标量（常用字符、UUID、布尔、整数/小数、日期时间）及其数组。非结构化正文/概要、路径/密钥/指纹、二进制、JSON/JSONB、向量与未支持的专用类型不对外作为字段。联系信息等名称命中隔离规则的列不进目录；当前 API key 应限可信售前范围，不提供个人级权限。数据库新增字段应审查列名、数据内容和数据库授权。

选择一行代表什么：project 一行一个项目，notice 一行一条公告，lot 一行一个标段记录，attachment 一行一个附件。后两者可上查公告和项目；不会把两个一对多明细同时展开。notice.channel 取该公告阶段，project.channel 取项目当前阶段。要查中标人明细先按 lot.winner 筛标段；这是原字段检索，没有标段而只在公告记录的中标人需另查 notice.winner。冻结项目结果集上的买家/中标人排行沿用 aggregate_tenders 的标段优先、公告兜底口径。

多个 filters 默认 AND；同字段多值用 in；contains 为字面不区分大小写的子串；数组用 has_any；时间区间用 gte、lt（左闭右开，北京时间）。query_facts **不默认排除撤销记录**，需要时显式加 notice.revoke_status=active；scope_summary 不是可直接聚合的业务维度。预算、限价、成交金额三者不要混用；NULL 不等于零，未指定币种与单位时不要直接跨口径汇总。

明细选择 fields、order_by；聚合选择 group_by、measures（不与 fields 混用）。支持 count、distinct、sum、avg、min、max 和日/周/月/季度/年分桶。排序字段写返回列名，例如 count、distinct:notice.project_key_name、sum:lot.amount_value。只分页返回（默认30，最多100），计数对整个筛选集执行。要全局总数可单独用 measures=[{"op":"count"}]。分类组可交叉，不能将各组项目数相加。原文和附件核验仍用 get_notice_evidence。

## 示例：河南每月各采购方式涉及多少项目

以下是 query_facts 工具参数，可保存为 facts.json 后运行 `python scripts/tender_cli.py call query_facts facts.json`。如主库不具备其中某列，以现场字段目录为准。

```json
{"query":{"entity":"notice","filters":[
  {"field":"notice.province_code","value":"410000"},
  {"field":"notice.channel","value":"采购公告"},
  {"field":"notice.revoke_status","value":"active"},
  {"field":"notice.publish_at","op":"gte","value":"2026-08-19"},
  {"field":"notice.publish_at","op":"lt","value":"2026-11-20"}
],"group_by":[{"field":"notice.publish_at","period":"month"},{"field":"notice.procurement_method"}],
"measures":[{"op":"count"},{"op":"distinct","field":"notice.project_key_name"}],
"order_by":[{"field":"notice.publish_at:month"},{"field":"distinct:notice.project_key_name","desc":true}]}}
```

预算超过100万元、当前项目仍处采购公告阶段：entity=notice，在 filters 中加 notice.budget_value gt 1000000（确认主库单位为元）、project.channel eq 采购公告，截止日用 notice.deadline；仅筛项目阶段并不等同投标尚未截止。按已知采购人反查直接筛 notice.buyer。统计标段金额选 lot 粒度，按 lot.winner 分组，sum:lot.amount_value；不能在标段粒度求公告预算的和。即使不跨粒度，也需留意同项目多次公告重复披露同一金额。

主题候选用 search_tenders，Agent 提供显式词并核验 matched_terms、source、excerpt。aggregate_tenders 可带 project_keys，仅统计冻结集中的指定项目（空列表代表零项）。也可将 selection_id 传给 query_facts：selection_scope=notice 只匹配原冻结公告；project 则查看这些项目的其他公告或标段。冻结的是成员，结构化字段值为查询时主库事实，不是历史回放。
