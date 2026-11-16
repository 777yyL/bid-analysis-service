const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const root = path.resolve(__dirname, '..');
function load(name) {
 const context = {exports:{}, TextDecoder, Uint8Array, ArrayBuffer, require: id=>id.startsWith('./') ? load(id.slice(2)) : require(id)};
 const source = fs.readFileSync(path.join(root, `src/${name}.ts`), 'utf8');
 vm.runInNewContext(ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText, context);
 return context.exports;
}
async function main() {
 const {createSalesTemplate, parseSalesExcel} = load('sales-excel');
 const workbook = await createSalesTemplate();
 const encode = async book => {const bytes = await book.xlsx.writeBuffer();return Uint8Array.from(bytes).buffer;};
 const buffer = await encode(workbook);
 const rows = await parseSalesExcel(buffer);
 assert.equal(rows.length, 1);
 assert.equal(rows[0].name, '访客管理');
 assert.ok(rows[0].description.includes('访客预约'));
 assert.equal(rows[0].products.length, 2);
 assert.equal(workbook.worksheets.length, 2);
 fs.mkdirSync(path.join(root, '../dist'), {recursive:true});
 fs.writeFileSync(path.join(root, '../dist/sales-items-template.xlsx'), Buffer.from(buffer));
 console.log('PASS formatted Excel template round trip; instruction sheet excluded');
 const sheet = workbook.getWorksheet('销售项');
 sheet.getCell('C2').value = '预约、审批\n支持"微信",企业集成';
 sheet.getCell('D2').value = null;
 const updated = await parseSalesExcel(await encode(workbook));
 assert.equal(updated[0].description, '预约、审批\n支持"微信",企业集成');
 assert.equal(updated[0].categories.length, 0);
 console.log('PASS multiline capability and empty optional fields');
 sheet.getCell('C2').value = {formula:'1+1',result:2};
 await assert.rejects(async()=>parseSalesExcel(await encode(workbook)), /包含公式/);
 console.log('PASS formula cells rejected');
 const bad = Uint8Array.from([1,2,3]).buffer;
 await assert.rejects(()=>parseSalesExcel(bad), /无法读取 Excel/);
 console.log('PASS invalid workbook explains supported format');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
