import {useState} from 'react';
import {Alert, Button, Space} from './ui';
import {send, useSalesResource} from './sales';
import {api} from './api';
import SalesJobLog from './sales-job-log';

interface Job {id:string;status:string;error?:string;progress:{phase?:string;enqueued?:number}}
export default function SalesHistoryReview({beforeStart}:{beforeStart:()=>Promise<boolean>}) {
 const state=useSalesResource<Job|null>('/review/history/status',5000);
 const [counts,setCounts]=useState<{projects:number;matches:number;threshold:number}>();
 const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [log,setLog]=useState<string>();
 const run=async(start:boolean)=>{setBusy(true);setError('');try{
  if(!await beforeStart())return;
  if(start){await send('/review/history/start');await state.refresh();}
  else setCounts(await api('/api/v3/review/history/preview',{},true));
 }catch(e){setError((e as Error).message);}finally{setBusy(false);}};
 const job=state.data;
 return <section className="section-row"><h3>已分析项目 · 历史低分复核</h3>
  <p className="muted">仅复核已有证据的低分自动标签与候选，不重新召回、不重新向量化。跳过人工处理、已模型复核、已排除及过期结果。模型排除会移除标签，依据不足保留待确认；变更保留审计记录。</p>
  <Space wrap><Button loading={busy} onClick={()=>run(false)}>保存并统计待复核项</Button><Button type="primary" loading={busy} disabled={!!job&&['queued','running','retry'].includes(job.status)} onClick={()=>run(true)}>启动历史低分复核</Button>{job&&<Button onClick={()=>setLog(job.id)}>查看扫描日志</Button>}</Space>
  {counts&&<p>相似度低于 {counts.threshold}：{counts.projects} 个项目，{counts.matches} 个销售项结果待检查。缺失原文证据者会记录提示并跳过。</p>}
  {job&&<p>扫描任务：{job.status} · 已入队 {job.progress.enqueued||0} 个项目。扫描完成不代表模型复核已完成，请到销售项能力页查看 review_existing 任务及日志。</p>}
  {(error||state.error||job?.error)&&<Alert type="error" message={error||state.error||job?.error}/>}
  {log&&<SalesJobLog id={log} onClose={()=>setLog(undefined)}/>}
 </section>;
}
