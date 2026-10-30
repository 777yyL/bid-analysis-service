import {useState} from 'react';
import {App, Alert, Button, Card, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Tag as Pill, Timeline} from 'antd';
import {ExperimentOutlined, PlusOutlined, RobotOutlined} from '@ant-design/icons';
import type {Dimension, Evidence, Knowledge, Page, Project, ProjectAnalysis, ProjectDetail, Tag} from './types';
import {dateTime, go, LoadState, NoData, PageTitle, Refresh, sourceText, StatePill, TagList, useResource} from './common';
import {post, query} from './api';

export default function Projects() {
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [busy, setBusy] = useState(false);
  const {message} = App.useApp();
  const state = useResource<Page<Project>>(`/projects${query({q: search, page, page_size: 20})}`, 5000);
  const analyze = async (keys: string[]) => {
    setBusy(true);
    try {
      for (const project_key of keys) await post('/project-analysis/reanalyze', {project_key, use_model: true, force: false});
      message.success(`已提交 ${keys.length} 个项目；将选择主公告及对应更正`);
      setSelected([]);
      await state.refresh();
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  };
  return <>
    <PageTitle eyebrow="PROJECT WORKBENCH" title="项目工作台" description="每个采购单元选择一份主公告，结合对应更正识别实际采购需求。" actions={<Refresh onClick={state.refresh}/>}/>
    <Alert className="selection-policy" showIcon type="info" message="主公告优先级：采购公告 → 采购意向" description="更正公告用于修订对应需求；结果、合同、验收等公告只更新项目进展。"/>
    <Card className="filter-card"><Input.Search placeholder="搜索项目名称或项目标识" allowClear enterButton="搜索项目" onSearch={value => {setSearch(value); setPage(1); setSelected([]);}}/></Card>
    <Card className="table-card" title={<Space>项目列表 <span className="count-badge">{state.data?.total || 0}</span></Space>} extra={<Button type="primary" icon={<RobotOutlined/>} disabled={!selected.length} loading={busy} onClick={() => analyze(selected.map(String))}>分析所选项目 {selected.length || ''}</Button>}>
      <LoadState loading={false} error={state.error} retry={state.refresh}>
        <Table<Project> rowKey="project_key" dataSource={state.data?.items || []} loading={state.loading} scroll={{x: 1380}} rowSelection={{selectedRowKeys: selected, onChange: setSelected}}
          pagination={{current: page, pageSize: 20, total: state.data?.total || 0, showSizeChanger: false, onChange: value => {setPage(value); setSelected([]);}}}
          columns={[
            {title: '项目', width: 300, render: (_, item) => <div className="notice-title"><a onClick={() => go(`/projects/${encodeURIComponent(item.project_key)}`)}>{item.title || item.project_key}</a><div className="project-key">{item.project_key}</div></div>},
            {title: '当前阶段', dataIndex: 'current_stage', width: 115, render: value => value ? <Pill>{value}</Pill> : '未识别'},
            {title: '主公告', width: 130, render: (_, item) => item.primary_notice_ids?.length ? <Space direction="vertical" size={0}>{item.primary_notice_ids.map(id => <a key={id} onClick={() => go(`/notices/${id}`)}>#{id}</a>)}</Space> : <span className="muted">尚未选定</span>},
            {title: '分析状态', width: 150, render: (_, item) => <StatePill status={item.analysis_status || 'pending'}/>},
            {title: '业务标签', width: 190, render: (_, item) => <TagList tags={item.tags} dimension="business"/>},
            {title: '产品标签', width: 190, render: (_, item) => <TagList tags={item.tags} dimension="product"/>},
            {title: '公告数', dataIndex: 'notice_count', width: 75},
            {title: '操作', width: 110, render: (_, item) => <Button size="small" loading={busy} onClick={() => analyze([item.project_key])}>分析项目</Button>},
            {title: '更新时间', dataIndex: 'updated_at', width: 165, render: dateTime}
          ]}/>
      </LoadState>
    </Card>
  </>;
}

function noticeRole(id: number, analyses: ProjectAnalysis[]) {
  if (analyses.some(a => a.primary_notice_id === id)) return {label: '主公告', color: 'cyan'};
  if (analyses.some(a => a.correction_notice_ids?.includes(id))) return {label: '需求更正', color: 'blue'};
  if (analyses.some(a => a.ignored_correction_ids?.includes(id))) return {label: '更正未纳入本次分析', color: 'default'};
  return {label: '进展或未选材料', color: 'default'};
}

export function ProjectView({projectKey}: {projectKey: string}) {
  const state = useResource<ProjectDetail>(`/projects/${encodeURIComponent(projectKey)}`, 5000);
  const lexicon = useResource<Knowledge>('/lexicon');
  const [busy, setBusy] = useState(false);
  const [evidence, setEvidence] = useState<Evidence>();
  const [editing, setEditing] = useState<{analysis: ProjectAnalysis; tag?: Tag}>();
  const [form] = Form.useForm();
  const {message} = App.useApp();
  const data = state.data;
  const analyses = data?.analyses || [];
  const staleReview = !!editing && !analyses.some(analysis => analysis.plan_id === editing.analysis.plan_id && analysis.revision === editing.analysis.revision);
  const dimension = Form.useWatch('dimension', form) as Dimension | undefined;
  const category = Form.useWatch('category', form) as string | undefined;
  const currentCategories = dimension === 'business' ? (lexicon.data?.lines || []).map(line => line.business_domain) : (lexicon.data?.product_keywords || []).map(word => word.category);
  const categoryOptions = [...new Set([...currentCategories, ...(editing?.tag && editing.tag.dimension === dimension ? [editing.tag.category] : [])])].map(value => ({value, label: value}));
  const currentLabels = dimension === 'business' ? (lexicon.data?.lines || []).filter(line => line.business_domain === category).map(line => line.business_line) : (lexicon.data?.product_keywords || []).filter(word => word.category === category).map(word => word.word);
  const labelOptions = [...new Set([...currentLabels, ...(editing?.tag && editing.tag.dimension === dimension && editing.tag.category === category ? [editing.tag.label] : [])])].map(value => ({value, label: value}));
  const analyze = async (use_model: boolean) => {
    setBusy(true);
    try {
      await post('/project-analysis/reanalyze', {project_key: projectKey, use_model, force: true});
      message.success(use_model ? '已按项目重新选择材料并提交模型复核' : '已按项目重新选择材料并提交规则分析');
      await state.refresh();
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  };
  const openReview = (analysis: ProjectAnalysis, tag?: Tag) => {
    setEditing({analysis, tag});
    form.resetFields();
    form.setFieldsValue(tag ? {dimension: tag.dimension, category: tag.category, label: tag.label, status: tag.status, reason: tag.reason} : {dimension: 'product', status: 'confirmed'});
  };
  const saveReview = async () => {
    if (!editing) return;
    if (staleReview) {message.warning('采购需求版本已变化，请关闭弹窗后重新核验'); return;}
    try {
      const values = await form.validateFields();
      setBusy(true);
      await post('/project-analysis/review', {plan_id: editing.analysis.plan_id, expected_revision: editing.analysis.revision, ...values});
      message.success('项目标签的人工判断已保存');
      setEditing(undefined);
      await state.refresh();
    } catch (error) {if (error instanceof Error) message.error(error.message);} finally {setBusy(false);}
  };
  const titleOf = (id: number) => data?.notices.find(n => n.notice_id === id)?.title || `公告 #${id}`;
  return <>
    <PageTitle eyebrow="PROJECT DEMAND REVIEW" title="项目需求分析" description={projectKey} actions={<><Button onClick={() => go('/projects')}>返回项目列表</Button><Refresh onClick={state.refresh}/><Button loading={busy} icon={<ExperimentOutlined/>} onClick={() => analyze(false)}>重算规则</Button><Button type="primary" loading={busy} icon={<RobotOutlined/>} onClick={() => analyze(true)}>模型复核项目</Button></>}/>
    <LoadState {...state} retry={state.refresh}>{data && <>
      <div className="project-stage-strip"><span>当前项目阶段 <Pill color="blue">{data.current_stage || '未识别'}</Pill></span><span>关联公告 <strong>{data.notices.length}</strong> 条</span><span>采购分析单元 <strong>{analyses.length}</strong> 个</span></div>
      <Card title="当前项目标签"><div className="project-tags"><div><span className="muted">业务</span><TagList tags={data.tags} dimension="business" limit={30}/></div><div><span className="muted">产品</span><TagList tags={data.tags} dimension="product" limit={30}/></div></div><p className="muted small">标签来自当前选定的采购材料。项目进入结果、合同或验收阶段时，仍追溯下方主公告。</p></Card>
      {!analyses.length && <Alert className="section-row" showIcon type="info" message="尚未生成采购材料选择结果" description="点击“重算规则”或“模型复核项目”，回溯采购公告、采购意向及对应更正。"/>}
      {analyses.map(analysis => <Card key={analysis.plan_id} className="section-row selection-card" title={<Space wrap><span>采购材料选择</span>{analysis.lot_key && <Pill>标包 {analysis.lot_key}</Pill>}{analysis.round_key && <Pill>轮次 {analysis.round_key}</Pill>}</Space>} extra={<StatePill status={analysis.analysis_status}/>} actions={analysis.primary_notice_id ? [<Button key="add" type="link" icon={<PlusOutlined/>} onClick={() => openReview(analysis)}>补充项目标签</Button>] : undefined}>
        {analysis.primary_notice_id ? <div className="selected-main"><span className="selection-label">主公告</span><div><a className="selected-main-title" onClick={() => go(`/notices/${analysis.primary_notice_id}`)}>{analysis.primary_title || titleOf(analysis.primary_notice_id)}</a><Space wrap><Pill color="cyan">{analysis.primary_type}</Pill><span className="mono muted">#{analysis.primary_notice_id}</span>{!analysis.material_complete && <span className="warning-text">采购材料尚未确认完整</span>}</Space></div></div> : <Alert type="warning" showIcon message="缺少采购分析材料" description="没有找到可用的采购公告或采购意向；不能据此认定业务无关。"/>}
        <Descriptions className="selection-details" size="small" column={1} items={[
          {key: 'reason', label: '选择依据', children: analysis.selection_reason || '等待选择'},
          {key: 'changes', label: '需求更正', children: analysis.correction_notice_ids?.length ? <Space direction="vertical" size={5}>{analysis.correction_notice_ids.map(id => <a key={id} onClick={() => go(`/notices/${id}`)}>{titleOf(id)} <span className="mono">#{id}</span></a>)}</Space> : '无已纳入的需求更正'},
          {key: 'ignored', label: '未纳入的更正', children: analysis.ignored_correction_ids?.length ? <Space wrap>{analysis.ignored_correction_ids.map(id => <a key={id} onClick={() => go(`/notices/${id}`)}>#{id}</a>)}<span className="muted">仅涉及流程或未满足本次关联条件</span></Space> : '无'},
          {key: 'model-routing', label: '模型分流', children: analysis.model_route_reason || '等待规则分析或旧结果尚未分流'},
          {key: 'version', label: '需求版本', children: <span className="mono small">{analysis.revision?.slice(0, 16) || '尚未生成'} · {dateTime(analysis.updated_at)}</span>}
        ]}/>
        {!!analysis.warnings?.length && <Alert className="selection-warnings" type="warning" showIcon message="选择或材料提示" description={analysis.warnings.join('；')}/>}
        {analysis.primary_notice_id && <div className="project-analysis-tags">{analysis.tags?.length ? analysis.tags.map((tag, index) => <div className={`tag-card dimension-${tag.dimension}`} key={`${tag.dimension}-${tag.category}-${tag.label}-${index}`}><div className="tag-top"><span className="eyebrow">{tag.dimension === 'business' ? '业务' : '产品'} / {tag.category}</span><Pill bordered={false}>{sourceText[tag.source] || tag.source}</Pill></div><div className="tag-name"><strong>{tag.label}</strong><StatePill status={tag.status}/></div><p>{tag.reason}</p><Space wrap>{(tag.evidence || []).slice(0, 4).map((item, i) => <Button size="small" key={i} onClick={() => setEvidence(item)}>证据 {i + 1} · {item.notice_id ? `#${item.notice_id}` : item.term || item.source}</Button>)}</Space><Button type="link" size="small" className="review-link" onClick={() => openReview(analysis, tag)}>人工纠正</Button></div>) : <NoData description="当前采购材料尚无标签，未命中不等于无关"/>}</div>}
      </Card>)}
      <Card title={`项目进展 · ${data.notices.length} 条公告`} className="section-row">{data.notices.length ? <Timeline items={data.notices.map(item => {
        const role = noticeRole(item.notice_id, analyses);
        return {color: role.color === 'cyan' ? '#087d7c' : role.color === 'blue' ? '#4c83aa' : '#a8b8c3', children: <div className="timeline-notice"><Space wrap><span className="timeline-stage">{item.notice_type || '公告'}</span><Pill color={role.color}>{role.label}</Pill><span className="muted">{item.publish_at ? dateTime(item.publish_at) : `发布日期未知 · 分析更新 ${dateTime(item.updated_at)}`}</span></Space><h3><a onClick={() => go(`/notices/${item.notice_id}`)}>{item.title}</a></h3><p className="muted">{item.buyer}</p></div>};
      })}/> : <NoData/>}</Card>
    </>}</LoadState>
    <Drawer title="采购证据" open={!!evidence} onClose={() => setEvidence(undefined)} width={520}>
      {evidence && <><Space wrap><Pill>{evidence.source}</Pill><Pill color={evidence.section_role === 'demand' ? 'cyan' : evidence.section_role === 'process' ? 'default' : 'gold'}>{evidence.section_role === 'demand' ? '采购需求' : evidence.section_role === 'process' ? '招标流程' : '上下文待核验'}</Pill></Space><p className="evidence-origin">{evidence.notice_title || (evidence.notice_id ? titleOf(evidence.notice_id) : '来源公告见主公告及更正')}</p><blockquote className="evidence-drawer">{evidence.text}</blockquote>{evidence.notice_id ? <Button type="primary" onClick={() => {go(`/notices/${evidence.notice_id}`); setEvidence(undefined);}}>查看原公告 #{evidence.notice_id}</Button> : <Alert type="info" message="旧结果尚未包含证据公告编号，请重新分析项目补齐来源。"/>}</>}
    </Drawer>
    <Modal title="人工确认项目标签" open={!!editing} onCancel={() => {if (!busy) setEditing(undefined);}} onOk={saveReview} confirmLoading={busy} cancelButtonProps={{disabled: busy}} okButtonProps={{disabled: staleReview}} okText="保存项目判断" destroyOnHidden>
      {staleReview && <Alert className="selection-warnings" type="warning" showIcon message="采购需求版本已变化" description="请关闭弹窗，查看新的主公告与更正后再提交人工判断。"/>}
      <Form form={form} layout="vertical" preserve={false} disabled={busy || staleReview}>
        <Form.Item name="dimension" label="标签维度" rules={[{required: true}]}><Select options={[{value: 'business', label: '业务'}, {value: 'product', label: '产品'}]} onChange={() => form.setFieldsValue({category: undefined, label: undefined})}/></Form.Item>
        <Form.Item name="category" label="业务域 / 产品分类" rules={[{required: true}]}><Select showSearch options={categoryOptions} onChange={() => form.setFieldValue('label', undefined)}/></Form.Item>
        <Form.Item name="label" label="业务线 / 产品关键词" rules={[{required: true}]}><Select showSearch options={labelOptions}/></Form.Item>
        <Form.Item name="status" label="判断" rules={[{required: true}]}><Select options={[{value: 'confirmed', label: '确认相关'}, {value: 'candidate', label: '保留候选'}, {value: 'excluded', label: '排除标签'}]}/></Form.Item>
        <Form.Item name="reason" label="依据" rules={[{required: true, whitespace: true}]}><Input.TextArea rows={3} maxLength={2000}/></Form.Item>
      </Form>
    </Modal>
  </>;
}
