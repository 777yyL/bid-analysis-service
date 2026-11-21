# 招标数据采集 · 前端分析报告

> 分析对象：`frontend/`（React 19 + antd 5 + Vite 7），共 14 个源文件、约 2100 物理行。行号均基于当前工作区文件。

## 1. 技术栈与构建方式

**依赖（frontend/package.json）**
- 运行时：`react@19.1.1`、`react-dom@19.1.1`、`antd@5.27.6`、`@ant-design/icons@5.6.1`；无路由库、无状态库、无请求库、无图表库（趋势图是手写 div 柱状）。
- 开发：`vite@7.1.12`、`@vitejs/plugin-react@5.0.4`、`typescript@5.9.3`、`@types/react@19.1.16`、`@types/react-dom@19.1.9`。
- 脚本只有 `dev`（`vite --host 127.0.0.1`）、`build`（`tsc --noEmit && vite build`）、`typecheck`；**无 lint、无测试、无 prettier 配置**，质量门只靠 `tsc`。
- `version: 0.5.0`，与 `analyzer/__init__.py:3` 的 `__version__ = "0.5.0"` 对齐。
- 包管理器实际是 pnpm（`pnpm-lock.yaml`、`pnpm-workspace.yaml` 存在，无 `package-lock.json`），但发布脚本 `scripts/build_release.py:11` 提示 `cd frontend && npm ci && npm run build`，提示语与锁文件不一致。

**Vite（frontend/vite.config.ts）**
- `base: '/ui/'`（第 4 行）；开发服务器未指定端口（默认 5173）。
- 代理（第 5 行）：`/api`、`/knowledge`、`/config` → `http://127.0.0.1:8100`；**未代理 `/healthz`、`/source`**（前端未用，暂无影响）。注意 v3 请求是绝对路径 `/api/v3/...`，落在 `/api` 前缀内，因此也被代理。
- 构建只有 `chunkSizeWarningLimit: 1200`（第 6 行），**没有 `manualChunks`/代码分割**。

**TypeScript（frontend/tsconfig.json）**
- `target/lib ES2022`、`moduleResolution: Bundler`、`jsx: react-jsx`、`strict`、`noUnusedLocals`、`noUnusedParameters`、`noEmit`、`include: ["src"]`。
- 未开 `noUncheckedIndexedAccess`；无路径别名；无 `types: ["vite/client"]`（所以 `import.meta.env` 基本不用）。

**dist 如何被 /ui 托管**
- `analyzer/app.py:250-252`：`ui_path = Path(__file__).parent.parent / "frontend" / "dist"`，存在则 `app.mount("/ui", StaticFiles(directory=ui_path, html=True), name="ui")`；`analyzer/app.py:145-147` 根路径 302 到 `/ui/`（否则 `/docs`）。
- 现存产物：`frontend/dist/index.html` 引用 `/ui/assets/index-BzKx2OJW.js`（**1,239,187 B 单 chunk**）与 `/ui/assets/index-BBfWdMqF.css`（18,643 B）。`chunkSizeWarningLimit: 1200` 恰好把 1.2MB 的警告压掉。
- `Dockerfile:29` `COPY frontend/dist ./frontend/dist`：镜像内无 Node，**前端必须宿主/CI 预构建**，否则构建失败。`frontend/dist/` 已被 `.gitignore` 忽略（`frontend/.gitignore` 与根 `.gitignore` 都有），产物不进仓库但仍留在工作区。
- `index.html` 极简：`lang="zh-CN"`、`theme-color #142c40`、标题「标析 · 招标分析工作台」，无 favicon、无 CSP、无 noscript。

## 2. 路由与页面结构

**路由机制**：无 react-router，自写 hash 路由。`frontend/src/workbench.tsx:13` `route() {return window.location.hash.slice(1) || '/sales-projects';}`，`workbench.tsx:18` 监听 `hashchange` 并 `window.scrollTo(0,0)`；`workbench.tsx:24-34` 是一条 if/else 分发链；`workbench.tsx:23` 把当前 path 映射到菜单 key（未知路径一律回落到 `/history-overview`）。默认落地页是 `/sales-projects`，默认内容是 `<Dashboard/>`（因此 `/history-overview` 与任何未知 hash 都渲染总览页，无 404 兜底）。

