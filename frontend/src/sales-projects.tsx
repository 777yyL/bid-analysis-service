import {categoryTree} from './sales-category-tree';
import {useState} from 'react';
import {Alert, Button, Card, Cascader, Input, InputNumber, Select, Space, Table, Tag} from './ui';
import {query} from './api';
import {dateTime, go, PageTitle} from './common';
import SalesFilter from './sales-filter';
import {type ProcurementExtraction} from './sales-extraction';

import {type ProjectNotice} from './sales-material';
import {salesStatus, useSalesResource} from './sales';
import {ListFilter, RefreshCw} from 'lucide-react';
export interface Evidence {chunk_id:number|string;created_at:string;notice_id:number;attachment_id?:number;content:string;page_start?:number;page_end?:number;source_type?:string}
export interface Match {sales_item_id:string;name:string;description?:string;score:number;status:string;method:string;reason:string;categories:string[];products:string[];evidence:Evidence[]}
export interface Project {procurement_extraction?:ProcurementExtraction;project_key:string;title:string;channel:string;status:string;matches:Match[];updated_at:number;revision?:string;warnings?:string[];notices?:ProjectNotice[];main_notice_id?:number;main_notice_ids?:number[];correction_notice_ids?:number[];selection_reason?:string}
export default function SalesProjects(){
 const [page,setPage]=useState(1),[q,setQ]=useState(''),[item,setItem]=useState<string[]>([]),[mode,setMode]=useState('any');
 const [searchText,setSearchText]=useState('');
 const [hit,setHit]=useState<string|undefined>('true');
 const [minScore,setMinScore]=useState<number>();const [scoreDraft,setScoreDraft]=useState<number|null>(null);
 const [categorySelection,setCategorySelection]=useState<string[]>([]);const [category,setCategory]=useState<string>();const [product,setProduct]=useState<string>();
 const filters=useSalesResource<{categories:string[];products:string[];sales_items?:{id:string;name:string;categories:string[]}[]}>('/filters/options',0);
 const state=useSalesResource<{items:Project[];total:number}>(`/projects${query({page,page_size:30,q,sales_item_id:item.join(','),match_mode:mode,hit,category,product,min_score:minScore})}`,0);

 const reset=()=>{setQ('');setSearchText('');setHit('true');setItem([]);setMode('any');setCategorySelection([]);setCategory(undefined);setProduct(undefined);setMinScore(undefined);setScoreDraft(null);setPage(1);};
 return <div className="opportunity-page">
  <PageTitle eyebrow="PROJECT OPPORTUNITIES" title="全部已记录项目" description="从采购需求发现销售机会，打开项目查看能力描述、采购原文与匹配证据。"/>
  <Card className="opportunity-filters">
   <div className="project-search-row"><Input.Search aria-label="搜索项目" placeholder="搜索项目名称或编号" value={searchText} onChange={e=>setSearchText(e.target.value)} onSearch={value=>{setQ(value);setPage(1);}}/><Select aria-label="是否命中销售项" value={hit} placeholder="全部项目" allowClear onChange={v=>{setHit(v);setPage(1);}} options={[{value:'true',label:'已命中销售项'},{value:'false',label:'未命中销售项',disabled:minScore!==undefined}]}/></div>
   <div className="project-filter-row">
    <div className="project-filter-field"><label>所属分类</label><Cascader aria-label="按分类筛选" placeholder="全部分类 / 销售项" showSearch allowClear changeOnSelect value={categorySelection} onChange={values=>{const v=values.map(String);setCategorySelection(v);const last=v[v.length-1];if(last?.startsWith('@sales:')){setCategory(v.length>1?v[v.length-2]:undefined);setItem([last.slice(7)]);}else{setCategory(last);setItem([]);}setPage(1);}} options={categoryTree(filters.data?.categories||[],filters.data?.sales_items||[])}/></div>
    <div className="project-filter-field"><label>关联产品</label><Select aria-label="按关联产品筛选" placeholder="全部关联产品" showSearch allowClear value={product} onChange={v=>{setProduct(v);setPage(1);}} options={filters.data?.products.map(v=>({value:v,label:v}))}/></div>
    <div className="project-filter-field score-filter"><label>标签相似度大于</label><Space.Compact><InputNumber aria-label="命中标签相似度阈值" placeholder="不限" min={0} max={1} step={0.01} value={scoreDraft} onChange={v=>setScoreDraft(v)}/><Button onClick={()=>{setMinScore(scoreDraft??undefined);if(scoreDraft!==null&&hit==='false')setHit('true');setPage(1);}}>应用阈值</Button><Button disabled={minScore===undefined&&scoreDraft===null} onClick={()=>{setScoreDraft(null);setMinScore(undefined);setPage(1);}}>清除</Button></Space.Compact></div>
   </div>
   <details className="advanced-filter-disclosure"><summary><ListFilter size={14}/>更多筛选{item.length>0&&<span> · 已选 {item.length} 个销售项</span>}</summary><div className="project-filter-row advanced-sales-filter"><div className="project-filter-field"><label>指定销售项</label><SalesFilter value={item} onChange={ids=>{setItem(ids);setPage(1);}}/></div><Select aria-label="销售项满足方式" value={mode} onChange={v=>{setMode(v);setPage(1);}} options={[{value:'any',label:'满足任一销售项'},{value:'all',label:'同时满足全部销售项'}]}/><Button type="text" onClick={reset}>重置筛选</Button></div></details>
  </Card>
  <Card className="section-row opportunity-results">
   <div className="project-results-heading"><div><h2>采购项目 <span>{state.data?.total??'—'}</span></h2><p className="muted small">{hit==='true'?'已命中销售项':hit==='false'?'未命中销售项':'全部命中状态'}{minScore!==undefined?` · 仅展示相似度严格大于 ${minScore} 的标签`:' · 按最近更新时间排序'}</p></div><Button onClick={()=>{void state.refresh();}}><RefreshCw size={14}/>刷新列表</Button></div>
   {state.error&&<Alert type="error" showIcon message={state.error}/>}
   <Table<Project> rowKey="project_key" dataSource={state.data?.items||[]} loading={state.loading} pagination={{current:page,pageSize:30,total:state.data?.total,onChange:setPage,showSizeChanger:false,showTotal:(total,range)=>`共 ${total} 个项目 · 当前 ${range[0]}–${range[1]}`}} scroll={{x:960}} columns={[
    {title:'采购项目',width:340,render:(_,r)=><div className="project-name-cell"><a onClick={()=>go('/sales-projects/'+encodeURIComponent(r.project_key))}>{r.title||r.project_key}</a><div className="muted small" title={r.project_key}>{r.project_key}</div></div>},
    {title:'命中销售项',width:330,render:(_,r)=><Space wrap>{r.matches?.filter(m=>m.status==='confirmed').map(m=><Tag className="sales-hit-tag" key={m.sales_item_id}>{m.name}</Tag>)}{!r.matches?.some(m=>m.status==='confirmed')&&<span className="muted">—</span>}</Space>},
    {title:'项目阶段',dataIndex:'channel',width:110},
    {title:'分析状态',width:125,render:(_,r)=><span className={`project-state ${r.status==='matched'?'is-matched':''}`}>{salesStatus[r.status]||r.status}</span>},
    {title:'更新时间',width:160,render:(_,r)=>dateTime(r.updated_at)}
   ]}/>
  </Card>
 </div>;
}
export {default as SalesProjectView} from './sales-project-detail';
