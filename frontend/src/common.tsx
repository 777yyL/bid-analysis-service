import type {ReactNode} from 'react';
export function dateTime(value?:number|string){if(!value)return '—';const d=new Date(typeof value==='number'?value*1000:value);return Number.isNaN(d.valueOf())?String(value):d.toLocaleString('zh-CN',{hour12:false});}
export const go=(path:string)=>{window.location.hash=path;};
export function PageTitle({title,description,actions}:{eyebrow?:string;title:string;description:string;actions?:ReactNode}){return <div className="page-title"><div><h1>{title}</h1><p>{description}</p></div>{actions&&<div className="page-actions">{actions}</div>}</div>;}
