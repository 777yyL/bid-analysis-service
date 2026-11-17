import {useState} from 'react';
import {App,Alert,Button,Card,Descriptions,Space,Table,Tag} from './ui';
import {PageTitle,go} from './common';
import {salesStatus,send,useSalesResource} from './sales';
import SalesBody from './sales-body';
import SalesCapability from './sales-capability';
import {materialRole,noticeLabel,primaryNoticeIds} from './sales-material';
import type {Project,Match} from './sales-projects';

function Verdict({match}:{match:Match}){return <article className="verdict-record"><Space wrap><h3>{match.name}</h3><Tag color={match.status==='confirmed'?'green':'gold'}>{match.status==='confirmed'?(match.method==='model'||match.method==='model_backfill'?'模型确认':'历史已确认标签'):salesStatus[match.status]||match.status}</Tag></Space><SalesCapability item={match} showTitle={false}/><p className="muted small">召回分数 {match.score?.toFixed(4)} · 不等于匹配概率</p><div className="verdict-reason"><strong>判断依据</strong><p>{match.reason}</p></div>{match.evidence?.map((e,i)=><details key={i}><summary>查看证据 {i+1} · 公告 #{e.notice_id}</summary><blockquote>{e.content}</blockquote></details>)}</article>;}
export default function SalesProjectView({projectKey}:{projectKey:string}){
 const state=useSalesResource<Project>(`/projects/${encodeURIComponent(projectKey)}`,0);const data=state.data;
 const [busy,setBusy]=useState(false);const {message}=App.useApp();
 const confirmed=data?.matches?.filter(m=>m.status==='confirmed')||[],others=data?.matches?.filter(m=>m.status!=='confirmed')||[];
 return <><PageTitle eyebrow="项目详情 / 采购依据与分析结论" title={data?.title||'项目详情'} description={projectKey} actions={<Space><Button onClick={()=>go('/sales-projects')}>返回项目检索</Button><Button loading={state.loading} onClick={()=>void state.refresh()}>刷新</Button><Button loading={busy} disabled={!data} onClick={async()=>{setBusy(true);try{await send('/analysis/start',{project_keys:[projectKey],force:true});message.success('已提交重新分析，可在运行管理查看进度');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}}}>重新分析</Button></Space>}/>
 {state.error&&<Alert type="error" message={state.error}/>}
 {data&&!state.error&&<><Card><Descriptions column={3} items={[{key:'stage',label:'当前阶段',children:data.channel||'阶段为空'},{key:'status',label:'当前结论',children:salesStatus[data.status]||data.status},{key:'main',label:'采购主公告',children:primaryNoticeIds(data).map(id=>noticeLabel(data,id)).join('；')||'缺少主公告'}]}/></Card>
 <div className="detail-columns section-row"><Card title="实际采购需求"><p className="muted small">以下是本次分析留存的需求片段；展开原文可以核对当前正文。</p>{data.procurement_extraction?.documents?.length?data.procurement_extraction.documents.map((d,i)=><section className="detail-demand" key={i}><h3>{d.title||`公告 #${d.notice_id}`}</h3><pre>{d.preview||'未提取到采购需求'}</pre>{d.preview_truncated&&<p className="muted small">片段预览已截断，可展开原文查看。</p>}</section>):<Alert type="info" message="此结果没有保存需求提取预览，请结合标签证据及公告原文核对。"/>}
 {!!data.warnings?.length&&<Alert type="warning" message={data.warnings.join('；')}/>}
 <details><summary>展开公告正文与附件对照</summary><SalesBody projectKey={projectKey}/></details><details><summary>本次材料选择依据</summary><p>{data.selection_reason||'旧结果未记录选择依据'}</p><Table rowKey="notice_id" dataSource={data.notices||[]} pagination={false} columns={[{title:'公告',dataIndex:'title'},{title:'材料角色',render:(_,n)=>materialRole(data,n.notice_id)}]}/></details></Card>
 <Card title="销售项匹配结论"><div className="verdict-list">{confirmed.map(m=><Verdict key={m.sales_item_id} match={m}/>)}{!confirmed.length&&<Alert type="info" message={data.status==='no_match'?'已完成分析，未命中销售项。':'暂无已确认标签，请查看当前状态及处理记录。'}/>}</div>{others.length>0&&<details><summary>查看排除或未完成的候选（{others.length}）</summary><div className="verdict-list">{others.map(m=><Verdict key={m.sales_item_id} match={m}/>)}</div></details>}<details><summary>处理记录</summary><p>运行管理中可查看任务步骤、模型错误和重试情况。</p><Button onClick={()=>go('/sales-tasks')}>进入运行管理</Button></details></Card></div></>}
 </>;
}
