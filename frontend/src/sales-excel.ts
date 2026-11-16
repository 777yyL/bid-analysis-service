import {parseSalesImport} from './sales-import';

export async function createSalesTemplate() {
 const ExcelJS = (await import('exceljs')).default;
 const workbook = new ExcelJS.Workbook();
 workbook.creator = '招标分析工作台';
 const sheet = workbook.addWorksheet('销售项', {views:[{state:'frozen', ySplit:1}]});
 sheet.columns = [
  {header:'销售项ID',key:'id',width:26}, {header:'销售项名称',key:'name',width:22},
  {header:'能力描述',key:'description',width:85}, {header:'分类',key:'categories',width:38},
  {header:'产品',key:'products',width:48},
 ];
 sheet.addRow({id:'visitor-management',name:'访客管理',description:'访客管理提供访客预约、访客审批、访客权限管理、黑名单、访客登记、访客签离、保安核验、访客通知、通行记录与访客轨迹查看等功能。支持微信公众号、企业微信、钉钉等三方集成。支持针对多场景预约自定义表单控制。',categories:'人员通行、智慧访客',products:'智慧园区管理平台、综合安防管理平台'});
 sheet.getRow(1).height = 30;
 sheet.getRow(1).eachCell(cell => {
  cell.font = {bold:true,color:{argb:'FFFFFFFF'}};
  cell.fill = {type:'pattern',pattern:'solid',fgColor:{argb:'FF164E63'}};
  cell.alignment = {vertical:'middle'};
 });
 sheet.getRow(2).height = 100;
 sheet.getRow(2).eachCell(cell => {cell.alignment = {wrapText:true,vertical:'top'};});
 sheet.autoFilter = 'A1:E1';
 const guide = workbook.addWorksheet('填写说明');
 guide.columns = [{header:'字段 / 操作',key:'field',width:25},{header:'填写说明',key:'help',width:110}];
 [
  ['销售项名称','必填。填写销售项名称，例如“访客管理”。'],
  ['能力描述','必填。说明该销售项提供的能力；可以包含换行，无需匹配到技术规格。只有销售项名称和能力描述参与向量化。'],
  ['销售项ID','建议填写稳定且唯一的编号。相同ID会更新已有销售项；留空时系统按名称生成ID。修改名称时应保留原ID。'],
  ['分类','选填。多个分类用顿号“、”分隔，仅用于关联、展示及检索，不参与向量化。'],
  ['产品','选填。多个产品用顿号“、”分隔，命中销售项后带出关联产品，不参与向量化。'],
  ['示例数据','“销售项”工作表第2行为访客管理示例。正式导入前删除或替换此行，保留第1行表头。'],
  ['导入方式','填写“销售项”工作表并保存为 .xlsx，在销售项能力页面导入。先检查预览，再提交，最后执行向量化。'],
  ['填写限制','每次最多10000条；请填写文本，不使用公式。文件最大10MB。其他工作表不会导入。'],
 ].forEach(([field,help])=>guide.addRow({field,help}));
 guide.getRow(1).font = {bold:true};
 guide.eachRow(row=>{row.height=45;row.eachCell(cell=>{cell.alignment={wrapText:true,vertical:'top'};});});
 return workbook;
}

export async function downloadSalesTemplate() {
 const workbook = await createSalesTemplate();
 const buffer = await workbook.xlsx.writeBuffer();
 const url = URL.createObjectURL(new Blob([buffer], {type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
 const anchor = document.createElement('a');
 anchor.href = url; anchor.download = 'sales-items-template.xlsx';
 document.body.appendChild(anchor); anchor.click(); anchor.remove();
 window.setTimeout(()=>URL.revokeObjectURL(url),1000);
}

export async function parseSalesExcel(buffer:ArrayBuffer) {
 if (buffer.byteLength > 10 * 1024 * 1024) throw Error('Excel 文件不能超过10MB，请分批导入');
 const ExcelJS = (await import('exceljs')).default;
 const workbook = new ExcelJS.Workbook();
 try {await workbook.xlsx.load(buffer);}
 catch {throw Error('无法读取 Excel，请确认文件为有效的 .xlsx 工作簿；旧版 .xls 请另存为 .xlsx');}
 const sheet = workbook.getWorksheet('销售项') || workbook.worksheets[0];
 if (!sheet) throw Error('Excel 没有工作表');
 if (sheet.rowCount > 10001) throw Error('每次最多导入10000条销售项，请分批导入');
 const rows:string[][] = [];
 sheet.eachRow(row=>{
  const cells:string[] = [];
  row.eachCell({includeEmpty:true}, cell=>{
   if (cell.type === ExcelJS.ValueType.Formula) throw Error(`第 ${row.number} 行包含公式，请粘贴为文本值后导入`);
   cells.push(cell.text);
  });
  if (cells.some(value=>value.trim())) rows.push(cells);
 });
 const width = rows[0]?.length || 0;
 return parseSalesImport(rows.map(cells=>{
  if (cells.length > width && cells.slice(width).some(value=>value.trim())) throw Error('Excel 中存在没有表头的数据列，请补齐表头或清除多余内容');
  return Array.from({length:width},(_,i)=>`"${(cells[i] || '').replaceAll('"','""')}"`).join(',');
 }).join('\r\n'));
}
