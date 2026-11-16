import SalesCapability from './sales-capability';
import {useState} from 'react';
import {App, Alert, Button, Descriptions, Input, Modal, Space, Table, Tag} from './ui';
import {go} from './common';
import SalesExtraction, {type ProcurementExtraction} from './sales-extraction';
import {salesStatus, send, useSalesResource} from './sales';

interface Job {id:string; status:string; error?:string; progress:Record<string,unknown>}
interface Result {procurement_extraction?:ProcurementExtraction;status:string;main_notice_id?:number;main_notice_ids?:number[];correction_notice_ids?:number[];warnings?:string[];vectorization?:Record<string,number>;collect_attachments?:boolean;matches:{sales_item_id:string;name:string;description?:string;categories?:string[];products?:string[];status:string;reason:string;evidence:{notice_id:number;attachment_id?:number;content:string}[]}[]}
function TestProgress({id,projectKey}:{id:string;projectKey:string}) {
 const job = useSalesResource<Job>(`/jobs/${id}`,2000);
 const phases:Record<string,string> = {extracting_requirements:'提取采购需求',reading_project:'读取公告正文',vectorizing_material:'生成或复用本地向量',saving_local_vectors:'写入本地分析库',matching:'召回销售项',model_review:'模型复核'};
 return <>
  <p>测试项目：{projectKey}</p>
  {job.error && <Alert type="error" message={job.error}/>}
  <p>任务状态：<Tag>{salesStatus[job.data?.status||'']||'读取中'}</Tag> {phases[String(job.data?.progress?.phase)]||''}</p>
  {job.data?.error && <Alert type="error" message={job.data.error}/>}
  {job.data?.progress && <p className="muted">{typeof job.data.progress.total==='number'?`分片进度：${job.data.progress.processed||0} / ${job.data.progress.total}`:''} {typeof job.data.progress.reused==='number'?`复用：${job.data.progress.reused}`:''}</p>}
  {job.data?.status==='completed' && <TestResult projectKey={projectKey}/>}
 </>;
}

function TestResult({projectKey}:{projectKey:string}) {
 const result = useSalesResource<Result>(`/projects/${encodeURIComponent(projectKey)}`,0);
 return <>
   {result.error && <Alert type="warning" message={result.error}/>}
   <Descriptions column={1} items={[
    {key:'status',label:'分析状态',children:salesStatus[result.data?.status||'']||result.data?.status||'加载中'},
    {key:'main',label:'采购主公告',children:result.data?.main_notice_ids?.join('、')||result.data?.main_notice_id||'—'},
    {key:'correction',label:'相关更正',children:result.data?.correction_notice_ids?.join('、')||'无'},
    {key:'attachments',label:'附件处理',children:result.data?.collect_attachments?'已开启':'关闭，仅正文'},
    {key:'vectors',label:'本地分片',children:result.data?.vectorization?`共 ${result.data.vectorization.chunks} 片，新生成 ${result.data.vectorization.generated} 片，复用 ${result.data.vectorization.reused} 片`:'无可用向量材料'},
   ]}/>
   {!!result.data?.warnings?.length && <Alert type="warning" message={result.data.warnings.join('；')}/>}
   <SalesExtraction data={result.data?.procurement_extraction}/>
   <Table rowKey="sales_item_id" dataSource={result.data?.matches||[]} pagination={false} columns={[
    {title:'销售项与能力',width:340,render:(_,r)=><SalesCapability item={r} compact/>}, {title:'判断',render:(_,r)=>salesStatus[r.status]||r.status},
    {title:'理由',dataIndex:'reason'}, {title:'原文证据',render:(_,r)=><>{r.evidence?.map((e,i)=><p key={i}>公告 #{e.notice_id}{e.attachment_id?` / 附件 #${e.attachment_id}`:''}：{e.content.slice(0,500)}</p>)}</>},
   ]}/>
   <Button onClick={()=>go('/sales-projects/'+encodeURIComponent(projectKey))}>打开完整项目详情</Button>
 </>;
}

export default function SalesProjectTest() {
 const [open,setOpen] = useState(false), [key,setKey] = useState(''), [busy,setBusy] = useState(false);
 const [submitted,setSubmitted] = useState<{id:string;key:string}>();
 const {message} = App.useApp();
 const start = async()=>{
  setBusy(true);
  try {const job=await send<Job>('/analysis/test',{project_key:key.trim()});setSubmitted({id:job.id,key:key.trim()});}
  catch(e){message.error((e as Error).message);}
  finally{setBusy(false);}
 };
 return <><Button onClick={()=>setOpen(true)}>单个项目测试</Button><Modal title="单个项目测试" width={1100} open={open} onCancel={()=>setOpen(false)} footer={null} destroyOnHidden>
  <Alert type="info" showIcon message="仅分析指定项目，依次读取正文、提取采购需求、向量化并匹配；不开启存量或增量扫描。使用当前阶段范围、附件开关及模型配置；结果会保存为该项目最新分析结果。请先同步项目与公告、完成销售项向量化并取消调度暂停。"/>
  <Space.Compact style={{width:'100%',margin:'20px 0'}}><Input aria-label="测试项目标识" placeholder="输入主库 mv_project.project_key（精确项目标识）" value={key} onChange={e=>setKey(e.target.value)} onPressEnter={()=>{if(key.trim()&&!busy)void start();}}/><Button type="primary" loading={busy} disabled={!key.trim()} onClick={start}>开始测试</Button></Space.Compact>
  {submitted && <TestProgress key={submitted.id} id={submitted.id} projectKey={submitted.key}/>}
 </Modal></>;
}
