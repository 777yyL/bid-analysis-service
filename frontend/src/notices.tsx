import {useMemo, useState} from 'react';
import {App, Alert, Button, Card, Descriptions, Drawer, Form, Input, Modal, Select, Space, Table, Tabs, Tag as Pill, Timeline} from 'antd';
import {DownloadOutlined, FilterOutlined, PlusOutlined, RobotOutlined} from '@ant-design/icons';
import {exportCsv, post, query} from './api';
import type {Dimension, Evidence, Knowledge, Notice, NoticeDetail, Page, ProjectAnalysis, Tag} from './types';
import {dateTime, go, LoadState, NoData, PageTitle, Refresh, sourceText, StatePill, TagList, useResource} from './common';
export default function Notices() {
  const [filters, setFilters] = useState({q: '', dimension: '', category: '', business_category: '', product_category: '', tag_status: '', analysis_status: ''});
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<React.Key[]>([]);
  const [busy, setBusy] = useState(false);
  const {message} = App.useApp();
  const state = useResource<Page<Notice>>(`/notices${query({...filters, page, page_size: 20})}`, 5000);
  const update = (key: keyof typeof filters, value: string) => {setFilters(current => ({...current, [key]: value})); setPage(1); setSelected([]);};
  const batch = async () => {
    setBusy(true);
    try {
      const projects = new Set<string>();
      for (const selectedId of selected) {
        const item = state.data?.items.find(notice => notice.notice_id === Number(selectedId));
        if (item?.project_key) {
          if (projects.has(item.project_key)) continue;
          projects.add(item.project_key);
          await post('/project-analysis/reanalyze', {project_key: item.project_key, use_model: true, force: false});
        } else await post(`/notices/${selectedId}/reanalyze`, {use_model: true});
      }
      message.success('已按所选公告归属的项目提交复核，同项目自动合并');
      setSelected([]); await state.refresh();
    } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}
  };
  return <><PageTitle eyebrow="NOTICE EXPLORER" title="公告与证据" description="按公告追溯证据；分析任务按项目选择主公告及对应更正。" actions={<><Refresh onClick={state.refresh}/><Button icon={<DownloadOutlined/>} disabled={!state.data?.items.length} onClick={() => exportCsv((state.data?.items || []).map(item => ({公告ID: item.notice_id, 标题: item.title, 采购人: item.buyer, 公告阶段: item.notice_type, 业务标签: item.tags.filter(t => t.dimension === 'business' && t.status !== 'excluded').map(t => t.label).join('；'), 产品标签: item.tags.filter(t => t.dimension === 'product' && t.status !== 'excluded').map(t => t.label).join('；'), 状态: item.analysis_status})), '公告当前页.csv')}>导出当前页</Button></>}/>
    <Card className="filter-card"><div className="filter-heading"><FilterOutlined/>筛选公告</div><div className="filter-fields"><Input.Search placeholder="公告标题、采购人或公告 ID" allowClear onSearch={value => update('q', value)} enterButton="搜索"/><Select aria-label="标签维度" placeholder="全部维度" allowClear value={filters.dimension || undefined} onChange={value => update('dimension', value || '')} options={[{value: 'business', label: '业务维度'}, {value: 'product', label: '产品维度'}]}/><Input aria-label="业务域筛选" placeholder="业务域（可与产品组合）" value={filters.business_category} onChange={event => update('business_category', event.target.value)}/><Input aria-label="产品分类筛选" placeholder="产品分类（可与业务组合）" value={filters.product_category} onChange={event => update('product_category', event.target.value)}/><Select aria-label="标签状态" placeholder="全部标签状态" allowClear value={filters.tag_status || undefined} onChange={value => update('tag_status', value || '')} options={[{value: 'confirmed', label: '已确认'}, {value: 'candidate', label: '候选'}, {value: 'excluded', label: '已排除'}]}/><Select aria-label="分析状态" placeholder="全部分析状态" allowClear value={filters.analysis_status || undefined} onChange={value => update('analysis_status', value || '')} options={['ready', 'completed', 'needs_review', 'queued', 'running', 'failed', 'context_only', 'unselected', 'missing_primary'].map(value => ({value, label: {ready: '规则完成', completed: '模型完成', needs_review: '待复核', queued: '排队中', running: '处理中', failed: '失败', context_only: '仅更新进展', unselected: '未选作材料', missing_primary: '缺少采购分析材料'}[value]}))}/></div></Card>
    <Card className="table-card" title={<Space>公告列表 <span className="count-badge">{state.data?.total.toLocaleString() || 0}</span></Space>} extra={<Button icon={<RobotOutlined/>} disabled={!selected.length} loading={busy} onClick={batch}>复核所属项目 {selected.length || ''}</Button>}><LoadState loading={false} error={state.error} retry={state.refresh}><Table<Notice> rowKey="notice_id" loading={state.loading} dataSource={state.data?.items || []} rowSelection={{selectedRowKeys: selected, onChange: setSelected}} scroll={{x: 1150}} pagination={{current: page, pageSize: 20, total: state.data?.total || 0, showSizeChanger: false, showTotal: total => `共 ${total} 条`, onChange: value => {setPage(value); setSelected([]);}}} columns={[
      {title: '公告 / 采购人', key: 'title', width: 350, render: (_, item) => <div className="notice-title"><a onClick={() => go(`/notices/${item.notice_id}`)}>{item.title}</a><div><span className="mono">#{item.notice_id}</span><span>{item.buyer || '采购人未识别'}</span></div></div>},
      {title: '公告阶段', dataIndex: 'notice_type', width: 110, render: value => value || '—'},
      {title: '业务标签', key: 'business', width: 220, render: (_, item) => <TagList tags={item.tags} dimension="business" limit={3}/>},
      {title: '产品标签', key: 'product', width: 220, render: (_, item) => <TagList tags={item.tags} dimension="product" limit={3}/>},
      {title: '状态', width: 130, render: (_, item) => <Space direction="vertical" size={2}><StatePill status={item.analysis_status}/>{!item.material_complete && !['context_only', 'unselected', 'not_selected'].includes(item.analysis_status) && <span className="warning-text">材料待补齐</span>}</Space>},
      {title: '更新时间', dataIndex: 'updated_at', width: 160, render: dateTime}
    ]}/></LoadState></Card></>;
}
function Highlight({text, terms}: {text: string; terms: string[]}) {
  const parts = useMemo(() => {
    const words = [...new Set(terms.filter(Boolean))].sort((a, b) => b.length - a.length);
    if (!words.length) return [text];
    const regex = new RegExp(`(${words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
    return text.split(regex);
  }, [text, terms.join('\u0000')]);
  const normalized = new Set(terms.filter(Boolean).map(term => term.toLowerCase()));
  return <>{parts.map((part, index) => normalized.has(part.toLowerCase()) ? <mark key={index}>{part}</mark> : part)}</>;
}
export function NoticeView({id}: {id: string}) {
  const state = useResource<NoticeDetail>(`/notices/${encodeURIComponent(id)}`, 5000);
  const [activeSource, setActiveSource] = useState('body');
  const [evidence, setEvidence] = useState<Evidence>();
  const [editing, setEditing] = useState<Partial<Tag>>();
  const [editingAnalysis, setEditingAnalysis] = useState<ProjectAnalysis>();
  const [form] = Form.useForm();
  const lexicon = useResource<Knowledge>('/lexicon');
  const formDimension = Form.useWatch('dimension', form) as Dimension | undefined;
  const formCategory = Form.useWatch('category', form) as string | undefined;
  const categoryOptions = [...new Set(formDimension === 'business' ? (lexicon.data?.lines || []).map(line => line.business_domain) : (lexicon.data?.product_keywords || []).map(word => word.category))].map(value => ({value, label: value}));
  const labelOptions = [...new Set(formDimension === 'business' ? (lexicon.data?.lines || []).filter(line => line.business_domain === formCategory).map(line => line.business_line) : (lexicon.data?.product_keywords || []).filter(word => word.category === formCategory).map(word => word.word))].map(value => ({value, label: value}));
  const [busy, setBusy] = useState(false);
  const {message} = App.useApp();
  const data = state.data;
  const activeAnalysis = data?.project_analysis?.find(analysis => analysis.primary_notice_id === Number(id));
  const staleReview = !!editing && (editingAnalysis?.plan_id !== activeAnalysis?.plan_id || editingAnalysis?.revision !== activeAnalysis?.revision);
  const openEdit = (tag?: Tag) => {const value = tag || {dimension: 'product', status: 'confirmed'}; setEditing(value); setEditingAnalysis(activeAnalysis); form.resetFields(); form.setFieldsValue(value);};
  const review = async () => {if (staleReview) {message.warning('采购需求版本已变化，请关闭后重新核验'); return;} try {const values = await form.validateFields(); setBusy(true); if (editingAnalysis) await post('/project-analysis/review', {plan_id: editingAnalysis.plan_id, expected_revision: editingAnalysis.revision, ...values}); else await post(`/notices/${id}/review`, values); message.success('人工判断已保存，自动重跑将保留此结果'); setEditing(undefined); void state.refresh();} catch (error) {if (error instanceof Error) message.error(error.message);} finally {setBusy(false);}};
  const reanalyze = async (use_model: boolean) => {setBusy(true); try {
    if (data?.project_key) await post('/project-analysis/reanalyze', {project_key: data.project_key, use_model, force: true});
    else await post(`/notices/${id}/reanalyze`, {use_model});
    message.success(use_model ? '已按项目选择主公告并提交模型复核' : '已按项目选择主公告并提交规则分析'); await state.refresh();
  } catch (error) {message.error((error as Error).message);} finally {setBusy(false);}};
  const locate = (item: Evidence) => {
    if (item.notice_id && item.notice_id !== Number(id)) {setEvidence(item); return;}
    setActiveSource(item.source.startsWith('attachment:') ? item.source : item.source === 'body' ? 'body' : 'metadata'); setEvidence(item);
  };
  return <><PageTitle eyebrow="EVIDENCE REVIEW" title="公告原文与证据" description={`公告 #${id} · 采购材料来源及历史判断`} actions={<><Button onClick={() => go('/notices')}>返回列表</Button><Refresh onClick={state.refresh}/><Button loading={busy} onClick={() => reanalyze(false)}>重算所属项目</Button><Button type="primary" icon={<RobotOutlined/>} loading={busy} onClick={() => reanalyze(true)}>复核所属项目</Button></>}/><LoadState {...state} retry={state.refresh}>{data && <>
    <Card className="notice-header"><h2>{data.title}</h2><Descriptions size="small" column={{xs: 1, sm: 2, lg: 4}} items={[{key: 'buyer', label: '采购人', children: data.buyer || '—'}, {key: 'type', label: '公告阶段', children: data.notice_type || '—'}, {key: 'status', label: '分析状态', children: <StatePill status={data.analysis_status}/>}, {key: 'project', label: '所属项目', children: data.project_key ? <a onClick={() => go(`/projects/${encodeURIComponent(data.project_key!)}`)}>查看项目</a> : '未关联'}]}/></Card>
    {!!data.project_analysis?.length && <Alert className="section-row" type="info" showIcon message={activeAnalysis ? '本公告已选为项目主公告' : data.project_analysis.some(analysis => analysis.correction_notice_ids?.includes(Number(id))) ? '本公告作为项目需求更正' : '本公告用于进展或历史追溯'} description={<Space direction="vertical" size={5}>{data.project_analysis.map(analysis => <span key={analysis.plan_id}>{analysis.primary_notice_id ? <a onClick={() => go(`/notices/${analysis.primary_notice_id}`)}>主公告：{analysis.primary_title || `#${analysis.primary_notice_id}`}</a> : '缺少采购公告或采购意向'} · <StatePill status={analysis.analysis_status}/></span>)}{data.project_key && <a onClick={() => go(`/projects/${encodeURIComponent(data.project_key!)}`)}>查看项目的选材依据和当前需求标签</a>}</Space>}/>}
    {data.warnings?.length > 0 && <Alert className="section-row" type="warning" showIcon message="材料或分析提示" description={data.warnings.join('；')}/>}
    <div className="detail-layout"><Card className="material-card" title="公告原文与附件" extra={<span className="muted">命中词高亮</span>}><Tabs activeKey={activeSource} onChange={setActiveSource} items={[{key: 'metadata', label: '标题与采购人', children: <div className="material-text"><Highlight text={`${data.title}\n${data.buyer || ''}`} terms={data.tags.flatMap(t => t.evidence.map(e => e.term || ''))}/></div>}, {key: 'body', label: '正文', children: data.notice?.body ? <div className="material-text"><Highlight text={data.notice.body} terms={data.tags.flatMap(t => t.evidence.filter(e => e.source === 'body').map(e => e.term || ''))}/></div> : <NoData description="正文尚未读取"/>}, ...(data.notice?.attachments || []).map(att => ({key: `attachment:${att.att_id}`, label: att.filename || `附件 ${att.att_id}`, children: att.text ? <div className="material-text"><Highlight text={att.text} terms={data.tags.flatMap(t => t.evidence.filter(e => e.source === `attachment:${att.att_id}`).map(e => e.term || ''))}/></div> : <NoData description="附件提取文本不可用"/>}))]}/></Card>
    <Card className="tags-card" title={activeAnalysis ? "项目主公告标签" : "单公告历史标签"} extra={<Button size="small" icon={<PlusOutlined/>} onClick={() => openEdit()}>补充</Button>}><Tabs items={[{key: 'effective', label: `当前结果 (${data.tags.length})`, children: <TagCards tags={data.tags} onEdit={openEdit} onLocate={locate}/>}, {key: 'rule', label: '规则', children: <TagCards tags={data.rule_tags || []} onEdit={openEdit} onLocate={locate}/>}, {key: 'model', label: '模型', children: <TagCards tags={data.model_tags || []} onEdit={openEdit} onLocate={locate}/>}, {key: 'manual', label: '人工', children: <TagCards tags={data.manual_tags || []} onEdit={openEdit} onLocate={locate}/>} ]}/></Card></div>
    <Card title="处理与修改历史" className="section-row">{data.history?.length ? <Timeline items={data.history.map((item, index) => ({key: index, children: <div><strong>{String(item.action || item.source || item.kind || '分析记录')}</strong><span className="history-date">{dateTime((item.created_at || item.at || item.timestamp) as number | string)}</span><pre className="history-json">{JSON.stringify(item, null, 2)}</pre></div>}))}/> : <NoData description="暂无历史记录"/>}</Card>
  </>}</LoadState><Drawer title="证据定位" open={!!evidence} onClose={() => setEvidence(undefined)} width={520}><Space wrap><Pill>{evidence?.source}</Pill>{evidence?.section_role && <Pill>{evidence.section_role === 'demand' ? '采购需求' : evidence.section_role === 'process' ? '招标流程' : '上下文待核验'}</Pill>}</Space><p className="evidence-origin">{evidence?.notice_title || (evidence?.notice_id ? `公告 #${evidence.notice_id}` : '')}</p><p className="evidence-drawer">{evidence && <Highlight text={evidence.text} terms={[evidence.term || '']}/>}</p>{evidence?.notice_id && evidence.notice_id !== Number(id) ? <><Alert type="info" showIcon message="该证据来自另一份关联公告"/><Button className="section-row" type="primary" onClick={() => {go(`/notices/${evidence.notice_id}`); setEvidence(undefined);}}>查看证据原公告 #{evidence.notice_id}</Button></> : <p className="muted">来源文本已在左侧切换；正文与附件中的命中词以高亮显示。</p>}</Drawer>
    <Modal title="人工确认标签" open={editing !== undefined} onCancel={() => {if (!busy) setEditing(undefined);}} onOk={review} confirmLoading={busy} cancelButtonProps={{disabled: busy}} okButtonProps={{disabled: staleReview}} okText="保存判断" destroyOnHidden>{staleReview && <Alert type="warning" showIcon message="采购需求版本已变化，请关闭弹窗后重新核验"/>}<Form form={form} layout="vertical" preserve={false} disabled={busy || staleReview}><Form.Item name="dimension" label="标签维度" rules={[{required: true}]}><Select options={[{value: 'business', label: '业务'}, {value: 'product', label: '产品'}]} onChange={() => form.setFieldsValue({category: undefined, label: undefined})}/></Form.Item><Form.Item name="category" label="业务域 / 产品分类" rules={[{required: true, whitespace: true}]}><Select showSearch options={categoryOptions} placeholder="从词库选择分类" onChange={() => form.setFieldValue('label', undefined)}/></Form.Item><Form.Item name="label" label="业务线 / 产品关键词" rules={[{required: true, whitespace: true}]}><Select showSearch options={labelOptions} placeholder="从词库选择标签"/></Form.Item><Form.Item name="status" label="判断" rules={[{required: true}]}><Select options={[{value: 'confirmed', label: '确认相关'}, {value: 'candidate', label: '保留候选'}, {value: 'excluded', label: '排除标签'}]}/></Form.Item><Form.Item name="reason" label="依据" rules={[{required: true, whitespace: true}]}><Input.TextArea rows={3} maxLength={2000}/></Form.Item></Form></Modal>
  </>;
}
function TagCards({tags, onEdit, onLocate}: {tags: Tag[]; onEdit: (tag: Tag) => void; onLocate: (evidence: Evidence) => void}) {
  if (!tags.length) return <NoData description="该来源暂无标签，未命中不等于无关"/>;
  return <div className="tag-cards">{tags.map((tag, index) => <div className={`tag-card dimension-${tag.dimension}`} key={`${tag.dimension}-${tag.category}-${tag.label}-${index}`}><div className="tag-top"><span className="eyebrow">{tag.dimension === 'business' ? '业务' : '产品'} / {tag.category}</span><Pill bordered={false}>{sourceText[tag.source] || tag.source}</Pill></div><div className="tag-name"><strong>{tag.label}</strong><StatePill status={tag.status}/></div><p>{tag.reason}</p><Space wrap>{(tag.evidence || []).slice(0, 4).map((item, i) => <Button key={i} size="small" onClick={() => onLocate(item)}>证据 {i + 1} · {item.term || item.source}</Button>)}</Space><Button type="link" size="small" className="review-link" onClick={() => onEdit(tag)}>人工纠正</Button></div>)}</div>;
}
