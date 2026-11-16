import {useEffect,useState} from 'react';
import {ArrowUpRight, Layers3, LogOut, FolderSearch, Shapes, Settings2, KeyRound} from 'lucide-react';
import {Button,Input,Alert} from './ui';
import {api,savedKey,saveKey} from './api';
import Sales from './sales';
import SalesSettings from './sales-settings';
import SalesProjects,{SalesProjectView} from './sales-projects';
const route=()=>window.location.hash.slice(1)||'/sales-projects';
export default function Workbench(){
 const [key,setKey]=useState(savedKey),[connected,setConnected]=useState(false),[busy,setBusy]=useState(!!savedKey()),[error,setError]=useState(''),[path,setPath]=useState(route);
 useEffect(()=>{const change=()=>{setPath(route());window.scrollTo(0,0);};window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);},[]);
 const connect=async()=>{setBusy(true);setError('');saveKey(key.trim());try{await api('/api/v3/settings',{},true);setConnected(true);}catch(e){setError((e as Error).message);}finally{setBusy(false);}};
 useEffect(()=>{if(savedKey())void connect();},[]);
 if(!connected)return <div className="login-shell"><div className="login-intro"><span className="brand-mark"><Layers3 size={24}/></span><h1>标析</h1><p>采购需求与销售能力，在这里连接。</p></div><section className="login-form"><div className="login-eyebrow">招标分析工作台</div><h2>连接你的工作空间</h2><p className="muted">使用独立分析服务的访问密钥登录。</p><form onSubmit={e=>{e.preventDefault();void connect();}}><label htmlFor="access-key">服务访问密钥</label><Input.Password id="access-key" prefix={<KeyRound size={16}/>} value={key} onChange={e=>setKey(e.target.value)} autoComplete="current-password" placeholder="ANALYZER_API_KEY"/>{error&&<Alert type="error" message={error}/>}<Button type="primary" block htmlType="submit" loading={busy} disabled={!key.trim()}>进入工作台 <ArrowUpRight size={16}/></Button></form><p className="small muted">访问密钥仅保存在当前标签页会话中。</p></section></div>;
 const selected=path==='/sales-settings'?'/sales-settings':path==='/sales'?'/sales':'/sales-projects';
 const nav=[{path:'/sales-projects',name:'项目',icon:FolderSearch},{path:'/sales',name:'销售项',icon:Shapes},{path:'/sales-settings',name:'系统配置',icon:Settings2}];
 let content=<SalesProjects/>;if(path.startsWith('/sales-projects/'))content=<SalesProjectView key={path} projectKey={decodeURIComponent(path.slice(16))}/>;else if(path==='/sales')content=<Sales/>;else if(path==='/sales-settings')content=<SalesSettings/>;
 return <div className="workspace"><header className="app-header"><a href="#/sales-projects" className="brand"><span className="brand-mark"><Layers3 size={19}/></span><strong>标析</strong><span className="brand-divider"/><span className="brand-caption">招标分析</span></a><nav aria-label="主导航">{nav.map(i=><a key={i.path} href={'#'+i.path} aria-current={selected===i.path?'page':undefined} className={selected===i.path?'active':''}><i.icon size={16}/>{i.name}</a>)}</nav><div className="header-account"><span className="connection-state"><i/>分析服务已连接</span><Button type="text" aria-label="退出登录" onClick={()=>{saveKey('');setKey('');setConnected(false);}}><LogOut size={16}/><span>退出</span></Button></div></header><main className="workspace-main">{content}</main><footer className="workspace-footer"><span>标析 · 招标数据分析</span><span>以采购原文为依据</span></footer></div>;
}
