import {Alert, Button, Descriptions, Drawer, Table} from './ui';
import {salesStatus,useSalesResource} from './sales';

interface Event {at:number;phase:string;attempt:number;elapsed_seconds:number;[key:string]:unknown}
interface Job {status:string;attempts:number;updated_at:number;lease_until?:number;error?:string;payload:{project_key?:string};progress:{phase?:string;phase_started_at?:number;events?:Event[]}}
const phases:Record<string,string>={existing_result_retained:'保留已有分析结果，未重跑',sync_batch_reading:'按时间批次读取主库',sync_batch_committing:'提交批次与截止点',sync_batch_reused:'本批已提交，复用成功记录',downloading_file:'下载材料文件',file_cached:'材料缓存校验通过',file_downloaded:'材料下载完成',download_batch_completed:'下载批次完成',download_project_warning:'项目材料提示',download_completed:'材料下载完成',task_started:'任务开始',historical_model_review:'历史低分模型复核',historical_missing_evidence:'历史原文证据缺失，已跳过',historical_review_completed:'项目历史复核完成',review_scan_completed:'历史复核扫描完成',review_scan_waiting:'等待任务队列或同项目任务',review_batch_queued:'历史复核分批入队',reading_project:'读取项目与正文',extracting_requirements:'提取采购需求',matching:'召回销售项',knowledge_chunk:'准备检索需求片段',knowledge_waiting:'等待知识库并发名额',knowledge_request:'知识库请求中',knowledge_response:'知识库返回',knowledge_error:'知识库请求异常',model_review:'模型复核',completed:'完成',failed:'失败',retry:'等待重试',interrupted:'服务中断',vectorizing_material:'材料向量化',saving_local_vectors:'保存材料向量'};
export default function SalesJobLog({id,onClose}:{id:string;onClose:()=>void}) {
 const state=useSalesResource<Job>(`/jobs/${encodeURIComponent(id)}`,0);
 const job=state.data;const age=job?.progress.phase_started_at?Math.max(0,Math.floor(Date.now()/1000-job.progress.phase_started_at)):0;
 return <Drawer title="分析任务日志" width={900} open onClose={onClose}>
  {state.error&&<Alert type="error" message={state.error}/>}
  {job&&<><Descriptions column={2} items={[
   {key:'id',label:'任务 ID',children:id},{key:'project',label:'项目',children:job.payload.project_key||'—'},
   {key:'status',label:'状态 / 尝试次数',children:`${salesStatus[job.status]||job.status} / ${job.attempts}`},
   {key:'phase',label:'当前阶段',children:phases[job.progress.phase||'']||job.progress.phase||'—'},
   {key:'age',label:'当前阶段持续',children:job.status==='running'?`${age} 秒`:'已结束'},
   {key:'heartbeat',label:'最后更新时间',children:new Date(job.updated_at*1000).toLocaleString()},
  ]}/>
  {job.error&&<Alert type="error" message={job.error}/>}
  {job.status==='running'&&age>=120&&<Alert type="warning" message="当前阶段持续超过 120 秒。请结合心跳、租约、请求超时和数据库等待检查；持续时间长不等于已卡死。"/>}
  {job.status==='running'&&job.lease_until&&job.lease_until<Date.now()/1000&&<Alert type="error" message="任务租约已过期，可能存在进程退出或心跳阻塞。后台将按租约机制尝试恢复。"/>}
  <Button loading={state.loading} onClick={()=>void state.refresh()}>刷新日志</Button><p>手动刷新，保留最近 100 条阶段事件；更早记录可通过容器日志按任务 ID 查询。耗时单位为秒，不记录请求密钥或公告全文。</p>
  <Table<Event> rowKey={(_,i)=>String(i)} pagination={{pageSize:15}} dataSource={job.progress.events||[]} columns={[
   {title:'时间',render:(_,r)=>new Date(r.at*1000).toLocaleTimeString()},
   {title:'阶段',render:(_,r)=>phases[r.phase]||r.phase},
   {title:'尝试',dataIndex:'attempt'},
   {title:'耗时',dataIndex:'elapsed_seconds'},
   {title:'详情',render:(_,r)=><pre style={{whiteSpace:'pre-wrap',maxWidth:440}}>{JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k])=>!['at','phase','attempt','elapsed_seconds'].includes(k))),null,2)}</pre>},
  ]}/></>}
 </Drawer>;
}
