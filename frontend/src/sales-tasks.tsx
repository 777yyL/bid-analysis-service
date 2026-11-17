import {useState} from 'react';
import {dateTime,PageTitle,go} from './common';
import {salesStatus,send,useSalesResource} from './sales';
import SalesJobLog from './sales-job-log';
import SalesProjectTest from './sales-project-test';
import {api,query} from './api';
import {App,Alert,Button,Card,Table,Tag,Space} from './ui';
interface Task{id:string;kind:string;status:string;project_key:string;phase?:string;error?:string;updated_at:number}
interface Operations{items:Task[];total:number;counts:Record<string,number>;paused:boolean;worker_enabled:boolean}
const phases:Record<string,string>={reading_project:'读取采购材料',matching:'召回销售项',model_review:'模型复核',existing_result_retained:'已有结果保留',completed:'结果保存'};
export default function SalesTasks(){
 const [page,setPage]=useState(1),[status,setStatus]=useState<string>(),[logId,setLogId]=useState<string>(),[busy,setBusy]=useState(false);
 const state=useSalesResource<Operations>('/operations'+query({page,page_size:30,status}),0);const {message}=App.useApp();
 const action=async(fn:()=>Promise<unknown>)=>{setBusy(true);try{await fn();await state.refresh();message.success('操作已提交');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}};
 return <><PageTitle eyebrow="任务运行 / 异常处理" title="运行管理" description="处理进度、失败原因与任务日志集中查看。任务次数与项目数量分别统计。" actions={<Button loading={state.loading} onClick={()=>void state.refresh()}>刷新</Button>}/>
 {state.error&&<Alert type="error" message={state.error}/>}
 {!state.error&&state.data&&<><Card><Space wrap><Tag color={state.data.paused?'gold':'green'}>{!state.data.worker_enabled?'后台未启动':state.data.paused?'调度已暂停':'调度运行中'}</Tag><span className="muted small">暂停只影响新任务领取，已发出的请求会继续完成。</span><Button loading={busy} onClick={()=>action(()=>api('/settings',{method:'PUT',body:JSON.stringify({paused:!state.data?.paused})}))}>{state.data.paused?'恢复任务':'暂停新任务'}</Button><SalesProjectTest/></Space></Card>
 <section className="daily-metrics section-row">{[['queued','排队任务'],['running','执行中'],['failed','失败任务'],['completed','已完成任务']].map(([key,label])=><button key={key} className="daily-metric" onClick={()=>{setStatus(key);setPage(1);}}><span>{label} ↗</span><strong>{(state.data?.counts[key]||0).toLocaleString()}</strong><small>次 · 非去重项目数</small></button>)}</section></>}
 <div className="operation-tabs">{[['','全部任务'],['running','分析中'],['queued','排队中'],['retry','等待重试'],['failed','失败'],['completed','已完成']].map(([key,label])=><button key={key} className={(status||'')===key?'active':''} onClick={()=>{setStatus(key||undefined);setPage(1);}}>{label}</button>)}</div>
 <Card><Table<Task> rowKey="id" dataSource={state.error?[]:state.data?.items||[]} loading={state.loading} pagination={{current:page,pageSize:30,total:state.error?0:state.data?.total,onChange:setPage,showTotal:n=>`共 ${n} 条任务记录`}} scroll={{x:980}} columns={[
 {title:'项目标识',render:(_,j)=><a onClick={()=>go('/sales-projects/'+encodeURIComponent(j.project_key))}>{j.project_key||'—'}</a>},
 {title:'当前步骤',render:(_,j)=>j.phase?(phases[j.phase]||j.phase):'—'},
 {title:'状态',render:(_,j)=><Tag color={j.status==='failed'?'red':undefined}>{salesStatus[j.status]||j.status}</Tag>},
 {title:'最近更新',render:(_,j)=>dateTime(j.updated_at)},
 {title:'处理说明',render:(_,j)=><span className="job-error">{j.error||'—'}</span>},
 {title:'操作',render:(_,j)=><Space><Button onClick={()=>setLogId(j.id)}>查看日志</Button>{j.status==='failed'&&<Button loading={busy} onClick={()=>action(()=>send(`/jobs/${j.id}/retry`))}>重试</Button>}</Space>} ]}/></Card>
 {logId&&<SalesJobLog id={logId} onClose={()=>setLogId(undefined)}/>}</>;
}
