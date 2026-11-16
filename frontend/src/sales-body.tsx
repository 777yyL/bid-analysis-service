import {useState} from 'react';
import {Alert, Button, Select, Space} from './ui';
import {send} from './sales';

interface Evidence {notice_id:number;attachment_id?:number;content:string}
interface Document {notice_id:number;attachment_id?:number;source_type:string;chunk_title?:string;content:string}
export default function SalesBody({projectKey,evidence}:{projectKey:string;evidence?:Evidence}) {
 const [documents,setDocuments]=useState<Document[]>();const [selected,setSelected]=useState(0);
 const [loading,setLoading]=useState(false);const [error,setError]=useState('');const [warnings,setWarnings]=useState<string[]>([]);
 const load=async()=>{setLoading(true);setError('');try{
  const result=await send<{documents:Document[];warnings:string[]}>('/analysis/body',{project_key:projectKey});
  setDocuments(result.documents);setWarnings(result.warnings||[]);
  const match=result.documents.findIndex(d=>d.notice_id===evidence?.notice_id&&(d.attachment_id||null)===(evidence?.attachment_id||null));setSelected(match>=0?match:0);
 }catch(e){setError((e as Error).message);}finally{setLoading(false);}};
 return <section className="sales-body"><Space wrap><Button loading={loading} onClick={load}>{documents?'重新读取原文':'读取公告正文'}</Button>
  {documents&&<Select aria-label="选择公告或附件原文" style={{minWidth:240,maxWidth:'100%'}} value={selected} onChange={setSelected} options={documents.map((d,i)=>({value:i,label:`${d.source_type==='attachment'?'附件':'正文'} · #${d.notice_id} · ${d.chunk_title||d.attachment_id||''}`}))}/>}
 </Space><p className="muted small">按需读取当前文本目录中的原文，不触发分析。文件发生变化时，可能与上次分析采用的文本不同。</p>
 {error&&<Alert type="error" message={error}/>}{warnings.length>0&&<Alert type="warning" message={warnings.join('；')}/>}
 {documents?.length===0&&<Alert type="info" message="未读到可用正文，请检查本地来源数据、文件分享地址和材料提示。"/>}
 <div className={evidence?'body-comparison':'body-single'}>
 <div>{documents?.[selected]&&<><h4>公告／附件原文</h4><pre className="body-original">{documents[selected].content}</pre></>}</div>
 {evidence&&<div><h4>本次命中证据 · 公告 #{evidence.notice_id}{evidence.attachment_id?` / 附件 #${evidence.attachment_id}`:''}</h4><blockquote className="evidence-drawer">{evidence.content}</blockquote></div>}
 </div></section>;
}
