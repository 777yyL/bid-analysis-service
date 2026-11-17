import {useEffect, useState} from 'react';
import {App, Alert, Button, Card, Checkbox, Collapse, Form, Input, InputNumber, Select, Switch, Table} from './ui';
import {api} from './api';
import {PageTitle} from './common';
import {send, useSalesResource, v3} from './sales';
import SalesSourceSync from './sales-source-sync';
import SalesFileDownload from './sales-file-download';

export default function SalesSettings({shared=false}:{shared?:boolean}) {
 const state = useSalesResource<Record<string, unknown>>('/settings', 0);
 const [form] = Form.useForm();
 const [busy, setBusy] = useState(false);
 const [tab, setTab] = useState('source');
 const [knowledgeQuery, setKnowledgeQuery] = useState('采购需求：建设访客管理系统，支持访客预约、登记、审批和通行授权。');
 const [knowledgeResult,setKnowledgeResult] = useState<{total:number;unresolved:number;items:Record<string,unknown>[]} >();
 const [tests, setTests] = useState<Record<string, {ok:boolean; message:string; data?:unknown}>>({});
 const {message} = App.useApp();
 useEffect(() => {if (state.data) form.setFieldsValue({...state.data, source_dsn_clear:false, embedding_api_key_clear:false, review_api_key_clear:false,knowledge_api_key_clear:false,source_download_key_clear:false});}, [state.data, form]);
 const save = async () => {
  setBusy(true);
  try {const values = await form.validateFields(); await api(v3+'/settings', {method:'PUT', body:JSON.stringify(values)}, true); await state.refresh(); message.success('全部配置已保存'); return true;}
  catch (e) {message.error((e as Error).message); return false;}
  finally {setBusy(false);}
 };
 const probe = async (path:string) => {
  if (!await save()) return;
  setBusy(true);
  try {
   const data = await send<Record<string, unknown>>(path);
   const compatible = path === '/embedding/test' ? data.ok === true : true;
   const summary = path === '/source/test' ? (shared?'同库只读连接成功，直接读取主库项目和公告。':'主库连接成功，当前部署使用来源同步模式。')
    : path === '/embedding/test' ? `向量接口测试通过，维度 ${data.dimensions}。销售项、公告正文和可选附件均使用此接口，在本地分析库保存向量。`
    : '复核接口测试通过，已返回有效的销售项判断及证据。';
   setTests(old => ({...old, [tab]:{ok:compatible, message:summary, data}}));
  } catch (e) {setTests(old => ({...old, [tab]:{ok:false, message:(e as Error).message}}));}
  finally {setBusy(false);}
 };
 const field = (name:string, label:string, help:string, min?:number, max?:number) => <Form.Item key={name} name={name} label={label} extra={help}>{min !== undefined ? <InputNumber min={min} max={max} step={max === 1 ? 0.01 : 1} style={{width:'100%'}}/> : <Input/>}</Form.Item>;
 const toggle = (name:string, label:string, help:string) => <Form.Item key={name} name={name} label={label} extra={help} valuePropName="checked"><Switch/></Form.Item>;
 const secret = (name:string, label:string, help:string) => <div key={name}><Form.Item name={name} label={label} extra={`${help}${state.data?.[name+'_configured'] ? ' 已配置，留空会保留原值。' : ''}`}><Input.Password autoComplete="new-password"/></Form.Item><Form.Item name={name+'_clear'} valuePropName="checked"><Checkbox>清除已保存的{label}</Checkbox></Form.Item></div>;
 const sections = [
  {key:'knowledge',label:'MaaS 知识库',children:<>
   <p className="muted">{shared?'使用主库已提取的采购需求检索 MaaS 知识库，并按销售项 ID 关联本地目录。':'每个销售项作为一个完整切片上传到 MaaS，并按销售项 ID 关联本地目录。'}</p>
   {!shared&&<Form.Item name="retrieval_backend" label="销售项召回方式"><Select options={[{value:'local',label:'本地向量检索（原方式）'},{value:'maas',label:'MaaS 知识库召回'}]}/></Form.Item>}
   {field('knowledge_url','知识库检索完整地址','填写文档 curl 中的完整 POST 地址，包含接口路径。本服务不会自动拼接 /v1 或 /embeddings。')}
   {field('knowledge_id','知识库 ID','填写 knowledgeId；多个知识库按平台约定用英文逗号分隔。')}
   {field('knowledge_workspace_id','工作空间 ID（可选）','对应 workspaceId；平台不需要时保持为空。')}
   {field('knowledge_result_max_length','resultMaxLength（可选）','填 0 不发送此参数；需复现平台请求时可填 2。该字段未在原参数文档中解释，不能将其视为 Top K；请避免平台截断销售项 ID。',0)}
   {secret('knowledge_api_key','知识库 API Key','使用 api-key 请求头认证，与向量接口密钥分别保存。')}
   {field('knowledge_revision','知识库内容版本','默认 1。更新知识库切片后修改此值，或对项目强制重跑，避免复用先前分析结果。')}
   <Form.Item name="knowledge_strategy" label="检索策略"><Select options={[{value:1,label:'语义检索'},{value:2,label:'全文检索'},{value:3,label:'混合检索'}]}/></Form.Item>
   {field('knowledge_top_k','每段采购需求返回切片数','默认 10，控制发送给 MaaS 的 topK。',1,100)}
   {field('knowledge_threshold','MaaS 相似度阈值','新配置默认 0.20，与提供的真实请求一致；独立于本地向量阈值 0.80，需要按知识库与重排分数分布验证。',0,1)}
   {toggle('knowledge_rerank','启用重排','混合检索按平台文档强制启用重排。')}
   {field('knowledge_rerank_model','重排模型标识','文档列出 bge-reranker-base、bge-reranker-v2-m3、bge-reranker-large、Qwen3-Reranker-0.6B；填写平台实际支持的标识。')}
   {field('knowledge_timeout','检索超时（秒）','默认 60，连接失败与限流按任务重试策略处理。',1,600)}
   {field('knowledge_concurrency','知识库请求并发数','默认 4，同一服务进程同时调用 MaaS 的请求上限。建议按 2→4→6 逐档验证吞吐、429、超时和重试。',1,32)}
   <Alert showIcon type="info" message="支持 Excel 解析后的 Markdown 表格（销售项ID列），也支持独立一行“销售项ID：你的销售项ID”。ID 可以包含空格，必须对应本地已启用项。一个切片中若包含多个不同 ID，会提示无法关联。"/>
   <Form.Item label="检索测试文本" extra="直接测试知识库接口，不做需求提取，不修改已分析的项目。正式项目分析与销售项页面检索会先提取采购需求。"><Input.TextArea rows={4} value={knowledgeQuery} onChange={e=>setKnowledgeQuery(e.target.value)}/></Form.Item>
   <Button loading={busy} disabled={!knowledgeQuery.trim()} onClick={async()=>{setKnowledgeResult(undefined);if(!await save())return;setBusy(true);try{setKnowledgeResult(await send('/knowledge/test',{text:knowledgeQuery,limit:10}));}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}}>保存并测试知识库召回</Button>
   {knowledgeResult&&<><Alert className="section-row" type={knowledgeResult.unresolved?'warning':'success'} message={`接口调用成功，返回 ${knowledgeResult.total} 个切片，其中 ${knowledgeResult.unresolved} 个无法关联本地销售项。返回为空表示本次查询未召回。`}/><Table rowKey={(_,i)=>String(i)} dataSource={knowledgeResult.items} pagination={false} columns={[{title:'销售项 ID',render:(_,r)=>String(r.sales_item_id||'未关联')},{title:'平台分数',dataIndex:'similarity'},{title:'文档',dataIndex:'docName'},{title:'切片 ID',dataIndex:'chunkId'},{title:'切片原文',dataIndex:'chunkContent'}]}/></>}
  </>},
  {key:'source', label:'数据与文件', children:<>
   <p className="muted">配置主库连接并同步关注的项目与公告。本地普通表是后续分析的数据源，主库账号只需 SELECT 权限，不需要 dblink 扩展。已完成项目的结果与标签不自动重跑。</p>
   {toggle('engine_enabled','启用销售项分析','开启后消费销售项分析任务；正文、附件及标签结果均在本服务中使用。')}
   {secret('source_dsn','主服务数据库连接','填写 PostgreSQL 连接串，例如 postgresql://用户名:密码@主机:端口/bidinfo。这里是公告主库地址；由分析服务直接发起只读连接，须确保分析服务可以访问主库。')}
   {toggle('collect_attachments','采集并分析附件','默认开启。同步选定采购公告的附件路径，下载原件及已有解析文本；匹配使用解析文本，不执行 OCR。改变开关后需要重新同步。')}
   {field('source_download_url','文件分享服务地址','填写主服务器独立文件服务的地址，例如 http://主服务器:8101。无需填写 /files；配置后从本库项目路径主动下载，不扫描远端目录。')}
   {secret('source_download_key','文件分享访问密钥','与主服务器 FILE_SHARE_API_KEY 一致；密钥加密保存在分析库。')}
   {field('source_cache_root','本地材料缓存目录','Docker 建议 /app/runtime/materials，位于已有持久化卷中。无需在分析服务器挂载主服务目录。')}
   <Collapse items={[{key:'download',label:'高级：下载并发与文件限制',children:<>
    {field('source_download_concurrency','批量下载项目并发','默认 4，每个项目内部按需下载；出现带宽或磁盘压力时调低。',1,16)}
    {field('source_download_timeout','下载请求超时（秒）','默认 120，仅影响文件传输，不影响项目列表查询。',1,600)}
    {field('source_download_max_mb','附件原件大小上限（MB）','默认 200。正文及解析文本另设 20 MB 上限；超限记录提示，不截断分析。',1,2048)}
    {field('source_file_root','兼容：本地共享目录','仅未配置分享地址时使用，例如 /source-data。已部署文件分享服务时可留空。')}
   </>}]}/>
   <Collapse items={[{key:'batches',label:'时间批次与自动增量同步',children:<>
    {field('source_sync_batch_hours','每个时间批次跨度（小时）','默认 24 小时。成功后下一批从本批截止时间继续；空批次也会记录并推进。失败不会推进截止点。',1,720)}
    {field('source_sync_interval_seconds','追平后的检查间隔（秒）','默认 1800，即 30 分钟；尚有历史时间区间待追平时连续处理下一批。需在运行调度开启增量同步。',10,86400)}
    {field('source_sync_start_at','首次同步起点（可选）','例如 2026-11-01T00:00:00+08:00。仅没有已保存截止点时生效；留空从最早采集时间开始。升级旧快照会继承旧同步时间，不清空已有结果。')}
    {field('source_timezone','源库无时区时间的时区','默认 Asia/Shanghai，用于解释 timestamp without time zone；批次截止时间统一保存为 UTC。')}
    {field('source_notice_time_column','公告增量时间列（可选）','默认自动组合 updated_at、first_seen_at 等采集时间字段；不使用公告发布日期。填写时仅使用指定的 timestamp 列。')}
    {field('source_attachment_time_column','附件增量时间列（可选）','默认组合更新、解析、下载时间列；只有 fetched_at 的主库还会轮流补查缺失解析文本的附件。')}
    {field('source_sync_overlap_seconds','时间重叠读取范围（秒）','默认 300，重复读取边界前 5 分钟并按唯一键去重，降低延迟提交造成漏数的风险。',0,86400)}
    {field('source_sync_lag_seconds','同步安全延迟（秒）','默认 120，同步截止点最多到主库当前时间前 2 分钟，留出采集事务提交时间。',0,86400)}
    {field('source_sync_page_size','每次读取行数','默认 500，服务端游标分批读取；同秒产生的大量记录不会因分页被截断。',10,2000)}
   </>}]}/>
   <SalesFileDownload beforeStart={save}/>
   <Collapse items={[{key:'chunks',label:'高级：新生成分片的大小',children:field('source_chunk_chars','每片字符数','默认 1600，仅影响本服务新生成的分片。过大可能超过模型输入限制；通常保持默认即可。',200,8000)}]}/>
   <Button className="section-row" loading={busy} onClick={()=>probe('/source/test')}>保存并测试主库直连</Button><SalesSourceSync beforeSync={save}/>
  </>},
  {key:'embedding', label:'向量模型', children:<>
   <p className="muted">销售项名称与能力描述，以及从正文和可选附件中前置提取的采购需求，由本服务调用此接口向量化并存入分析库。招标流程等无关内容先过滤。相同材料与配置会复用缓存，不依赖主服务的向量表。</p>
   {field('embedding_base_url','向量接口地址','填写 OpenAI 兼容服务地址，例如 http://模型主机:端口/v1；服务会调用 /embeddings。')}
   {field('embedding_model','向量模型名称','填写接口实际提供的模型标识；销售项和公告正文使用同一模型，不要求与主服务一致。')}
   {secret('embedding_api_key','向量接口密钥','模型服务需要认证时填写；无需认证可留空。')}
   {field('embedding_dimensions','向量维度','填 0 自动检测；指定维度时严格校验。本地公告向量会与销售项向量校验维度一致。',0,65536)}
   <Collapse items={[{key:'encoding',label:'高级：编码前缀、批次与并发',children:<>
    {field('embedding_query_prefix','销售项查询侧前缀','默认留空。增加检索指令前，应对比真实正负样本；指令不保证提高区分度。')}
    {field('embedding_document_prefix','公告文档侧前缀','用于本服务公告和附件的文档侧编码；按当前向量模型要求填写，没有前缀则留空。')}
    {field('embedding_batch_size','每次请求的文本数量','默认 16。模型单次输入有限制时调小。',1,256)}
    {field('embedding_concurrency','向量请求并发数','默认 2。接口出现限流或超时时调小；资源充足时逐步增加。',1,32)}
    {field('embedding_timeout','向量请求超时（秒）','默认 60。单次请求超过此时间会按任务重试策略处理。',1,600)}
   </>}]}/>
   <Alert className="section-row" showIcon type="info" message="修改模型、维度或编码前缀后，请到销售项能力页面重新向量化，再启动分析。"/>
   <Button className="section-row" loading={busy} onClick={()=>probe('/embedding/test')}>保存并测试向量模型</Button>
  </>},
  {key:'review', label:'模型复核', children:<>
   {shared?<Alert type="info" message="召回候选逐项复核。确认才打标签，排除后不展示为命中。"/>:<>
   {toggle('low_score_review_enabled','仅对低相似度候选进行模型复核','低于下方阈值的候选经过模型复核。')}
   {field('review_score_threshold','自动采纳相似度阈值（低于此值需复核）','默认 0.80，为初始验证值。召回阈值应低于此值（知识库默认 0.20）；低于召回阈值的切片不会返回，也无法复核。本地模式也需相应调低候选阈值。不同模型须分别验证。',0,1)}
   <p className="muted">向量检索先筛出候选，再让语言模型结合采购原文判断不确定项。这里配置的是复核语言模型，与向量模型是两种用途。</p>
   {toggle('review_enabled','启用模型复核','默认开启。填写接口和模型后复核低分候选；关闭后，仍要求低分复核的候选会保留待确认。')}
   </>}
   {field('review_base_url','复核接口地址','填写 OpenAI 兼容服务地址，例如 http://模型主机:端口/v1；服务会调用 /chat/completions。')}
   {field('review_model','复核模型名称','填写语言模型服务实际提供的模型标识。')}
   <Form.Item name="review_system_prompt" label="复核系统提示词" extra="可补充公司软件业务范围和典型误匹配案例。留空使用内置规则：明确采购对应的软件能力才打标，证据不足则排除。">
    <Input.TextArea rows={5} maxLength={4000}/>
   </Form.Item>
   {secret('review_api_key','复核接口密钥','使用复核模型服务的认证密钥。')}
   {field('review_concurrency','同时复核的请求数','默认 3。并发有助于利用等待时间，但仍受下面 RPM、TPM 预算约束。',1,32)}
   {field('review_rpm','每分钟请求上限（RPM）','默认 5，按公司分配的可用额度填写。出现 HTTP 429 时应调低。',1,1000000)}
   {field('review_tpm','每分钟 Token 预算（TPM）','填 0 不额外限制 Token；有明确额度时填写。RPM 限制仍然有效。',0)}
   <Collapse items={[{key:'budget',label:'高级：超时与单次输入输出预算',children:<>
    {field('review_timeout','复核超时（秒）','默认 120。较慢模型可以适当提高。',1,600)}
    {field('review_max_input_chars','单次输入字符预算','默认 28000。过大可能超出模型上下文或 TPM；服务会按预算组织证据。',1000,1000000)}
    {field('review_max_output_tokens','单次输出 Token 上限','默认 3000。输出需要容纳判断、理由和证据编号。',256,32000)}
   </>}]}/>
   <Button className="section-row" loading={busy} onClick={()=>probe('/review/test')}>保存并测试复核模型</Button>

  </>},
  {key:'matching', label:'匹配规则', children:<>
   {toggle('manual_confirmation_enabled','未经过模型复核的候选需人工确认','仅影响未经过模型复核的候选。模型确认后直接打标，排除后不打标。')}
   <p className="muted">控制哪些项目参与分析，以及候选销售项的筛选范围。先用真实正负样本验证召回与误判，再确定用于批量分析的阈值。</p>
   <Alert className="section-row" showIcon type="info" message="新配置的候选阈值为 0.80，仅作为初始验证值。不同模型接口的分数分布不同，不能直接套用同一个阈值；升级不会覆盖已保存的配置。"/>
   {toggle('allow_later_stages','纳入后期阶段项目','默认关闭，优先关注采购意向、采购公告、更正公告。开启后也处理结果、合同、验收、废标终止、其他阶段项目，仍回溯其采购主公告及更正材料。')}
   {field('candidate_threshold','候选相似度阈值','新配置默认 0.80，用于初始验证，并非生产精度保证。调低会增加召回与复核量，调高可能漏掉相关销售项。相似度不是匹配概率；既有配置请手动调整并保存。',-1,1)}
   {field('confirm_threshold','直接确认相似度阈值','默认 0.85，不能低于候选阈值。达到阈值仍需满足明确采购上下文等规则，并非只看分数。',-1,1)}
   {field('candidate_limit','每个分片最多召回的销售项数','默认 10。增加可扩大候选范围，同时增加后续判断工作量。',1,100)}
   {field('no_hit_sample_percent','无命中项目抽检比例（%）','默认 5，仅在启用模型复核时生效。抽取少量无命中项目复核，帮助发现漏召回。',0,100)}
  </>},
  {key:'scheduler', label:'运行调度', children:<>

   {field('analysis_concurrency','分析任务并发数','默认 4，同时处理的项目任务上限。与知识库请求并发分别生效，不受扫描批次大小控制。修改前请暂停并等待运行任务结束，保存后恢复。',1,32)}
   <p className="muted">存量分析仅补齐本地尚未完成的项目；增量同步自动从主库读取下一时间批次并分析新项目。已经完成的项目不会自动重新分析，单个项目测试仍可手动强制执行。</p>
   {toggle('paused','暂停新任务消费与扫描','开启后暂停调度；已运行的请求会继续完成。修改模型或数据源前，应暂停并等待运行任务结束。')}
   {toggle('history_enabled','存量项目补跑','仅为本地尚未分析的项目建立任务，保留已完成结果；登记完成后自动关闭。')}
   {toggle('incremental_enabled','自动时间批次同步与分析','开启后按上次成功截止点读取下一批主库数据，再下载材料并处理尚未分析的项目。记录每批数量与明细；失败保留截止点。已完成结果不自动重跑。')}
   {field('project_batch_size','每轮扫描项目数','默认 20，是扫描批次大小，不是模型请求并发数。',1,500)}

  </>},
  {key:'snapshot', label:'查询快照', children:<>
   <p>为数据库直接查询生成销售项匹配结果的物化视图，包含销售项标签、中文分析状态和关联公告。</p>
   <Alert showIcon type="info" message="物化视图是上次刷新的快照，不会随分析结果自动实时更新。页面查询直接读取分析结果，无需依赖此操作。"/>
   <p className="muted">仅支持 PostgreSQL 分析库。刷新任务可在销售项能力页面的任务列表查看。</p>
   <Button loading={busy} onClick={async()=>{setBusy(true);try{await send('/views/refresh');message.success('物化视图刷新任务已排队');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}}>刷新销售项物化视图</Button>
  </>},
 ];
 const visibleSections=shared?[
  {key:'source',label:'主库与材料',children:<>
   <DescriptionsForSharedSource/>
   {toggle('allow_later_stages','纳入后期阶段项目','默认只关注采购意向、采购公告和更正公告；开启后回溯后期项目的采购材料。')}
   <Button loading={busy} onClick={()=>probe('/source/test')}>测试主库读取</Button>
   {field('source_file_root','原文挂载目录','分析服务容器内的只读路径，用于查看公告正文和解析附件。')}
  </>},
  ...sections.filter(section=>section.key==='knowledge'||section.key==='review'),
  {key:'scheduler',label:'分析调度',children:<>
   <p className="muted">直接发现主库中有采购需求且尚无分析记录的项目，已有结果保留。失败任务在运行管理中重试。</p>
   {toggle('engine_enabled','启用分析引擎','后台 worker 启动后才会处理任务。')}
   {toggle('incremental_enabled','自动发现新增采购需求','直接读取主库，不复制来源数据。已完成项目不自动重跑。')}
   {field('poll_seconds','扫描间隔（秒）','扫描一轮完成后，等待此间隔再次检查未分析项目。',1,86400)}
   {field('analysis_concurrency','分析任务并发数','同时处理的项目数量，模型请求仍受各自限额控制。',1,32)}
   {field('project_batch_size','每轮扫描项目数','每次扫描最多登记的项目数。',1,500)}
  </>},
 ]:[...sections].sort((a,b)=>(a.key==='source'?-1:b.key==='source'?1:0));
 return <><PageTitle eyebrow="RUNTIME SETTINGS" title="系统配置" description={shared?'管理数据来源、销售项目录、知识库、模型和任务调度。':'配置主库连接、时间批次、召回与复核模型。'} actions={<Button type="primary" loading={busy} disabled={state.loading || !!state.error} onClick={save}>保存全部配置</Button>}/>
  {state.error && <Alert type="error" message={state.error}/>}
  <div className="settings-layout"><aside className="settings-nav" aria-label="配置分类"><span className="settings-caption">数据与业务</span><a href="#/sales-settings/catalog">销售项目录</a>{visibleSections.filter(section=>section.key!=='snapshot').map(section=><button key={section.key} className={tab===section.key?'active':''} onClick={()=>setTab(section.key)}>{section.label}</button>)}</aside><div className="settings-content"><Card><Form form={form} layout="vertical" disabled={busy || state.loading || !!state.error}>{visibleSections.map(section=><div key={section.key} hidden={tab!==section.key}><h2>{section.label}</h2>{section.children}</div>)}</Form>
  {tests[tab] && <div className="section-row"><Alert showIcon type={tests[tab].ok?'success':'warning'} message={tests[tab].message}/>{tests[tab].data !== undefined && <Collapse items={[{key:'details',label:'查看接口详细返回',children:<pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(tests[tab].data,null,2)}</pre>}]}/>}</div>}</Card></div></div>
 </>;
}

function DescriptionsForSharedSource(){
 return <dl className="shared-source-details">
  <dt>采购需求</dt><dd><code>public.notices</code> · 读取上游提取的采购需求</dd>
  <dt>项目阶段</dt><dd><code>public.mv_project</code> · 按项目标识读取当前阶段</dd>
  <dt>标签读取</dt><dd><code>bid_analysis.v_project_sales_tags</code> · 主服务按项目标识查询已确认的销售项</dd>
 </dl>;
}
