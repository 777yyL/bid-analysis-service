import {useState} from 'react';
import {App, Alert, Button, Descriptions, Space, Tag} from './ui';
import {dateTime} from './common';
import {send, useSalesResource} from './sales';
import SalesProjectTest from './sales-project-test';

interface Overview {source_sync?:{checkpoint?:string;next_sync_at?:number;last_error?:string};settings:{paused:boolean;history_enabled:boolean;incremental_enabled:boolean};history:{phase:string;processed:number;error?:string;updated_at:number}}
export default function SalesOperations({beforeStart}:{beforeStart:()=>Promise<boolean>}) {
 const state=useSalesResource<Overview>('/overview');
 const [busy,setBusy]=useState(false);const {message}=App.useApp();
 const action=async(mode:string,start:boolean)=>{if(start&&!await beforeStart())return;setBusy(true);try{await send(`/analysis/${start?'start':'stop'}`,{mode});await state.refresh();message.success('调度操作已提交');}catch(e){message.error((e as Error).message);}finally{setBusy(false);}};
 return <div className="section-row">
  {state.error&&<Alert type="error" message={state.error}/>}
  {state.data?.settings.paused&&<Alert type="warning" showIcon message="调度已暂停，新任务将在恢复后执行"/>}
  <Space wrap><Button type="primary" loading={busy} onClick={()=>action('history',true)}>启动存量项目补跑</Button><Button disabled={!state.data?.settings.history_enabled} loading={busy} onClick={()=>action('history',false)}>停止补跑</Button><Button loading={busy} onClick={()=>action('incremental',true)}>启动自动增量同步</Button><Button disabled={!state.data?.settings.incremental_enabled} loading={busy} onClick={()=>action('incremental',false)}>停止自动同步</Button><SalesProjectTest/></Space>
  <Descriptions className="section-row" column={3} items={[
   {key:'phase',label:'自动增量同步',children:<Tag>{state.data?.settings.incremental_enabled?'已开启':'未开启'}</Tag>},
   {key:'processed',label:'成功截止时间',children:dateTime(state.data?.source_sync?.checkpoint)},
   {key:'updated',label:'下次主库检查',children:dateTime(state.data?.source_sync?.next_sync_at)}
  ]}/>
  {state.data?.source_sync?.last_error&&<Alert type="error" message={state.data.source_sync.last_error}/>}
 </div>;
}