导航（`workbench.tsx:22`，8 项）：`/sales-projects` 销售项项目、`/sales` 销售项能力、`/sales-settings` 系统配置、`/projects` 历史分析、`/history-overview` 历史总览、`/notices` 历史公告与证据、`/tasks` 历史任务、`/lexicon` 历史词库。

**页面清单与端点**（前端统一走 `api()`，默认前缀 `/api/v2`；`direct=true` 时不加前缀）

| 视图 | 文件:行 | 主要交互 | 端点（方法 + 路径） |
|---|---|---|---|
| 登录/连接 | `frontend/src/workbench.tsx:19,35` | 输入 API Key 校验后进入；已存 key 自动重连 | GET `/config`（direct） |
| 分析总览 | `frontend/src/dashboard.tsx:10` | 5s 轮询；指标卡、日趋势、状态环、业务/产品分布；跳转任务/公告/词库 | GET `/overview` |
| 公告与证据（列表） | `frontend/src/notices.tsx:13` | 关键词/维度/业务域/产品分类/标签状态/分析状态筛选，分页 20，多选批量复核，导出当前页 CSV | GET `/notices`；POST `/project-analysis/reanalyze`；POST `/notices/{id}/reanalyze` |
| 公告详情 | `frontend/src/notices.tsx:53` | 5s 轮询；正文/附件 Tab + 命中词高亮、证据抽屉、四类标签（当前/规则/模型/人工）、人工纠正弹窗、处理历史时间线 | GET `/notices/{id}`；GET `/lexicon`；POST `/notices/{id}/review`；POST `/project-analysis/review`；POST `/project-analysis/reanalyze`；POST `/notices/{id}/reanalyze` |
| 项目工作台 | `frontend/src/projects.tsx:14` | 5s 轮询；搜索、分页 20、多选批量分析、行内分析 | GET `/projects`；POST `/project-analysis/reanalyze` |
| 项目详情 | `frontend/src/projects.tsx:56` | 5s 轮询；主公告/更正/未纳更正、选择依据、模型分流、需求版本、项目标签、公告时间线、证据抽屉、人工确认弹窗 | GET `/projects/{key}`；GET `/lexicon`；POST `/project-analysis/reanalyze`；POST `/project-analysis/review` |
| 任务与模型 | `frontend/src/tasks.tsx:18-19` | 5s 双轮询；暂停/恢复、模型开关与 RPM、同步进度（history/incremental）、同步弹窗、失败重试、停止拉取 | GET `/overview`；GET `/tasks`；POST `/tasks/control`；POST `/tasks/{id}/retry`；POST `/sync` |
| 词库与规则 | `frontend/src/lexicon.tsx:11,26-29` | 版本/质量模式编辑、业务词/产品词/业务线三表增删改、发布版本、源词库同步、TXT 导入、样例试匹配 | GET `/lexicon`；PUT `/lexicon`；POST `/knowledge/sync`（direct）；POST `/lexicon/import-files`；POST `/lexicon/trial` |
| 销售项能力 | `frontend/src/sales.tsx:20` | 5s 轮询；销售项 CRUD、CSV/JSON 导入预览、向量化、启用开关、反查公告、语义检索、任务表 | GET `/api/v3/sales-items`；GET `/api/v3/jobs`；POST `/sales-items/import`；PUT `/sales-items/{id}`；POST `/sales-items/{id}/enabled`；POST `/sales-items/vectorize`；POST `/sales-items/{id}/search`；POST `/search`；POST `/jobs/{id}/retry` |
| 销售项项目 | `frontend/src/sales-projects.tsx:10` | 5s 三轮询；统计卡、扫描进度、启停历史/增量、项目/分类/多销售项/匹配模式筛选、分页 30 | GET `/api/v3/projects`；GET `/api/v3/overview`；GET `/api/v3/sales-items`；POST `/api/v3/analysis/start`；POST `/api/v3/analysis/stop` |
| 商机详情 | `frontend/src/sales-projects.tsx:11` | 主公告/更正/选择依据、逐销售项匹配卡与证据抽屉、人工确认/排除、重新匹配 | GET `/api/v3/projects/{key}`；POST `/api/v3/projects/{key}/review`；POST `/api/v3/analysis/start` |
| 系统配置 | `frontend/src/sales-settings.tsx:6-8` | 主库 DSN、向量模型、复核模型、召回阈值、调度开关、密钥脱敏+清除、连接测试、物化视图刷新 | GET/PUT `/api/v3/settings`；POST `/api/v3/source/test`、`/embedding/test`、`/review/test`、`/views/refresh` |

