const aliases: Record<string, string> = {
  id: 'id', '销售项id': 'id', '销售项编号': 'id',
  name: 'name', '销售项': 'name', '销售项名称': 'name', '二级销售项': 'name', '二级销售项名称': 'name',
  description: 'description', '能力描述': 'description', '销售项描述': 'description',
  '销售项能力描述': 'description', '功能描述': 'description', '能力说明': 'description',
  categories: 'categories', '分类': 'categories', '销售项分类': 'categories',
  products: 'products', '产品': 'products', '产品名称': 'products', '关联产品': 'products',
};
const headerKey = (value: string) => value.replace(/[\s\uFEFF\u200B]/g, '').toLowerCase();

export function decodeSalesBytes(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  if (bytes[0] === 0x50 && bytes[1] === 0x4b) throw Error('检测到 Excel 工作簿，请使用 .xlsx 扩展名导入，或另存为 CSV');
  if (bytes[0] === 0xff && bytes[1] === 0xfe) return new TextDecoder('utf-16le', {fatal: true}).decode(bytes);
  if (bytes[0] === 0xfe && bytes[1] === 0xff) return new TextDecoder('utf-16be', {fatal: true}).decode(bytes);
  try {return new TextDecoder('utf-8', {fatal: true}).decode(bytes);}
  catch {return new TextDecoder('gb18030', {fatal: true}).decode(bytes);}
}

function readRows(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') {cell += '"'; i++;}
      else quoted = !quoted;
    } else if (c === delimiter && !quoted) {row.push(cell); cell = '';}
    else if ((c === '\n' || c === '\r') && !quoted) {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell);
      if (row.some(value => value.trim())) rows.push(row);
      row = []; cell = '';
    } else cell += c;
  }
  if (quoted) throw Error('CSV 引号未闭合，请检查含换行或引号的单元格');
  row.push(cell);
  if (row.some(value => value.trim())) rows.push(row);
  return rows;
}

export function parseSalesCsv(raw: string): Record<string, unknown>[] {
  let text = raw.replace(/^\uFEFF/, '').trimStart();
  const separator = /^sep=([,;\t])\r?\n/i.exec(text);
  if (separator) text = text.slice(separator[0].length);
  let rows: string[][] | undefined;
  for (const delimiter of separator ? [separator[1]] : [',', '\t', ';']) {
    const candidate = readRows(text, delimiter);
    const headers = candidate[0]?.map(h => aliases[headerKey(h)]) || [];
    if (headers.includes('name') && headers.includes('description')) {rows = candidate; break;}
  }
  if (!rows) {
    const firstLine = text.split(/\r?\n/, 1)[0] || '空文件';
    throw Error(`未识别到“销售项名称”和“能力描述”表头。当前首行：${firstLine.slice(0, 200)}。请参考下载模板，使用逗号、制表符或分号分隔。`);
  }
  const headers = rows.shift()!.map(h => aliases[headerKey(h)] || h.trim());
  const recognized = headers.filter(h => Object.values(aliases).includes(h));
  if (new Set(recognized).size !== recognized.length) throw Error('存在映射到同一字段的重复表头，请保留一列销售项名称、能力描述及各关联字段');
  return rows.map((cells, index) => {
    if (cells.length !== headers.length) throw Error(`第 ${index + 1} 条数据列数为 ${cells.length}，表头为 ${headers.length} 列；含分隔符或换行的内容需用英文双引号包围`);
    return Object.fromEntries(headers.map((h, i) => [h, cells[i]]));
  });
}

export function parseSalesImport(raw: string): Record<string, unknown>[] {
  const text = raw.replace(/^\uFEFF/, '').trim();
  const parsed = text.startsWith('[') || text.startsWith('{') ? JSON.parse(text) : parseSalesCsv(text);
  const list = Array.isArray(parsed) ? parsed : parsed?.items;
  if (!Array.isArray(list) || !list.length) throw Error('文件中没有销售项');
  return list.map((item: unknown, index: number) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) throw Error(`第 ${index + 1} 条不是有效销售项对象`);
    const row = Object.fromEntries(Object.entries(item).map(([key, value]) => [aliases[headerKey(key)] || key, value]));
    const missing = [['name', '销售项名称'], ['description', '能力描述']].filter(([key]) => typeof row[key] !== 'string' || !row[key].trim()).map(([, label]) => label);
    if (missing.length) throw Error(`第 ${index + 1} 条缺少${missing.join('、')}；识别到字段：${Object.keys(row).join('、')}`);
    for (const key of ['categories', 'products']) {
      if (!Array.isArray(row[key])) row[key] = String(row[key] || '').split(/[、,，;；]/).map(v => v.trim()).filter(Boolean);
    }
    return row;
  });
}
