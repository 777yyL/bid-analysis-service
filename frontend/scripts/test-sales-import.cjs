const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');
const context = {exports: {}, TextDecoder, Uint8Array};
const source = fs.readFileSync(path.join(root, 'src/sales-import.ts'), 'utf8');
vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, context);
const {parseSalesImport, decodeSalesBytes} = context.exports;
let count = 0;
function test(name, run) {run(); count++; console.log(`PASS ${name}`);}
const parse = text => JSON.parse(JSON.stringify(parseSalesImport(text)));
test('published CSV template', () => {
  const items = parse(fs.readFileSync(path.join(root, '../examples/sales-items-template.csv'), 'utf8'));
  assert.equal(items[0].name, '访客管理');
  assert.ok(items[0].description.includes('访客预约'));
});
for (const sep of [',', ';', '\t']) test(`separator ${JSON.stringify(sep)}`, () => {
  const items = parse(['销售项名称', '能力描述'].join(sep) + '\r\n' + ['访客管理', '预约审批'].join(sep));
  assert.equal(items[0].name, '访客管理');
  assert.equal(items[0].description, '预约审批');
});
test('Excel separator directive and multiline quotes', () => {
  const row = parse('sep=;\r\n销售项名称;能力描述\r\n访客管理;"预约\n审批;支持""微信"""')[0];
  assert.equal(row.description, '预约\n审批;支持"微信"');
});
test('header aliases and whitespace', () => {
  const row = parse('\uFEFF 二级销售项名称 , 销售项 描述 ,产品名称\n访客管理,预约审批,园区平台')[0];
  assert.equal(row.name, '访客管理');
  assert.deepEqual(row.products, ['园区平台']);
});
test('JSON uses same aliases', () => assert.equal(parse('[{"销售项名称":"访客管理","功能描述":"预约审批"}]')[0].description, '预约审批'));
test('missing headers report actual first line', () => assert.throws(() => parse('错误列,描述\n访客,预约'), /当前首行：错误列,描述/));
test('missing field identifies exact data row', () => assert.throws(() => parse('name,description\n访客,预约\n视频,'), /第 2 条缺少能力描述/));
test('extra delimiter is not silently truncated', () => assert.throws(() => parse('name,description\n访客,预约,审批'), /列数/));
test('duplicated mapped headers are rejected', () => assert.throws(() => parse('name,description,能力描述\n访客,预约,审批'), /重复表头/));
test('XLSX content gives format-specific error', () => assert.throws(() => decodeSalesBytes(Uint8Array.from([0x50,0x4b,3,4]).buffer), /另存为 CSV/));
test('GBK export decodes without replacement characters', () => {
  // GBK bytes for name,description + newline + 访客管理,访客预约.
  const bytes = Buffer.concat([Buffer.from('name,description\n'), Buffer.from('b7c3bfcdb9dcc0ed2cb7c3bfcdd4a4d4bc', 'hex')]);
  const row = parse(decodeSalesBytes(Uint8Array.from(bytes).buffer))[0];
  assert.equal(row.name, '访客管理');
  assert.equal(row.description, '访客预约');
});
console.log(`${count} import checks passed`);