**未使用的后端端点**：`/api/v2/notices/export.csv`（`analyzer/v2_api.py:91`，服务端 CSV，前端改在客户端 `exportCsv`）、`/api/v2/notices/import`（`v2_api.py:119`）、`/api/v3/views/status`（`sales_api.py:100`）、`/source/check`（`analyzer/app.py:171`）。

## 3. 状态管理、数据获取、鉴权

- **状态管理**：零全局状态。每个页面 `useState` + 两个自写 hook：`useResource<T>(path, pollMilliseconds)`（`frontend/src/common.tsx:14`）与 `useSalesResource<T>(path, interval=5000)`（`frontend/src/sales.tsx:7`）。跨页共享仅靠 `sessionStorage` 里的 key 和 hash。
- **useResource 设计**：`AbortController` + 递增 `sequence` 丢弃过期响应；`inFlight` 互斥；卸载时 `abort()` 并自增序号；轮询用 `setInterval` → `load(true)`（silent，不触发骨架）；返回 `{data,error,loading,refresh}`。
  - **关键缺陷**：`common.tsx:22` `if (inFlight.current) return;` 会让“用户操作后的刷新”被静默吞掉——当 5s 轮询请求正在飞时，`await state.refresh()` 立即返回、数据未重取，但调用方仍弹“操作已提交”；同理筛选/翻页导致 `path` 变化时，新请求若撞上在飞的旧请求会被丢弃，界面最多滞后一个轮询周期。
- **useSalesResource**：没有 in-flight 互斥、没有 abort（`sales.tsx:10-11`），慢响应时会叠加并发；`loading` 只在首个响应后置 false。
- **数据获取模式**：几乎全部是“每 5 秒全量拉当前视图”，包括详情页（`notices.tsx:53` 每 5s 拉公告正文+附件全文、`projects.tsx:56` 每 5s 拉项目全部公告），单页可达 2–3 路轮询；`tasks.tsx:19` 同时轮询 overview 与 tasks；`sales-projects.tsx:10` 同时三路。无 ETag/条件请求/增量游标，也无 React Query 类缓存与去重。
- **错误处理**：`api()` 在非 2xx 时读 `detail`，字符串则抛出原文，否则抛 `请求失败 (${status})：${JSON.stringify(detail||'')}`（`frontend/src/api.ts:9-13`）。页面层：`LoadState` 渲染 `Alert + 重试`（`common.tsx:46-50`）；写操作多用 antd `message`。缺陷：`lexicon.tsx:23` 空 catch；`sales.tsx:25-27` 多处只有一行动作提示、无重试；无 ErrorBoundary，任意字段缺失（如 `dashboard.tsx:24` 直接读 `data.metrics.llm_calls`）会白屏。
- **鉴权**：API Key 存 `sessionStorage`，键名 `bid-analysis-api-key`（`api.ts:1-3`），**不用 localStorage**（登录页文案亦声明“仅保存在当前浏览器标签页会话中”，`workbench.tsx:35`）。每个请求带 `X-API-Key` 与 `Content-Type: application/json`（`api.ts:7`）；无 key 时发送空字符串头。登录态不是 token，而是“一次 `GET /config` 成功”即 `connected=true`，因此**刷新页面必重新校验**，且校验期间 `connected=false` 会先渲染整张登录页（`workbench.tsx:15,35`）——已登录用户会看到闪屏。退出（`workbench.tsx:36`）清 key 但不清理任何页面缓存数据。
- 后端侧对应 `analyzer/app.py:122-131`：`APIKeyHeader(name="X-API-Key")`，未配置 `ANALYZER_API_KEY` 返回 503「请先配置 ANALYZER_API_KEY」，不匹配返回 401「API key 无效」。

