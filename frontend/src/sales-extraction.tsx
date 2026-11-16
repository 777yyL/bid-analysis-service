import {Alert, Card, Collapse, Descriptions, Space, Tag} from './ui';

export interface ProcurementExtraction {
 version:string; original_chars:number; extracted_chars:number; removed_chars:number; uncertain_chars:number;
 omitted_documents?:number;
 documents?:{notice_id:number;attachment_id?:number;source_type:string;title:string;original_chars:number;
   extracted_chars:number;uncertain_chars:number;preview:string;preview_truncated:boolean}[];
}
export default function SalesExtraction({data}:{data?:ProcurementExtraction}) {
 if (!data) return <Alert className="section-row" type="info" message="尚无前置需求提取记录：材料可能未读取成功，或结果来自旧版本。请查看材料提示或重新测试。"/>;
 return <Card className="section-row" title="采购需求提取 · 召回之前">
  <p className="muted">先按采购章节和语句提取需求，再按配置进行本地向量检索或知识库召回。明确流程、资格、业绩证明及联系信息已过滤；待确认片段保留供复核。证据仍对应原文位置。</p>
  <Descriptions size="small" column={{xs:1,sm:2,lg:4}} items={[
   {key:'original',label:'原文字符',children:data.original_chars},
   {key:'extracted',label:'提取字符',children:data.extracted_chars},
   {key:'removed',label:'过滤字符',children:data.removed_chars},
   {key:'uncertain',label:'待确认片段字符',children:data.uncertain_chars},
  ]}/>
  {!data.extracted_chars && <Alert type="warning" message="未提取到具体采购需求，未对全文向量化，也不据此认定项目与销售项无关。"/>}
  <Collapse items={data.documents?.map((document,index)=>({key:String(index),label:<Space wrap><span>公告 #{document.notice_id}{document.attachment_id?` / 附件 #${document.attachment_id}`:' / 正文'}</span><span>{document.title}</span><Tag>{document.extracted_chars} 字</Tag>{document.uncertain_chars>0&&<Tag color="gold">含待确认片段</Tag>}</Space>,children:<>
   {document.preview_truncated&&<Alert type="info" message="此处为部分预览，向量化使用完整提取内容。"/>}
   <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:400,overflowY:'auto'}}>{document.preview||'未提取到采购需求，请核对原文。'}</pre>
  </>}))}/>
  {!!data.omitted_documents&&<p className="muted">另有 {data.omitted_documents} 份材料未在此预览，提取与向量化统计包含全部材料。</p>}
 </Card>;
}
