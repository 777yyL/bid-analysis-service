import {useCallback, useEffect, useRef, useState} from 'react';
import {Alert, Button, Empty, Skeleton, Space, Tag as Pill, Tooltip} from 'antd';
import {ReloadOutlined} from '@ant-design/icons';
import {api} from './api';
import type {Tag} from './types';
export const statusText: Record<string, string> = {confirmed: '已确认', candidate: '候选', excluded: '已排除', queued: '排队中', running: '处理中', retry: '等待重试', ready: '规则完成', completed: '模型完成', needs_review: '待复核', failed: '失败', pending: '待处理', rule_done: '规则完成', model_pending: '待模型复核', model_done: '模型完成', context_only: '仅更新进展', unselected: '未选作分析材料', not_selected: '未选作分析材料', missing_primary: '缺少采购分析材料', missing_material: '采购材料待补齐', stale: '版本已过期'};
export const sourceText: Record<string, string> = {rule: '规则', model: '模型', manual: '人工'};
export function dateTime(value?: number | string) {
  if (!value) return '—';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString('zh-CN', {hour12: false});
}
export const go = (path: string) => {window.location.hash = path;};
export function useResource<T>(path: string, pollMilliseconds = 0) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const sequence = useRef(0);
  const inFlight = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(async (silent = false) => {
    if (inFlight.current) return;
    inFlight.current = true;
    const request = new AbortController();
    controller.current = request;
    const id = ++sequence.current;
    if (!silent) setLoading(true);
    try {const result = await api<T>(path, {signal: request.signal}); if (sequence.current === id) {setData(result); setError('');}}
    catch (error) {if (sequence.current === id) setError(error instanceof Error ? error.message : '请求失败');}
    finally {if (sequence.current === id) {inFlight.current = false; controller.current = null; setLoading(false);}}
  }, [path]);
  const refresh = useCallback(() => load(), [load]);
  useEffect(() => {
    void refresh();
    const timer = pollMilliseconds > 0 ? window.setInterval(() => {void load(true);}, pollMilliseconds) : undefined;
    return () => {
      if (timer !== undefined) window.clearInterval(timer);
      sequence.current++;
      controller.current?.abort();
      controller.current = null;
      inFlight.current = false;
    };
  }, [load, refresh, pollMilliseconds]);
  return {data, error, loading, refresh};
}
export function LoadState({loading, error, retry, children}: {loading: boolean; error: string; retry: () => void; children: React.ReactNode}) {
  if (error) return <Alert type="error" showIcon message="无法读取数据" description={error} action={<Button onClick={retry}>重试</Button>}/>;
  if (loading) return <Skeleton active paragraph={{rows: 6}}/>;
  return <>{children}</>;
}
export function StatePill({status}: {status: string}) {
  const color = ['confirmed', 'completed', 'model_done'].includes(status) ? 'green' : ['failed', 'excluded'].includes(status) ? 'red' : ['running'].includes(status) ? 'processing' : ['candidate', 'needs_review'].includes(status) ? 'gold' : 'default';
  return <Pill color={color}>{statusText[status] || status}</Pill>;
}
export function TagList({tags, dimension, limit = 4}: {tags: Tag[]; dimension?: 'business' | 'product'; limit?: number}) {
  const items = (tags || []).filter(tag => (!dimension || tag.dimension === dimension) && tag.status !== 'excluded');
  if (!items.length) return <span className="muted">未识别</span>;
  return <Space size={[2, 4]} wrap>{items.slice(0, limit).map((tag, index) => <Tooltip key={`${tag.category}-${tag.label}-${index}`} title={`${tag.category} · ${statusText[tag.status] || tag.status} · ${sourceText[tag.source] || tag.source}`}><Pill className={`dimension-${tag.dimension}`} bordered={false}>{tag.label}{tag.status === 'candidate' && ' · 待确认'}</Pill></Tooltip>)}{items.length > limit && <Pill>+{items.length - limit}</Pill>}</Space>;
}
export function PageTitle({eyebrow, title, description, actions}: {eyebrow: string; title: string; description: string; actions?: React.ReactNode}) {
  return <div className="page-title"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div><Space wrap>{actions}</Space></div>;
}
export const Refresh = ({onClick}: {onClick: () => void}) => <Button icon={<ReloadOutlined/>} onClick={onClick}>刷新</Button>;
export const NoData = ({description = '暂无数据'}: {description?: string}) => <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description}/>;