## 4. 后端契约（types.ts）与业务概念

`frontend/src/types.ts` 19 行，关键结构：
- `Dimension = 'business' | 'product'`；`TagStatus = 'confirmed' | 'candidate' | 'excluded'` —— 双维度标签的最小contract。
- `Evidence {source,text,term?,start?,end?,notice_id?,notice_title?,notice_type?,section_role?:'demand'|'process'|'unknown'}`：`source` 形如 `body`、`metadata`、`attachment:{att_id}`（与 `notices.tsx:78,84` 的 Tab key 对齐）；`section_role` 用于区分“采购需求 / 招标流程”，即“未命中不等于无关”的证据基础。
- `Tag {dimension,category,label,status,source,reason,evidence[]}`：`source ∈ rule|model|manual`（`common.tsx:7` 翻译为 规则/模型/人工）。
- `Notice`（列表项）含 `analysis_status`、`material_complete`、`tags`；`NoticeDetail` 追加 `notice: NoticeMaterial{body?, attachments[{att_id,filename,text}]}`、`rule_tags/model_tags/manual_tags`、`warnings`、`history`、`project_analysis?: ProjectAnalysis[]`。
- `Project`/`ProjectDetail`/`ProjectAnalysis`：核心是“一个采购单元选一份主公告 + 对应更正”。`ProjectAnalysis` 含 `plan_id`、`unit_key`、`primary_notice_id|null`、`primary_type`、`correction_notice_ids`、`ignored_correction_ids`、`selection_reason`、`model_route/model_route_reason`、`revision`、`round_key?`、`lot_key?`、`material_complete`、`warnings`。业务规则在 UI 明示：**主公告优先级 采购公告 → 采购意向；更正公告修订需求；结果/合同/验收只更新进展**（`projects.tsx:26`、`notices.tsx:82`）。
- 词库：`BusinessLine{business_domain,business_line,body_md}`、`BusinessKeyword{business_line,word,kind,term_family?,enabled}`（kind ∈ core/ext/scene/tech/negative，见 `lexicon.tsx:47`）、`ProductKeyword{category,word,strength:'strong'|'weak',enabled,exclude_patterns}`、`Knowledge{version,lines,keywords,product_keywords,quality:'full'|'keywords_only',source_rubric?,warnings?}`。**业务维度两级（业务域→业务线），产品维度（产品分类→关键词），弱词只形成候选、排除词按上下文生效**（`lexicon.tsx:38`）。
- 调度/同步：`Scheduler{paused,model_paused,llm_enabled,llm_rpm}`；`SyncState{phase,page_size,last_count,material_total,material_processed,expanded,processed,projects,projects_processed,next_retry_at,cursor,error,enabled,updated_at}`。
- 总览：`Overview{model_routing?.no_hit_sample_percent,total_notices,tagged_notices,model_confirmed,model_reviewed?,needs_review,material_incomplete,project_total?,project_analyzed?,project_pending_material?,counts_by_status,business_distribution,product_distribution,daily,metrics,scheduler,knowledge_id?,sync?}`。`dashboard.tsx:12-14` 用 `project_total !== undefined` 判断后端是否已切成“项目口径”，同一页面兼容两代契约。
- 商机（v3）类型**没有进 types.ts**：`frontend/src/sales.tsx:14-16` 与 `frontend/src/sales-projects.tsx:6-9` 各自重复声明 `SalesItem/SalesJob/Project/Match/Evidence/Overview`，且 `sales-projects.tsx:6` 的 `Evidence{chunk_id,notice_id,attachment_id?,content,page_start?,page_end?,source_type?}` 与 `types.ts:3` 的 `Evidence` 是两种不同结构、同名——契约漂移的典型隐患。

## 5. UI/UX 与代码质量观察

