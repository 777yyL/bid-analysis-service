# 同库工作台部署说明

新版页面：每日概览、项目检索、运行管理、系统设置。独立原型仍位于 `designs/daily-workbench`；正式 UI 不使用原型数据。

## 数据与兼容性

- `SOURCE_MODE=shared` 直接读取 `DATABASE_URL` 内的 `public.notices`、`public.mv_project`、`public.attachments`。不使用另一个源库连接，不复制来源表，不下载远程材料。
- `ANALYSIS_SCHEMA=bid_analysis` 保存销售项、任务、分析结论、证据和配置；普通视图 `bid_analysis.v_project_sales_tags` 展示当前版本、已启用、已确认的标签。不会修改 public 业务表。
- 首次入库时间使用 `notices.first_seen_at`，支持 timestamp 有/无时区。需求字段为 `has_procurement_content` 和 `procurement_content`。每日统计直接从主库读取，同日多个公告按项目合并，公告数与项目数分开。短时缓存 30 秒，刷新概览强制重读；查询最多 7 天，每日超过 20000 个项目时明确报错，不截断计数。
- 同库模式只使用 MaaS 召回和模型逐项复核。不再自动接纳未经模型判断的高分候选。NULL 或成功标志对应空内容视为待提取；已有结果不会自动重算。
- 自动发现仅处理尚无分析记录、有完整已提取需求的项目。已失败任务需在运行管理重试；旧 missing_material、needs_review 等已有记录也不会自动覆盖，可在项目详情重新分析。
- 旧 `compose.yaml` 仍是原来的来源同步模式，升级不会自动切换数据库，也不会删除旧表。新模式使用单独的 `compose.shared.yaml`。

## 旧结果迁移必须先完成

使用包内 `scripts/migrate_to_shared.py` 一次性迁移。它从旧分析库读取，写入主库独立的新 schema；不是把整个旧数据库恢复进主库。默认只读检查，只有 `--apply` 才写入。目标 schema 已存在就停止，不覆盖已有数据。

| 迁移对象 | 说明 |
| --- | --- |
| sales_items_v1 | 销售项、描述、分类、产品与启用状态 |
| sales_projects_v1 | 已分析的项目与结果版本 |
| sales_matches_v1 | 全部标签，包括确认、排除、候选及原时间戳 |
| sales_jobs_v1 | 历史任务、日志进度及未完成队列 |
| sales_audit_v1 | 分析审计记录 |
| sales_runtime_v1 | 页面配置、旧来源标识与运行状态 |
| runtime/settings.key 文件 | 原配置密钥，用于解密已保存的接口密钥 |

不搬主库已有的公告、附件，不搬旧来源同步表、同步视图、公告向量缓存和下载材料。旧库保留作为备份。项目键不做转换，主库必须保留原 `project_key` 关联。迁移时保留 settings 中原 source_dsn 和 `_source_revision`，用于识别旧结果；同库模式不会用旧 source_dsn 建立来源连接。

影响范围：旧分析服务暂停，新主库增加独立 schema；主服务及其 public 业务表无需重启或修改。风险：迁移期间标签处理暂停，目标库会增加写入和磁盘负载。先备份，再执行；只重启分析服务，不重启 PostgreSQL 或 Docker。回滚步骤见下文。以下命令在 Linux 服务器执行。

### 1. 在旧服务器暂停并备份

在旧页面暂停任务，确认运行中为 0，再进入**旧部署目录**执行。保留 PostgreSQL 容器运行，不执行 `down -v`。若旧部署使用自定义 compose 文件，请沿用原来的 `-f`、`--env-file` 参数。

```bash
set -euo pipefail
umask 077
docker compose stop analyzer
backup_dir="migration-backup-$(date +%Y%m%d-%H%M%S)"
mkdir "${backup_dir}"
docker compose exec -T analysis-db pg_dump -U bid_analysis -d bid_analysis -Fc > "${backup_dir}/bid-analysis.dump"
docker compose exec -T analysis-db pg_restore -l < "${backup_dir}/bid-analysis.dump" > "${backup_dir}/contents.txt"
docker compose cp analyzer:/app/runtime/settings.key "${backup_dir}/settings.key"
```

备份和密钥含敏感信息，妥善保存。将备份目录通过内网复制到新服务器；迁移脚本需要从新容器连通旧分析库（之前的宿主端口通常为 55434，实际以部署为准）。备份用于恢复，不直接向主库执行整个 dump 的恢复。

如果停止时仍有 `running` 任务，脚本会拒绝迁移：用旧服务恢复任务并排空后再停，不批量篡改任务状态。

### 2. 在新服务器准备配置与密钥

解压新版包并进入目录。尚未填写过时复制 `.env.shared.example` 为 `.env.shared`，配置目标主库 `DATABASE_URL`、`ANALYSIS_SCHEMA`、API Key 和挂载目录。`ANALYSIS_SCHEMA` 默认 `bid_analysis`，必须尚不存在；如新服务已创建了空 schema，停止新服务后可另选 `bid_analysis_migrated`，不要直接删表。

将旧 `settings.key` 放到 `.env.shared` 中 `ANALYSIS_RUNTIME_DIR` 对应目录下。以下用默认 `/opt/bid-analysis/runtime` 举例，原密钥来源路径请换为实际路径。若目标已经有密钥，先核对用途并保留副本，不覆盖正在使用的文件。

```bash
sudo install -d -o 10001 -g 10001 -m 700 /opt/bid-analysis/runtime
sudo install -o 10001 -g 10001 -m 600 /实际备份目录/settings.key /opt/bid-analysis/runtime/settings.key
docker compose --env-file .env.shared -f compose.shared.yaml build analyzer
```

