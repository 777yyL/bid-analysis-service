export interface ProjectNotice {notice_id:number; title:string; channel?:string; notice_type?:string}
export interface ProjectMaterial {main_notice_id?:number; main_notice_ids?:number[]; correction_notice_ids?:number[]; notices?:ProjectNotice[]}
export function primaryNoticeIds(project?:ProjectMaterial):number[] {
 return [...new Set(project?.main_notice_ids?.length ? project.main_notice_ids : project?.main_notice_id ? [project.main_notice_id] : [])];
}
export function materialRole(project:ProjectMaterial, noticeId:number):string {
 if (primaryNoticeIds(project).includes(noticeId)) return '采购主公告';
 if (project.correction_notice_ids?.includes(noticeId)) return '采购更正';
 return '关联公告（未选入本次材料）';
}
export function noticeLabel(project:ProjectMaterial|undefined, noticeId:number):string {
 const title = project?.notices?.find(notice=>notice.notice_id===noticeId)?.title;
 return title ? `#${noticeId} ${title}` : `#${noticeId}`;
}
