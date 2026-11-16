import {useEffect, useRef, useState} from 'react';
import {Alert, Select, Spin} from './ui';
import {api, query} from './api';

interface Option {value:string; label:string}
export default function SalesFilter({value, onChange}:{value:string[]; onChange:(ids:string[])=>void}) {
 const [search,setSearch] = useState('');
 const [page,setPage] = useState(1);
 const [options,setOptions] = useState<Option[]>([]);
 const [total,setTotal] = useState(0);
 const [loading,setLoading] = useState(false);
 const [error,setError] = useState('');
 const labels = useRef(new Map<string,string>());
 useEffect(()=>{
  const controller = new AbortController();
  let current = true;
  setLoading(true);
  const timer = window.setTimeout(()=>{
   void api<{items:{id:string;name:string}[];total:number}>('/api/v3/sales-items'+query({q:search,enabled:'true',page,page_size:50}),{signal:controller.signal},true)
    .then(data=>{
     if (!current) return;
     const next = data.items.map(item=>({value:item.id,label:item.name}));
     next.forEach(item=>labels.current.set(item.value,item.label));
     setOptions(previous=>page === 1 ? next : Array.from(new Map([...previous,...next].map(item=>[item.value,item])).values()));
     setTotal(data.total); setError('');
    }).catch(e=>{if(current) setError((e as Error).message);})
    .finally(()=>{if(current) setLoading(false);});
  },250);
  return ()=>{current=false;window.clearTimeout(timer);controller.abort();};
 },[search,page]);
 const selected = value.map(id=>({value:id,label:labels.current.get(id)||id}));
 const merged = Array.from(new Map([...selected,...options].map(item=>[item.value,item])).values());
 return <Select mode="multiple" allowClear showSearch filterOption={false} value={value}
  aria-label="按销售项筛选项目" placeholder="搜索销售项名称（可多选）" style={{minWidth:280,maxWidth:'100%'}}
  options={merged} loading={loading} onChange={onChange}
  onSearch={text=>{if(text===search && page===1) return;setSearch(text);setPage(1);setOptions([]);setTotal(0);setError('');}}
  popupRender={menu=><>{error && <Alert type="error" message={`搜索失败：${error}。请重新输入检索。`}/>}<div>{menu}</div>{loading && options.length>0 && <Spin size="small"/>}</>}
  onPopupScroll={event=>{const node=event.currentTarget;if(!loading && !error && options.length<total && node.scrollHeight-node.scrollTop-node.clientHeight<32) {setLoading(true);setPage(previous=>previous+1);}}}
  notFoundContent={loading?<Spin size="small"/>:error?<Alert type="error" message={error}/>: '未找到已启用的销售项'}
 />;
}
