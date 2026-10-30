export type Dimension = 'business' | 'product';
export type TagStatus = 'confirmed' | 'candidate' | 'excluded';
export interface Evidence {source: string; text: string; term?: string; start?: number; end?: number; notice_id?: number; notice_title?: string; notice_type?: string; section_role?: 'demand' | 'process' | 'unknown'}
export interface Tag {dimension: Dimension; category: string; label: string; status: TagStatus; source: string; reason: string; evidence: Evidence[]}
export interface Notice {notice_id: number; title: string; buyer: string; notice_type: string; project_key?: string; publish_at?: string | null; updated_at: number | string; analysis_status: string; material_complete: boolean; tags: Tag[]}
export interface NoticeMaterial {notice_id: number; title: string; buyer?: string; body?: string; attachments?: {att_id: number; filename: string; text: string}[]}
export interface NoticeDetail extends Notice {notice: NoticeMaterial; rule_tags: Tag[]; model_tags: Tag[]; manual_tags: Tag[]; warnings: string[]; history: Record<string, unknown>[]; project_analysis?: ProjectAnalysis[]}
export interface Page<T> {items: T[]; total: number; page?: number; page_size?: number}
export interface Scheduler {paused: boolean; model_paused: boolean; llm_enabled: boolean; llm_rpm: number}
export interface SyncState {cursor?: number | string; error?: string; enabled?: boolean; updated_at?: number | string}
export interface Overview {total_notices: number; tagged_notices: number; model_confirmed: number; model_reviewed?: number; needs_review: number; material_incomplete: number; project_total?: number; project_analyzed?: number; project_pending_material?: number; counts_by_status: Record<string, number>; business_distribution: {name: string; count: number}[]; product_distribution: {name: string; count: number}[]; daily: {date: number | string; count: number}[]; metrics: Record<string, unknown>; scheduler: Scheduler; knowledge_id?: string; sync?: Record<string, SyncState>}
export interface Project {project_key: string; title?: string; notice_count: number; tags: Tag[]; updated_at: number | string; current_stage?: string; analysis_status?: string; primary_notice_ids?: number[]; analysis_count?: number}
export interface ProjectAnalysis {plan_id: string; unit_key: string; primary_notice_id: number | null; primary_title: string; primary_type: string; correction_notice_ids: number[]; ignored_correction_ids: number[]; selection_reason: string; analysis_status: string; material_complete: boolean; warnings: string[]; round_key?: string | null; lot_key?: string | null; revision: string; knowledge_id: string; tags: Tag[]; updated_at: number | string}
export interface ProjectDetail {project_key: string; notices: Notice[]; tags: Tag[]; analyses: ProjectAnalysis[]; current_stage?: string}
export interface Task {id: string; notice_id: number; kind: string; status: string; priority: number; attempts: number; error?: string; created_at: number | string; updated_at: number | string}
export interface BusinessLine {business_domain: string; business_line: string; body_md: string}
export interface BusinessKeyword {business_line: string; word: string; kind: string; term_family?: string | null; enabled: boolean}
export interface ProductKeyword {category: string; word: string; strength: 'strong' | 'weak'; enabled: boolean; exclude_patterns: string[]}
export interface Knowledge {version: string; lines: BusinessLine[]; keywords: BusinessKeyword[]; product_keywords: ProductKeyword[]; quality: 'full' | 'keywords_only'; source_rubric?: Record<string, unknown>; warnings?: string[]}
