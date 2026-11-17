import {useState} from 'react';
import {Alert, Button} from './ui';
import {PageTitle, go, dateTime} from './common';
import {useSalesResource} from './sales';
import {query} from './api';

export const slices:Record<string,string>={all:'全部涉及项目',new:'新增项目',existing:'已有项目更新',eligible:'关注阶段',out:'阶段不纳入',demand:'有采购需求',unextracted:'待提取需求',no_demand:'无采购需求',missing_primary:'缺少主公告',analyzed:'已分析',matched:'已命中',no_match:'未命中',pending:'待分析',running:'分析中',failed:'分析失败',review_pending:'复核未完成',missing_material:'缺少材料'};
export function yesterday(){return new Date(Date.now()+8*3600000-86400000).toISOString().slice(0,10);}
type Counts=Record<string,number>;
type Report={date:string;timezone:string;generated_at:number;summary:Counts;days:(Counts&{date:string})[]};
export const drill=(day:string,slice:string)=>go('/sales-projects'+query({day,slice}));
export default function SalesDaily(){
 const [day,setDay]=useState(yesterday),[refresh,setRefresh]=useState(0);
 const state=useSalesResource<Report>('/daily'+query({day,refresh:refresh?'true':undefined}),0);
 const m=state.data?.summary;const valid=!!m&&!state.error&&!state.loading;
 const n=(k:string)=>valid?m![k]?.toLocaleString()??'—':'—';
 const link=(k:string,d=day,value?:number)=><button className="daily-count" disabled={!valid} onClick={()=>drill(d,k)}>{value===undefined?n(k):value.toLocaleString()}</button>;
 const pct=valid&&m!.demand?100*m!.analyzed/m!.demand:0;
 return <><PageTitle eyebrow="每日数据 / 处理结果" title="每日概览" description="从每日入库到销售项命中，逐步查看这批项目的处理结果。" actions={<><input className="workbench-date" aria-label="入库日期" type="date" value={day} onChange={e=>{if(e.target.value)setDay(e.target.value);}}/><Button loading={state.loading} onClick={()=>{if(!refresh)setRefresh(1);else void state.refresh();}}>刷新</Button></>}/>
 {state.error&&<Alert type="error" message={'每日数据读取失败：'+state.error}/>}
 <section className="daily-metrics">{[['notices','新增公告','条 · 含已有项目的新公告','all'],['all','涉及项目',`${n('new')} 个新项目 · ${n('existing')} 个已有项目`,'all'],['demand','有采购需求','个项目 · 关注阶段内已提取到需求','demand'],['matched','命中销售项','个项目 · 至少有一个已确认标签','matched']].map(([k,label,note,slice])=><button disabled={!valid} className={k==='matched'?'daily-metric accent':'daily-metric'} key={k} onClick={()=>drill(day,slice)}><span>{label}<span>↗</span></span><strong>{n(k)}</strong><small>{note}</small></button>)}</section>
 <div className="daily-layout"><section className="daily-surface"><div className="daily-surface-title"><h2>这批项目，处理到哪一步了？</h2><p>{day} 新增公告涉及的项目 · 展示当前处理状态</p></div><div className="daily-process">
 <section><h3>01　阶段筛选</h3><p>关注阶段{link('eligible')}</p><p>其他阶段{link('out')}</p><small>优先采购意向、采购公告及更正公告。</small></section>
 <section><h3>02　采购需求</h3><p>有采购需求{link('demand')}</p><p>无采购需求{link('no_demand')}</p><p>等待上游提取{link('unextracted')}</p><p>缺少采购主公告{link('missing_primary')}</p></section>
 <section><h3>03　销售项分析</h3><p>已完成分析{link('analyzed')}</p><p>已命中 / 未命中<span>{link('matched')} / {link('no_match')}</span></p><p>待分析 / 分析中<span>{link('pending')} / {link('running')}</span></p><p>失败 / 复核未完成<span>{link('failed')} / {link('review_pending')}</span></p><p>缺少材料{link('missing_material')}</p></section>
 </div><div className="daily-footnote">有采购需求不代表与软件业务相关；命中与分析进度独立，重跑期间可保留既有标签。</div></section>
 <aside className="daily-surface daily-completion"><h2>分析完成情况</h2><strong>{valid?pct.toFixed(1):'—'}<small> %</small></strong><div className="daily-progress"><span style={{width:Math.min(100,pct)+'%'}}/></div><p>已完成 {n('analyzed')} / {n('demand')} 个有需求项目</p><Button onClick={()=>go('/sales-tasks')}>查看运行情况 →</Button></aside></div>
 <section className="daily-surface"><div className="daily-surface-title"><h2>每日明细</h2><p>主库首次入库日期按 {state.data?.timezone||'Asia/Shanghai'} 归集。项目可能在多天收到新公告，各日项目数不可直接相加去重。</p></div><div className="daily-scroll"><table className="daily-table"><thead><tr><th>入库日期</th><th>新增公告</th>{['all','demand','unextracted','analyzed','pending','failed','matched'].map(k=><th key={k}>{slices[k]}</th>)}</tr></thead><tbody>{valid&&state.data?.days.map(d=><tr key={d.date}><td>{d.date}</td><td>{d.notices.toLocaleString()}</td>{['all','demand','unextracted','analyzed','pending','failed','matched'].map(k=><td key={k}>{link(k,d.date,d[k])}</td>)}</tr>)}</tbody></table>{!valid&&<p className="daily-footnote">{state.loading?'正在读取主库和分析结果…':'无法显示统计，请检查主库连接和字段权限。'}</p>}</div><div className="daily-footnote">{valid?`统计生成于 ${dateTime(state.data?.generated_at)}；短时缓存 30 秒，点击刷新重新读取。未关联项目的公告：${m!.unlinked_notices} 条。`:'统计失败不显示为零。'}</div></section>
 </>;
}
