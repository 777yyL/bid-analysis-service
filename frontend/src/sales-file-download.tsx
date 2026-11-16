import {useState} from 'react';
import {App, Alert, Button, Card, Input, Space, Tag} from './ui';
import {salesStatus, send, useSalesResource} from './sales';
import SalesJobLog from './sales-job-log';

interface DownloadJob {id:string;status:string;error?:string;progress:{phase?:string;processed?:number;errors?:number};payload:{recent_errors?:{project_key:string;warnings:string[]}[]}}
export default function SalesFileDownload({beforeStart}:{beforeStart:()=>Promise<boolean>}) {
 const state=useSalesResource<DownloadJob|null>('/source/files/status');
 const [key,setKey]=useState('');const [busy,setBusy]=useState(false);const [log,setLog]=useState<string>();
 const [result,setResult]=useState<{status:string;documents:number;attachments:number;warnings:string[];storage:string}>();
 const {message}=App.useApp();
 const running=state.data&&['queued','retry','running'].includes(state.data.status);
 const run=async(test:boolean)=>{if(!await beforeStart())return;setBusy(true);try{if(test)setResult(await send('/source/files/test',{project_key:key.trim()}));else{await send('/source/files/download');await state.refresh();message.success('材料下载任务已提交');}}catch(e){message.error((e as Error).message);}finally{setBusy(false);}};
 return <Card className="section-row" title="本地材料下载" extra={<Button loading={busy} disabled={!!running} onClick={()=>run(false)}>下载已同步项目的材料</Button>}>
  <p className="muted">配置分享服务后，每次时间批次成功后，自动下载本批尚未分析项目的采购正文、相关更正及附件；已完成项目不会自动重跑。仅解析文本参与匹配，附件原件留存；主服务尚未解析的附件不会在此执行 OCR。暂停调度也会暂停批量下载。</p>
  {state.error&&<Alert type="error" message={state.error}/>}
  {state.data&&<Space wrap><Tag>{salesStatus[state.data.status]||state.data.status}</Tag><span>已处理 {state.data.progress.processed||0} 个项目</span><span>材料提示 {state.data.progress.errors||0} 个项目</span><Button onClick={()=>setLog(state.data!.id)}>查看下载日志</Button></Space>}
  {state.data?.error&&<Alert type="error" message={state.data.error}/>}
  {!!state.data?.progress.errors&&<Alert className="section-row" type="warning" message="部分项目材料缺失、下载失败或材料选择需确认，请查看日志；修复后可再次下载，已缓存文件会校验更新。"/>}
  <div className="section-row"><Space.Compact style={{width:'100%',maxWidth:650}}><Input aria-label="材料下载测试项目" placeholder="输入已同步的 project_key，测试正文和附件下载" value={key} onChange={e=>setKey(e.target.value)}/><Button disabled={!key.trim()} loading={busy} onClick={()=>run(true)}>测试下载</Button></Space.Compact></div>
  {result&&<Alert className="section-row" type={result.warnings.length?'warning':'success'} message={`可读文本 ${result.documents} 份 · 关联附件 ${result.attachments} 份 · ${salesStatus[result.status]||result.status}`} description={result.warnings.join('；')||'材料读取成功，未调用向量或复核模型。'}/>}
  {log&&<SalesJobLog id={log} onClose={()=>setLog(undefined)}/>}
 </Card>;
}
