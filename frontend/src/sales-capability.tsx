import {categoryParts} from './sales-category-tree';
import {Tag, Typography} from './ui';

export interface Capability {sales_item_id:string;name:string;description?:string;categories?:string[];products?:string[]}
function Metadata({label,values}:{label:string;values?:string[]}) {
 const unique=[...new Set((values||[]).filter(Boolean))];
 return <div className="capability-meta"><span className="capability-label">{label}</span><div className="capability-values">{unique.length?unique.map(value=><Tag key={value}>{label==='所属分类'?categoryParts(value).join(' / '):value}</Tag>):<span className="capability-empty">暂未关联</span>}</div></div>;
}
export default function SalesCapability({item,compact=false,showTitle=true}:{item:Capability;compact?:boolean;showTitle?:boolean}) {
 return <section className={`sales-capability${compact?' sales-capability-compact':''}`} aria-label={`${item.name}能力信息`}>
  {showTitle&&<div className="capability-heading"><strong>{item.name}</strong></div>}
  <Typography.Paragraph className="capability-description" ellipsis={compact?{rows:2,expandable:true,symbol:'展开描述'}:false}>{item.description||'暂无能力描述'}</Typography.Paragraph>
  <Metadata label="所属分类" values={item.categories}/>
  <Metadata label="关联产品" values={item.products}/>
  {!compact&&<div className="capability-id">销售项 ID <span>{item.sales_item_id}</span></div>}
 </section>;
}

export function SalesCapabilityList({items}:{items:Capability[]}) {
 if(!items.length)return <span className="capability-empty">暂无命中销售项</span>;
 return <div className="capability-list">{items.slice(0,2).map(item=><SalesCapability key={item.sales_item_id} item={item} compact/>)}{items.length>2&&<details className="capability-more"><summary>展开其余 {items.length-2} 个销售项</summary>{items.slice(2).map(item=><SalesCapability key={item.sales_item_id} item={item} compact/>)}</details>}</div>;
}