**组件规模与格式**
- 体积严重不均：`notices.tsx` 18.4KB 却只有 94 物理行（单行 JSX 常 2–3KB）；`projects.tsx` 16.5KB/135 行；`sales-projects.tsx` **9.5KB/11 行**（第 10 行 3792 字符、第 11 行 3833 字符）；`sales.tsx` 9.3KB/31 行；`sales-settings.tsx` 6.0KB/14 行（第 12 行 2879 字符）。前四个页面是正常 prettier 风格，后四个是整段压成一行，**同一仓库两种代码风格**，diff/评审成本极高。
- 无 `components/` 目录，页面文件即组件；`NoticeView`、`ProjectView` 各自 40+ 行的单行 JSX，等于把 3–4 个组件塞进一个函数。

**重复代码**
- 标签卡（tag-card）渲染块两份：`notices.tsx:93`（`TagCards`，带高亮定位）与 `projects.tsx:114`（内联，直接 `setEvidence`），已出现行为分叉。
- 人工确认 Modal 两份：`notices.tsx:88`（校验 `whitespace`、词库选项无兜底）vs `projects.tsx:124-132`（把 `editing.tag` 合并进选项兜底），规则不一致。
- 两套资源 hook（`common.tsx:14` / `sales.tsx:7`）、两套状态文案表（`common.tsx:6` 的 `statusText` vs `sales.tsx:16` 的 `salesStatus`）、两套 CSV 逻辑（`api.ts:22` 客户端 vs 后端 `/notices/export.csv` 闲置）、两套“分析状态”下拉标签映射（`notices.tsx:32` 内联对象 vs `common.tsx:6`）。
- 词库下拉选项拼装逻辑在 `notices.tsx:62-63` 与 `projects.tsx:68-71` 近似复制。

**缺失的加载/错误态**
- `notices.tsx:33` 与 `projects.tsx:29` 写死 `<LoadState loading={false}>`：首屏永远不出骨架，只靠 Table 自身 loading，错误 Alert 会直接顶掉表格。
- 词库页完全没有 `LoadState`：加载中只剩标题与试匹配卡片；失败时用“新建词库”按钮兜底（`lexicon.tsx:32`），语义是“把 404 当空白初始化”。
- 销售项页错误只有 `<Alert>`，无重试按钮；`sales-settings.tsx` 同样只有 Alert。
- 无全局错误边界；`main.tsx:7` 只有 `StrictMode` + antd `App`。

**可访问性**
- 大量 `<a onClick={...}>` 不带 `href`（`notices.tsx:34,81,82`；`projects.tsx:33,35,108,109,118,119`）：不可 Tab 聚焦、无链接语义、右键/新标签不可用。`style.css:316` 写了 `a:focus-visible`，但这些元素根本拿不到焦点。
- 表格行内按钮无 `aria-label`；仅少数控件有（`notices.tsx:32` 的 Select、`tasks.tsx:26` 的 InputNumber、`lexicon.tsx:33` 的版本输入）。
- 做得好的：`lang="zh-CN"`、移动端菜单按钮有 `aria-label="展开导航"`（`workbench.tsx:36`）、日趋势图 `role="img" aria-label`（`dashboard.tsx:23`）、`prefers-reduced-motion` 降级（`style.css:326`）、`inert` + `aria-busy`（`lexicon.tsx:31`）。注意 `lexicon.tsx:18` 的 beforeunload 只调了 `event.preventDefault()`，未设 `returnValue`，Chrome 下离开确认可能不弹。

**样式组织**
- 单个 18.7KB `style.css`，手写工具类（`.muted/.small/.mono/.section-row`）+ 页面级类名混排；无 CSS Modules、无 scoped、无设计 token 文件。
- 品牌色有两处来源：`main.tsx:8` 的 antd token（`colorPrimary '#087d7c'`、`borderRadius 5`、字体 `"Microsoft YaHei"`）与 `style.css:2` 的 `--navy/--teal/--amber/--muted/--border`，且 CSS 里又大量硬编码 `#087d7c/#d59645/#142c40`，改主题需两处同步。
- 重复/失效规则：`.filter-fields` 定义了两次 grid 且后者覆盖（`style.css:159` 与 `style.css:524`）；媒体查询 1200px/680px 出现多轮（`style.css:338/526/567`），后写的覆盖先写的，读起来要按层叠顺序心算；`.tag-card` 与 `.tags-card`、`.tag-top` 与 `.tag-name` 命名相近易混。
- 无打印样式；`max-width:1920px` 居中，超宽屏两侧留白固定。