**此时只 build，不要 up。** 新服务提前启动会创建目标 schema，脚本随后会拒绝覆盖。目标账号需要读取 public 来源对象、在目标数据库创建 schema、管理自己结果表的权限。使用与后续服务相同的数据库账号执行迁移，避免对象所有者不一致。

### 3. 检查，然后迁移

在新部署目录执行。提示时输入旧库连接串，例如 `host=旧服务器 port=55434 dbname=bid_analysis user=bid_analysis password=实际密码`；特殊字符按 libpq 连接串规则填写。输入不回显，也不写入命令历史。它只用于本次搬库，迁移完成后不再使用。

```bash
read -r -s -p '旧分析库连接串：' MIGRATION_SOURCE_DSN
printf '\n'
export MIGRATION_SOURCE_DSN

# 先检查，不修改目标库
docker compose --env-file .env.shared -f compose.shared.yaml run --rm --no-deps -T \
  -e MIGRATION_SOURCE_DSN analyzer python - < scripts/migrate_to_shared.py

# 确认检查通过、旧 analyzer 已停止后执行
docker compose --env-file .env.shared -f compose.shared.yaml run --rm --no-deps -T \
  -e MIGRATION_SOURCE_DSN analyzer python - --apply < scripts/migrate_to_shared.py
unset MIGRATION_SOURCE_DSN
```

脚本默认从旧库 `public` 读取；旧表如在其他 schema，可在两个命令 `python -` 后均增加 `--source-schema 实际名称`。容器根文件系统只读不影响此方式，脚本通过标准输入运行，无须 `docker cp`。

脚本检查字段、密钥、暂停状态及运行中任务；从同一只读快照流式复制六张表，核对各表行数和当前有效标签数、命中项目数。目标建表和导入在一个事务内完成，中途异常则回滚。采用 Psycopg 的 [COPY 流式传输](https://www.psycopg.org/psycopg3/docs/basic/copy.html)，不将完整数据装入内存，不将历史 JSON 强制转换成 JSONB。

迁移保留所有原状态和时间戳，仅把目标 settings 中 `paused=true`、`incremental_enabled=false`、`history_enabled=false`。旧库配置不改；旧任务不会因为迁移被清空或重新执行。共享模式只消费 analyze/review_existing 队列，旧同步任务仅留存。

### 4. 启动并核对

使用下方“启动”命令启动新服务。先查看项目详情中的历史标签及系统设置的知识库/模型连接。主库执行：

```sql
SELECT '项目记录' AS 指标, count(*) AS 数量 FROM bid_analysis.sales_projects_v1
UNION ALL
SELECT '全部标签记录', count(*) FROM bid_analysis.sales_matches_v1
UNION ALL
SELECT '当前有效标签', count(*) FROM bid_analysis.v_project_sales_tags
UNION ALL
SELECT '当前命中项目', count(DISTINCT project_key) FROM bid_analysis.v_project_sales_tags;

-- 应为 0；非 0 表示新旧主库的项目键没有完全对应，需要先排查关联。
SELECT count(*) AS 主库找不到的历史项目
FROM bid_analysis.sales_projects_v1 p
WHERE NOT EXISTS (SELECT 1 FROM public.mv_project m WHERE m.project_key=p.project_key);
```

使用其他 schema 时替换 SQL 中 `bid_analysis`。有效标签数应与脚本输出相同（新任务尚未恢复时）。系统设置确认无误后，解除暂停并开启自动发现新增采购需求。旧分析服务继续保持停止，避免双边重复跑模型。

### 5. 回滚

若验证不通过，在新目录执行 `docker compose --env-file .env.shared -f compose.shared.yaml stop analyzer`；保留目标 schema 供排查。到旧目录确认原 `.env`、原镜像、数据库卷及 `settings.key` 均未更换，然后 `docker compose up -d --no-build analyzer`，检查旧服务 `/healthz`、页面标签和模型配置，再解除暂停。无需重启主服务、数据库或 Docker。若新服务已经处理了新数据，回滚前还需保存这部分结果，不能假设它们自动回到旧库。

在数据与密钥迁移并核验前，不要让空 schema 接管生产分析。应用账号只需 public 来源对象的 SELECT，以及 bid_analysis schema 中的建表、视图与读写权限。

## 启动

构建使用仓库现有 Dockerfile 和已经构建的 frontend/dist，基础镜像与 pip 均使用内网地址。先复制 `.env.shared.example` 为 `.env.shared`，填写主库地址、访问密钥与实际目录。容器使用 UID 10001，runtime 必须可写，材料目录需可读。数据库主机名必须能从容器访问，不能用容器自己的 127.0.0.1 指代宿主主库。

```bash
docker compose --env-file .env.shared -f compose.shared.yaml up -d --build
docker compose --env-file .env.shared -f compose.shared.yaml logs --tail=100 analyzer
```

此命令启动分析容器并创建/升级独立结果结构，不启动 PostgreSQL，也不删除任何旧数据。升级前备份结果 schema 和 runtime。回滚时停止该 compose 服务，使用原数据库配置、原 runtime 和旧镜像启动原服务；本版本没有删除式迁移。

进入系统设置，依次测试主库读取、配置知识库、复核模型，最后打开“自动发现新增采购需求”。正文对照目录填 `/data/bidinfo`。查看 `/healthz` 与运行管理，确认后台已启动；端口可达不代表模型或主库权限已验证。

## 验证边界

本地单元/API 测试可以验证统计、状态、版本过滤和鉴权。内网主库、MaaS 及正式数据迁移必须在部署环境联调；不能用原型数据或 Mock 页面测试替代该项验证。
