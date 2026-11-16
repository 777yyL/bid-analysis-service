import {useState} from 'react';
import {App,Alert,Button,Card,Descriptions,Drawer,Space,Table,Tag} from './ui';
import {dateTime} from './common';
import {salesStatus,send,useSalesResource} from './sales';
import SalesJobLog from './sales-job-log';
interface SyncStatus{ready:boolean;supported:boolean;checkpoint?:string;next_sync_at?:number;synced_at?:number;last_error?:string;counts?:{projects:number;notices:number;attachments:number};job?:{id:string;status:string;error?:string;progress?:{phase?:string}}}
interface Batch{id:string;status:string;window_start:string;window_end:string;read_start:string;counts:Record<string,number>;error?:string;attempts:number;started_at:number}
function Records({id}:{id:string}){const [page,setPage]=useState(1);const state=useSalesResource<{total:number;items:{kind:string;source_id:string;project_key:string;payload:unknown}[]}>(`/source/sync/batches/${id}/records?page=${page}&page_size=30`,0);return <>{state.error&&<Alert type="error" message={state.error}/>}<p className="muted small">保存本批实际读取的项目、公告和附件元数据及路径；不包含模型密钥或正文全文。</p><Table rowKey={r=>r.kind+':'+r.source_id} dataSource={state.data?.items||[]} loading={state.loading} pagination={{current:page,pageSize:30,total:state.data?.total,onChange:setPage}} columns={[{title:'类型',render:(_,r)=>({project:'项目',notice:'公告',attachment:'附件'}[r.kind]||r.kind)},{title:'来源标识',dataIndex:'source_id'},{title:'项目',dataIndex:'project_key'},{title:'本批数据',render:(_,r)=><details><summary>查看元数据</summary><pre>{JSON.stringify(r.payload,null,2)}</pre></details>}]}/></>;}
export default function SalesSourceSync({beforeSync}:{beforeSync?:()=>Promise<boolean>}){
 const state=useSalesResource<SyncStatus>('/source/sync/status',5000);const [page,setPage]=useState(1);
 const batches=useSalesResource<{total:number;items:Batch[]}>(`/source/sync/batches?page=${page}&page_size=10`,5000);
 const [busy,setBusy]=useState(false),[batch,setBatch]=useState<Batch>(),[log,setLog]=useState<string>();const {message}=App.useApp();
 const running=!!state.data?.job&&['queued','running','retry'].includes(state.data.job.status);
 const synchronize=async()=>{if(beforeSync&&!await beforeSync())return;setBusy(true);try{await send('/source/sync');message.success('已提交下一时间批次；成功后推进截止点，失败原地重试');await state.refresh();await batches.refresh();}catch(e){message.error((e as Error).message);}finally{setBusy(false);}};
 return <><Card className="section-row" title="时间批次同步" extra={<Button type="primary" loading={busy||running} disabled={!state.data?.supported||!!state.error} onClick={synchronize}>{beforeSync?'保存并同步下一批':'同步下一时间批次'}</Button>}>
  {state.error&&<Alert type="error" message={state.error}/>}
  <p className="muted">分析服务直连主库，只读拉取时间区间内变化的项目和关联采购材料，写入本地普通表。成功后从本批截止时间继续；已完成分析的项目保持原结果及标签。</p>
  <Alert type={state.data?.ready?'success':'warning'} message={state.data?.ready?'本地来源表可用':'尚未建立当前数据源的本地表数据，请同步首批'} description="启用运行调度中的自动增量同步后，会连续处理尚未追平的时间批次；追平后按配置间隔检查主库。仅对尚未分析的项目自动下载材料、建立分析任务。"/>
  <Descriptions column={3} items={[
   {key:'projects',label:'本地项目',children:state.data?.counts?.projects??'—'},
   {key:'notices',label:'本地公告',children:state.data?.counts?.notices??'—'},
   {key:'attachments',label:'本地附件',children:state.data?.counts?.attachments??'—'},
   {key:'cutoff',label:'上次成功截止时间',children:dateTime(state.data?.checkpoint)},
   {key:'next',label:'下次检查时间',children:dateTime(state.data?.next_sync_at)},
   {key:'synced',label:'最近提交时间',children:dateTime(state.data?.synced_at)}
  ]}/>
  {state.data?.job&&<Space><Tag>{salesStatus[state.data.job.status]||state.data.job.status}</Tag><Button size="small" onClick={()=>setLog(state.data!.job!.id)}>查看同步日志</Button></Space>}
  {(state.data?.job?.error||state.data?.last_error)&&<Alert type="error" message={state.data?.job?.error||state.data?.last_error}/>}
 </Card><Card className="section-row" title="同步批次记录">
  {batches.error&&<Alert type="error" message={batches.error}/>}
  <p className="muted small">时间区间采用“开始时间 ≤ 数据时间 &lt; 截止时间”；读取范围包含配置的边界重叠。数量为本批读取的上下文材料，跨批可能重复，入库按唯一键更新。</p>
  <Table<Batch> rowKey="id" dataSource={batches.data?.items||[]} pagination={{current:page,pageSize:10,total:batches.data?.total,onChange:setPage}} scroll={{x:980}} columns={[
   {title:'时间批次',width:300,render:(_,r)=><><div>{dateTime(r.window_start)}</div><div className="muted small">至 {dateTime(r.window_end)}</div></>},
   {title:'状态',render:(_,r)=><Tag color={r.status==='failed'?'gold':r.status==='completed'?'green':undefined}>{salesStatus[r.status]||r.status}</Tag>},
   {title:'项目 / 公告 / 附件',render:(_,r)=>`${r.counts.projects||0} / ${r.counts.notices||0} / ${r.counts.attachments||0}`},
   {title:'待处理 / 保留结果',render:(_,r)=>`${r.counts.pending_projects||0} / ${r.counts.retained_results||0}`},
   {title:'尝试次数',dataIndex:'attempts'},
   {title:'操作',render:(_,r)=><Button size="small" onClick={()=>setBatch(r)}>本批明细</Button>}
  ]}/>
 </Card>{batch&&<Drawer title="时间批次与读取明细" width={1100} open onClose={()=>setBatch(undefined)}><Descriptions items={[{key:'id',label:'批次 ID',children:batch.id},{key:'start',label:'批次开始',children:dateTime(batch.window_start)},{key:'end',label:'本批截止',children:dateTime(batch.window_end)},{key:'read',label:'含重叠的实际读取起点',children:dateTime(batch.read_start)}]}/>{batch.error&&<Alert type="error" message={batch.error}/>}<Records key={batch.id} id={batch.id}/></Drawer>}{log&&<SalesJobLog id={log} onClose={()=>setLog(undefined)}/>}</>;
}