**其它可维护性问题**
- 5 个页面各自 5s 轮询，同一标签页稳定 8–12 req/5s；详情页每次都传正文/附件全文，代价与信息量不成比例。
- 路由用 `if/else` 手写，新增页面要同时改 3 处（route 默认值、`selected` 映射、分发链），且 `selected` 的三元链可读性差（`workbench.tsx:23`）。
- 无 loading 防重复提交的统一封装；`busy` 布尔量页面级共享，导致一个行内操作让整表按钮转圈。

## 6. 值得注意的具体事实（文件:行号）

**路由/导航**
- `frontend/src/workbench.tsx:13`：默认路由 `/sales-projects`；`/` 也被视为销售项项目页。
- `frontend/src/workbench.tsx:22`：菜单文案「**销售项项目**」疑为「销售项目」笔误；图标复用严重——`ApartmentOutlined` 同时给 `/sales-projects` 与 `/projects`，`DatabaseOutlined` 给 `/sales` 与 `/lexicon`，`ThunderboltOutlined` 给 `/sales-settings` 与 `/tasks`。
- `frontend/src/workbench.tsx:24`：`let content = <Dashboard/>`，任何未匹配 path（含 `/history-overview`）都落到总览，没有 404。
- `frontend/src/workbench.tsx:15,35`：`connected` 初值 false、`checking` 初值 `!!savedKey()`，但渲染判据只看 `connected` → 已有 key 的用户刷新必先看到登录页闪屏。
- `frontend/src/workbench.tsx:19`：`saveKey(value.trim())` 在校验**之前**执行；校验失败也会把错误 key 写入 sessionStorage，下次刷新自动重试一次。
- `frontend/src/workbench.tsx:21`：载入演示样例成功后 `window.location.reload()` 整页刷新（同文件 `:36` 侧边栏按钮在 `/sales*` 下隐藏）。
- `frontend/src/workbench.tsx:25,29,31`：`path.slice(16)`、`slice(9)`、`slice(10)` 硬编码前缀长度，与路由字符串强耦合。

**数据层**
- `frontend/src/common.tsx:22`：`if (inFlight.current) return;`（吞刷新，见 §3）。
- `frontend/src/common.tsx:6`：`statusText` 一张表混装标签态、任务态、项目态、同步态（`confirmed/candidate/excluded/queued/running/retry/ready/completed/needs_review/failed/pending/rule_done/model_pending/model_done/context_only/unselected/not_selected/missing_primary/missing_material/stale/skipped`），同义项 `unselected` 与 `not_selected` 并存。
- `frontend/src/api.ts:27`：CSV 公式注入防护 `if (/^[=+\-@\t\r]/.test(text)) text = \`'${text}\``；`api.ts:31` 加 BOM；`api.ts:32` 点击后**立即** `URL.revokeObjectURL(url)`（Safari/部分环境有下载竞态风险）。
- `frontend/src/api.ts:5`：`direct` 为真时 URL 无任何前缀，因此 `/config`、`/knowledge/sync` 这种非 `/api` 路由靠“裸路径”实现，无集中常量表。

**总览**
- `frontend/src/dashboard.tsx:12-14`：用 `project_total !== undefined` 做新旧契约分支，`coverage` 分子分母随之切换。
- `frontend/src/dashboard.tsx:23`：每个 day 列都重算一次 `Math.max(1, ...data.daily.map(...))`，O(n²)；同日 `day.date.slice(5)` 假设字符串是 ISO，若后端给 `20250101` 则切错。
- `frontend/src/dashboard.tsx:25`：「页面更新时间 {dateTime(Date.now()/1000)}」显示的是**渲染时刻**而非数据时间，每 5s 轮询后会变，误导。
- `frontend/src/dashboard.tsx:24`：`data.metrics.llm_calls` 等字段直接取，`metrics` 若缺失即 TypeError（无兜底 `?.`）。

