/* Deterministic prototype fixtures only. No network, source files, or database access. */
const capabilities = [
 {name:'访客管理',category:'人员通行 / 访客应用',product:'智慧园区管理平台',description:'提供访客预约、审批、登记、签离、通行授权和记录查询，支持微信公众号、企业微信集成。',demand:'建设访客管理软件，提供线上预约、审批、访客登记及通行权限下发功能，与现有门禁设备对接。',title:'园区访客与人员通行平台建设'},
 {name:'视频监控',category:'视频应用 / 视频监控',product:'综合安防管理平台',description:'提供视频实时预览、录像检索回放、设备接入管理与视频巡查能力。',demand:'采购视频集中管理软件，整合现有摄像机，实现实时预览、录像检索回放和远程视频巡查。',title:'校园视频监控平台升级'},
 {name:'动态称重车道数',category:'车辆管控 / 非现场治超',product:'交通行政综合执法平台',description:'对接公路主线动态称重子系统，实现不停车称重数据接入和管理，按车道数配置。',demand:'建设非现场治超检测点，部署动态称重系统及管理软件，实现不停车称重数据接入和省级治超平台对接。',title:'公路非现场治超检测点建设'},
 {name:'设备网络管理',category:'视频应用 / 设备管理',product:'综合安防管理平台',description:'提供设备在线状态监测、故障告警、网络诊断与设备运行统计能力。',demand:'建设设备集中运维管理平台，提供在线状态监测、故障告警、网络诊断与设备运行统计。',title:'教学设备集中运维平台采购'},
 {name:'停车场管理',category:'车辆管控 / 停车管理',product:'智慧园区管理平台',description:'提供车辆出入管理、停车计费、余位统计与车位引导能力。',demand:'采购停车管理软件，统一管理园区车辆出入、停车计费及余位统计，并对接停车场设备。',title:'园区停车管理系统建设'},
];
const dateOptions = ['2026-11-17','2026-11-16','2026-11-15','2026-11-14','2026-11-13','2026-11-12','2026-11-11'];
const regions=['郑州市','武汉市','杭州市','南京市','成都市','宁波市','合肥市','长沙市'];
const allDays = dateOptions.map((date,day)=>{
 const size=day===1?6100:day===0?3260:5800+day*43;
 const extra=day===1?1900:Math.floor(size*.3);
 return {date,rows:Array.from({length:size},(_,i)=>{
  const role=i%61;
  const cap=capabilities[i%capabilities.length];
  let demand=role<32?'yes':role<38?'pending':role<47?'no':'out';
  let status=demand==='yes'?(i%100<15?'matched':i%100<87?'no_match':i%100<97?'pending':i%100<99?'running':'failed'):demand==='pending'?'awaiting':'skipped';
  const hardware=status==='no_match';
  const key=`demo-${date}-${String(i+1).padStart(5,'0')}`;
  const title=demand==='out'?`${regions[i%8]}办公用品采购结果公告`:demand==='no'?`${regions[i%8]}采购事项说明通知`:hardware?`${regions[i%8]}车辆继电器备件采购`:`${regions[i%8]}${cap.title}`;
  return {key,date,title:`${title}（${String(i+1).padStart(4,'0')}）`,notices:1+(i<extra?1:0),isNew:i%61<51,
   demand,status,channel:demand==='out'?'结果公告':i%5===0?'采购意向':i%11===0?'更正公告':'采购公告',cap,
   region:regions[i%8],score:.86+(i%11)/100,publish:date+' 08:'+String(i%60).padStart(2,'0'),
   content:hardware?'采购电客车继电器备件，包括包装、运输和售后服务，不涉及软件系统建设。':demand==='yes'?cap.demand:'',
   enabled:true};
 })};
});
const slices={all:'全部涉及项目',new:'新增项目',existing:'已有项目更新',eligible:'关注阶段',out:'阶段不纳入',demand:'有采购需求',unextracted:'待提取需求',no_demand:'无采购需求',analyzed:'已分析',matched:'已命中',no_match:'未命中',pending:'待分析',running:'分析中',failed:'分析失败'};
function matchesSlice(p,s){return s==='all'||s==='new'&&p.isNew||s==='existing'&&!p.isNew||s==='eligible'&&p.demand!=='out'||s==='out'&&p.demand==='out'||s==='demand'&&p.demand==='yes'||s==='unextracted'&&p.demand==='pending'||s==='no_demand'&&p.demand==='no'||s==='analyzed'&&['matched','no_match'].includes(p.status)||p.status===s;}
function summarize(rows){const m={notices:rows.reduce((s,p)=>s+p.notices,0)};Object.keys(slices).forEach(k=>m[k]=rows.filter(p=>matchesSlice(p,k)).length);return m;}
const statusNames={matched:'已完成',no_match:'已完成',pending:'待分析',running:'分析中',failed:'失败',awaiting:'等待需求提取',skipped:'无需分析'};
const demandNames={yes:'有采购需求',no:'无采购需求',pending:'待提取',out:'阶段不纳入'};
