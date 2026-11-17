import {useState} from 'react';
import {Alert,Button,Card,Input,Select,Space,Table,Tag,Cascader,InputNumber,Drawer} from './ui';
import {PageTitle,go,dateTime} from './common';
import {useSalesResource} from './sales';
import {query} from './api';
import {slices,yesterday} from './sales-daily';
import {categoryTree} from './sales-category-tree';
import SalesBody from './sales-body';

interface Row {project_key:string;title:string;channel:string;publish_at:string;notices:number;demand:string;progress:string;recorded:boolean;matches:{sales_item_id:string;name:string;score:number}[]}
const demand:Record<string,string>={yes:'有采购需求',no:'无采购需求',pending:'待提取',out:'阶段不纳入',missing_primary:'缺少主公告'};
const progress:Record<string,string>={completed:'已完成',pending:'待分析',running:'分析中',failed:'失败',awaiting:'等待需求提取',skipped:'无需分析',missing_primary:'缺少主公告',missing_material:'缺少材料',review_pending:'复核未完成'};
export default function SalesCohort(){
 const params=new URLSearchParams(window.location.hash.split('?')[1]);
 const [day,setDay]=useState(params.get('day')||yesterday()),[slice,setSlice]=useState(params.get('slice')||'matched');
 const [q,setQ]=useState(''),[page,setPage]=useState(1),[category,setCategory]=useState<string>(),[product,setProduct]=useState<string>(),[channel,setChannel]=useState<string>(),[score,setScore]=useState<number>();
 const [selected,setSelected]=useState<Row>();
 const filters=useSalesResource<{categories:string[];products:string[]}>('/filters/options',0);
 const state=useSalesResource<{items:Row[];total:number;generated_at:number}>('/daily/projects'+query({day,slice,q,page,page_size:30,category,product,channel,min_score:score}),0);
 const change=(fn:()=>void)=>{fn();setPage(1);};
 return <><PageTitle eyebrow="采购机会 / 项目检索" title="项目检索" description="按新增公告入库日查看涉及项目，未完成分析不会计为未命中。" actions={<Button onClick={()=>go('/sales-projects/all')}>查看全部已记录项目</Button>}/>
 <Card><div className="cohort-filters"><label>新增公告入库日<input className="workbench-date" type="date" value={day} onChange={e=>e.target.value&&change(()=>setDay(e.target.value))}/></label><label className="grow">项目名称或编号<Input.Search placeholder="输入关键词后搜索" onSearch={v=>change(()=>setQ(v))}/></label><label>筛选范围<Select value={slice} onChange={v=>change(()=>setSlice(v))} options={Object.entries(slices).map(([value,label])=>({value,label}))}/></label></div>
 <div className="cohort-filters"><label>销售项分类<Cascader aria-label="销售项分类" allowClear changeOnSelect options={categoryTree(filters.data?.categories||[])} onChange={v=>change(()=>setCategory(v.length?String(v[v.length-1]):undefined))}/></label><label>关联产品<Select allowClear placeholder="全部产品" value={product} onChange={v=>change(()=>setProduct(v))} options={filters.data?.products.map(v=>({value:v,label:v}))}/></label><label>项目阶段<Select allowClear placeholder="全部阶段" value={channel} onChange={v=>change(()=>setChannel(v))} options={['采购意向','采购公告','更正公告','结果公告','合同公告','验收公告','废标终止','其他'].map(v=>({value:v,label:v}))}/></label><details><summary>更多筛选</summary><label>标签相似度大于<InputNumber min={0} max={1} step={0.01} value={score} onChange={v=>change(()=>setScore(v??undefined))}/></label></details></div><p className="muted small">{day} · {slices[slice]}{category||product||score?' · 标签条件仅匹配已确认标签':''}</p>{filters.error&&<Alert type="error" message={filters.error}/>}</Card>
 <Card className="section-row"><div className="project-results-heading"><h2>采购项目 · {state.error?'—':state.data?.total??'—'}</h2><Button loading={state.loading} onClick={()=>void state.refresh()}>刷新列表</Button></div>{state.error&&<Alert type="error" message={state.error}/>}
 <Table<Row> rowKey="project_key" loading={state.loading} dataSource={state.error?[]:state.data?.items||[]} pagination={{current:page,pageSize:30,total:state.error?0:state.data?.total,onChange:setPage,showTotal:n=>`共 ${n} 个项目`}} scroll={{x:1050}} columns={[
 {title:'项目名称',width:340,render:(_,r)=><div><a className="project-name-cell" onClick={()=>r.recorded?go('/sales-projects/'+encodeURIComponent(r.project_key)):setSelected(r)}>{r.title}</a><p className="muted small">{r.notices} 条新增公告</p></div>},
 {title:'项目阶段',render:(_,r)=>r.channel||'阶段为空'},
 {title:'采购需求',render:(_,r)=><Tag>{demand[r.demand]||r.demand}</Tag>},
 {title:'分析进度',render:(_,r)=><Tag color={r.progress==='failed'?'red':undefined}>{progress[r.progress]||r.progress}</Tag>},
 {title:'已确认销售项',width:250,render:(_,r)=><Space wrap>{r.matches.map(m=><Tag color="green" key={m.sales_item_id}>{m.name}</Tag>)}{!r.matches.length&&(r.progress==='completed'?'未命中':'—')}</Space>},
 {title:'发布时间',render:(_,r)=>dateTime(r.publish_at)}]}/></Card>
 <Drawer width="85vw" title={selected?.title} open={!!selected} onClose={()=>setSelected(undefined)}>{selected&&<><Alert type="info" message={`${demand[selected.demand]} · ${progress[selected.progress]}，尚无已保存的分析结论。`}/><SalesBody projectKey={selected.project_key}/></>}</Drawer></>;
}