**公告**
- `frontend/src/notices.tsx:8`：`filters` 里有 `category`，但界面从无控件设置它 —— 死字段（后端 `v2_api.py:112` 反而支持）。
- `frontend/src/notices.tsx:19-26`：批量复核按 `project_key` 去重后**串行** `await`，逐条请求，无并发/进度；提示固定为「已按所选公告归属的项目提交复核，同项目自动合并」。
- `frontend/src/notices.tsx:33`：`<LoadState loading={false}>`（首屏无骨架）。
- `frontend/src/notices.tsx:53`：详情页每 5s 重拉含正文与全部附件文本的 `NoticeDetail`。
- `frontend/src/notices.tsx:68`：`staleReview` 比较 `editingAnalysis` 与 `activeAnalysis`；在**非主公告**上做单公告人工标注时两者都是 `undefined`，比较结果为相等 → 版本冲突校验形同虚设（只有编辑主公告标签时才真正生效）。`notices.tsx:70` 同时把 `activeAnalysis` 存进 `editingAnalysis`，导致 `editingAnalysis` 有值时走 `/project-analysis/review`，否则走 `/notices/{id}/review`。
- `frontend/src/notices.tsx:84`：附件 Tab key 为 `attachment:${att.att_id}`，高亮 terms 按 `e.source === \`attachment:${att.att_id}\`` 过滤——与 `types.ts:3` 的 `source` 约定强耦合，改格式会静默失去高亮。
- `frontend/src/notices.tsx:88`：人工标签 `category/label` 只能从词库下拉选，不可自由输入，与后端 `v2_api.py:148` 的 422「新标签不在当前词库中，请先维护词库」一致。
- `frontend/src/notices.tsx:31`：导出仅当前页（最多 20 行），列名为中文硬编码，`公告当前页.csv`。

**项目**
- `frontend/src/projects.tsx:26`：主公告优先级文案硬编码；`projects.tsx:48-53` 的 `noticeRole()` 用 `analyses` 反推「主公告/需求更正/更正未纳入本次分析/进展或未选材料」。
- `frontend/src/projects.tsx:40`：行内「分析项目」也绑定页面级 `busy` → 点任意一行全部行按钮转圈。
- `frontend/src/projects.tsx:110-111`：`model_route_reason || '等待规则分析或旧结果尚未分流'`；`analysis.revision?.slice(0,16)` 截断展示需求版本，复制不到完整 revision。
- `frontend/src/projects.tsx:18`：批量分析把 `selected.map(String)` 逐个串行 POST，失败即中断后续。

**任务**
- `frontend/src/tasks.tsx:7`：`phaseNames` 硬编码 12 个阶段中文；未知 phase 直接显示英文原值。
- `frontend/src/tasks.tsx:26`：`InputNumber` 的 `onChange` 每次键入即 `control({llm_rpm: value})` → 无 debounce，可能连发多个 PUT 等效请求；控件值来自 5s 轮询的 `scheduler.llm_rpm`，服务端未及时更新时输入会“回跳”。
- `frontend/src/tasks.tsx:31`：kind 只有 `project_model/project_rule/model/rule` 四类映射，未知 kind 会被显示成「规则分析」；状态列把 rule 类 `completed` 显示为 `ready`（`statusText` 里两者中文都是“规则完成”，属冗余兼容）。

**词库**
- `frontend/src/lexicon.tsx:26`：`PUT /lexicon` 提交整份 `Knowledge`，无 `expected_version`/乐观锁；两人同时编辑后写覆盖。
- `frontend/src/lexicon.tsx:30`：搜索用 `JSON.stringify(item).toLowerCase().includes(...)` 逐行序列化，且每次渲染重算。
- `frontend/src/lexicon.tsx:31`：`inert={busy}`（全站唯一）把整页变不可交互，而刷新按钮仍在忙时被禁用，用户无反馈。
- `frontend/src/lexicon.tsx:9`：`blankKnowledge` 兜底 `version:'v1'`，与真实快照版本可能同名 → 误发布覆盖风险。

