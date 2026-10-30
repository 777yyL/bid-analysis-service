import {useEffect, useState} from 'react';
import {App, Alert, Button, Card, Form, Input, Layout, Menu, Space, Tag} from 'antd';
import {ApartmentOutlined, BarChartOutlined, DatabaseOutlined, FileSearchOutlined, KeyOutlined, LogoutOutlined, MenuOutlined, RadarChartOutlined, ThunderboltOutlined} from '@ant-design/icons';
import {api, post, savedKey, saveKey} from './api';
import Dashboard from './dashboard';
import Notices, {NoticeView} from './notices';
import Projects, {ProjectView} from './projects';
import Tasks from './tasks';
import Lexicon from './lexicon';
function route() {return window.location.hash.slice(1) || '/projects';}
export default function Workbench() {
  const [key, setKey] = useState(savedKey()); const [connected, setConnected] = useState(false); const [checking, setChecking] = useState(!!savedKey()); const [error, setError] = useState('');
  const [path, setPath] = useState(route()); const [mobileMenu, setMobileMenu] = useState(false); const [demoBusy, setDemoBusy] = useState(false);
  const {message, modal} = App.useApp();
  useEffect(() => {const onHash = () => {setPath(route()); setMobileMenu(false); window.scrollTo(0, 0);}; window.addEventListener('hashchange', onHash); return () => window.removeEventListener('hashchange', onHash);}, []);
  const connect = async (value = key) => {setChecking(true); setError(''); saveKey(value.trim()); try {await api('/config', {}, true); setConnected(true); setKey(value.trim());} catch (error) {setConnected(false); setError((error as Error).message);} finally {setChecking(false);}};
  useEffect(() => {if (savedKey()) void connect(savedKey());}, []);
  const seedDemo = async () => {setDemoBusy(true); try {await post('/demo'); message.success('演示样例已加入分析库'); window.location.hash = '/projects'; setPath('/projects'); window.location.reload();} catch (error) {message.error((error as Error).message);} finally {setDemoBusy(false);}};
  const navigation = [{key: '/projects', icon: <ApartmentOutlined/>, label: '项目工作台'}, {key: '/', icon: <BarChartOutlined/>, label: '分析总览'}, {key: '/notices', icon: <FileSearchOutlined/>, label: '公告与证据'}, {key: '/tasks', icon: <ThunderboltOutlined/>, label: '任务与模型'}, {key: '/lexicon', icon: <DatabaseOutlined/>, label: '词库与规则'}];
  const selected = path.startsWith('/notices') ? '/notices' : path.startsWith('/projects') ? '/projects' : path.startsWith('/tasks') ? '/tasks' : path.startsWith('/lexicon') ? '/lexicon' : '/';
  let content = <Dashboard/>;
  if (path.startsWith('/notices/')) content = <NoticeView key={path} id={decodeURIComponent(path.slice(9))}/>;
  else if (path === '/notices') content = <Notices/>;
  else if (path.startsWith('/projects/')) content = <ProjectView key={path} projectKey={decodeURIComponent(path.slice(10))}/>;
  else if (path === '/projects') content = <Projects/>;
  else if (path === '/tasks') content = <Tasks/>;
  else if (path === '/lexicon') content = <Lexicon/>;
  if (!connected) return <div className="login-screen"><div className="login-brand"><div className="brand-symbol"><RadarChartOutlined/></div><h1>标析<span>招标分析工作台</span></h1><p>业务与产品，双维度洞察。<br/>每一条标签，都有原文依据。</p><div className="login-decoration"><span>READ</span><i/><span>MATCH</span><i/><span>REVIEW</span></div></div><Card className="login-card"><div className="eyebrow">SECURE WORKSPACE</div><h2>连接分析服务</h2><p className="muted">输入独立分析服务的访问密钥。</p><Form layout="vertical" onFinish={() => connect()}><Form.Item label="API Key" required><Input.Password autoComplete="current-password" size="large" prefix={<KeyOutlined/>} value={key} onChange={event => setKey(event.target.value)} placeholder="ANALYZER_API_KEY"/></Form.Item>{error && <Alert type="error" showIcon message={error} className="login-error"/>}<Button type="primary" block size="large" htmlType="submit" loading={checking} disabled={!key.trim()}>进入工作台</Button></Form><p className="small muted">密钥仅保存在当前浏览器标签页会话中。</p></Card><div className="login-footer">BID ANALYSIS / INDEPENDENT INTELLIGENCE SERVICE</div></div>;
  return <Layout className="workbench"><aside className={`sidebar ${mobileMenu ? 'open' : ''}`}><a className="brand" href="#/projects"><span className="brand-symbol"><RadarChartOutlined/></span><span>标析<small>BID ANALYSIS</small></span></a><div className="nav-caption">分析工作空间</div><Menu theme="dark" mode="inline" selectedKeys={[selected]} items={navigation} onClick={({key}) => {window.location.hash = key; setMobileMenu(false);}}/><div className="sidebar-bottom"><div><span className="live-dot"/>独立分析服务</div><small>业务标签 × 产品标签</small><Button size="small" ghost loading={demoBusy} onClick={() => modal.confirm({title: '载入演示样例？', content: '将明确标识的演示公告写入分析库，便于验证页面与分析流程。', onOk: seedDemo})}>载入演示样例</Button></div></aside>{mobileMenu && <div className="sidebar-mask" onClick={() => setMobileMenu(false)}/>}<Layout className="main-layout"><header className="topbar"><Space><Button className="mobile-toggle" type="text" icon={<MenuOutlined/>} aria-label="展开导航" onClick={() => setMobileMenu(!mobileMenu)}/><span className="breadcrumb">分析工作空间 <span>/</span> {navigation.find(item => item.key === selected)?.label}</span></Space><Space><Tag bordered={false} color="cyan">独立部署</Tag><Button type="text" icon={<LogoutOutlined/>} onClick={() => {saveKey(''); setKey(''); setConnected(false);}}>退出</Button></Space></header><main className="main-content">{content}</main><footer className="app-footer"><span>标析 · 招标数据分析工作台</span><span>规则识别 · 模型复核 · 证据追溯</span></footer></Layout></Layout>;
}
