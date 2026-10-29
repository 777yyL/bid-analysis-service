import {useState} from 'react';
import {Button, Card, Input, Space, Table, Timeline} from 'antd';
import type {Notice, Page, Project, Tag} from './types';
import {dateTime, go, LoadState, NoData, PageTitle, Refresh, StatePill, TagList, useResource} from './common';
import {query} from './api';
export default function Projects() {
  const [search, setSearch] = useState(''); const [page, setPage] = useState(1);
  const state = useResource<Page<Project>>(`/projects${query({q: search, page, page_size: 20})}`, 5000);
  return <><PageTitle eyebrow="PROJECT INTELLIGENCE" title="项目视图" description="按项目串联公告阶段，保留每个标签的来源。" actions={<Refresh onClick={state.refresh}/>}/><Card className="filter-card"><Input.Search placeholder="搜索项目名称或项目标识" allowClear enterButton="搜索项目" onSearch={value => {setSearch(value); setPage(1);}}/></Card><Card title={<Space>项目列表 <span className="count-badge">{state.data?.total || 0}</span></Space>}><LoadState loading={false} error={state.error} retry={state.refresh}><Table<Project> rowKey="project_key" dataSource={state.data?.items || []} loading={state.loading} scroll={{x: 850}} pagination={{current: page, pageSize: 20, total: state.data?.total || 0, showSizeChanger: false, onChange: setPage}} columns={[
    {title: '项目', width: 340, render: (_, item) => <div className="notice-title"><a onClick={() => go(`/projects/${encodeURIComponent(item.project_key)}`)}>{item.title || item.project_key}</a><div className="project-key">{item.project_key}</div></div>},
    {title: '公告数', dataIndex: 'notice_count', width: 90}, {title: '业务标签', render: (_, item) => <TagList tags={item.tags} dimension="business"/>}, {title: '产品标签', render: (_, item) => <TagList tags={item.tags} dimension="product"/>}, {title: '更新时间', dataIndex: 'updated_at', width: 170, render: dateTime}
  ]}/></LoadState></Card></>;
}
export function ProjectView({projectKey}: {projectKey: string}) {
  const state = useResource<{project_key: string; notices: Notice[]; tags: Tag[]}>(`/projects/${encodeURIComponent(projectKey)}`, 5000);
  return <><PageTitle eyebrow="PROJECT TIMELINE" title="项目进展" description={projectKey} actions={<><Button onClick={() => go('/projects')}>返回项目列表</Button><Refresh onClick={state.refresh}/></>}/><LoadState {...state} retry={state.refresh}>{state.data && <><Card title="项目标签汇总"><div className="project-tags"><div><span className="muted">业务</span><TagList tags={state.data.tags} dimension="business" limit={30}/></div><div><span className="muted">产品</span><TagList tags={state.data.tags} dimension="product" limit={30}/></div></div><p className="muted small">标签来自下方关联公告；查看公告可定位具体原文证据。</p></Card><Card title={`公告时间线 · ${state.data.notices.length} 条`} className="section-row">{state.data.notices.length ? <Timeline items={state.data.notices.map(item => ({color: item.notice_type.includes('终止') || item.notice_type.includes('废标') ? '#ce8529' : '#087d7c', children: <div className="timeline-notice"><Space wrap><span className="timeline-stage">{item.notice_type || '公告'}</span><span className="muted">{item.publish_at ? dateTime(item.publish_at) : `发布日期未知 · 分析更新 ${dateTime(item.updated_at)}`}</span><StatePill status={item.analysis_status}/></Space><h3><a onClick={() => go(`/notices/${item.notice_id}`)}>{item.title}</a></h3><p className="muted">{item.buyer}</p><TagList tags={item.tags} limit={12}/></div>}))}/> : <NoData/>}</Card></>}</LoadState></>;
}