**销售项 / 商机**
- `frontend/src/sales.tsx:18`：手写 CSV parser（支持 `""` 转义，`if (quoted) throw Error('CSV引号未闭合')`），表头别名 `{'销售项ID':'id','销售项':'name','销售项名称':'name','能力描述':'description','分类':'categories','产品':'products'}`。
- `frontend/src/sales.tsx:23`：`beforeUpload` 始终 `return false`（只本地解析），预览写入 `imports`；第 1 条缺名称/描述时抛「第N条缺少销售项名称或能力描述」。
- `frontend/src/sales.tsx:24`：未选中时向量化发送 `{ids: []}`，按钮文案却是「向量化所有待处理项」——是否表示“全部”完全依赖后端对空数组的解释（`sales_api.py:134-137` 原样透传 payload）。
- `frontend/src/sales.tsx:26`：「反查公告」与「语义检索」共用同一个 `results` state 与同一张结果表，表头/标题固定为语义检索语义（名称/相似度/能力），复用后列义不符。
- `frontend/src/sales.tsx:29`：新增走 `/sales-items/import` 单条导入、编辑走 `PUT`，同一 Modal 两套路径；`categories/products` 用 `Select mode="tags"` 自由输入，与后端向量化只用 name+description 的说明（页面描述 `sales.tsx:24`）一致。
- `frontend/src/sales-projects.tsx:10`：单行 3792 字符承载整页；三路 5s 轮询；「二级销售项」多选 `ids.join(',')` 塞进 `sales_item_id`（与 `sales_api.py:201` 的 `split(',')` 对得上）；`match_mode` 取 `any|all`。
- `frontend/src/sales-projects.tsx:11`：`stale = !!review && review.revision !== data?.revision`，而详情每 5s 轮询会更新 `data.revision` → 弹窗开着时一旦项目被后台重算就立刻变“已过期”，只能关闭重来；「重新匹配」固定发 `{mode:'history', project_keys:[key], force:true}`，忽略用户当前所处模式。
- `frontend/src/sales-settings.tsx:8`：`probe()` 先 `await save()` 再调测试接口——**点“测试”等于隐式保存**，会先弹「配置已保存」toast；`save()` 内部先 `setBusy(true)` 再在 finally 置 false，`probe` 紧接着又置 true，状态灯闪烁。
- `frontend/src/sales-settings.tsx:12`：配置页直接提供 `toggle('paused', '全局暂停同步与分析')`，即可在“系统配置”里改全局调度；`candidate_threshold/confirm_threshold` 允许 `-1..1`，与页面提示“相似度不等于匹配概率”并置。
- `frontend/src/sales-settings.tsx:11`：密钥字段渲染 `${name}_clear` 复选框；`sales-settings.tsx:6` 加载时把三个 `_clear` 重置为 false。后端 `sales_service.py:52-56` 对空字符串密钥不视为变更，并有 `_clear` 分支，前端语义与之一致。

**构建/发布**
- `frontend/dist/assets/index-BzKx2OJW.js` 1,239,187 B，单文件无分割；`frontend/vite.config.ts:6` 的 `chunkSizeWarningLimit: 1200` 正好抹掉警告。
- 根 `dist/bid-analysis-service-0.2.0~0.2.2.zip` 是旧版本产物，与当前 0.5.0 不一致（陈旧文件，易误发）。
- `frontend/package.json:8` `build` 会先跑 `tsc --noEmit`，因此 `noUnusedLocals` 让任何未用 import 都能卡住发布（正面约束，但无 lint 兜底格式）。

## 结论（优先级建议）

1. **修 `useResource` 的 in-flight 吞刷新**（`common.tsx:22`）：改为“请求中则标记 pending、结束后补一次”，否则“操作已提交”提示与页面数据长期不一致。
2. **拆 `sales*.tsx` 的压缩代码并统一格式化**，把 v3 类型收进 `types.ts`，消除同名 `Evidence` 两套定义。
3. **把轮询改成按需 + 可见性感知**（`document.hidden` 时停），详情页去掉 5s 全量重拉。
4. **补错误边界与统一 `useAction`**（loading/错误/成功提示），消除页面级 `busy` 共享导致的整表转圈。
5. **a11y 最小修复**：`<a onClick>` 换 `Button type="link"` 或补 `href`，行内操作补 `aria-label`。
6. **构建产物治理**：`manualChunks` 拆 antd/react，删掉根 `dist/` 旧 zip，修 `build_release.py` 的 `npm ci` 为 pnpm（或补 `package-lock.json`）。
