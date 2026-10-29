export type Dimension = 'business' | 'product';
export type TagStatus = 'confirmed' | 'candidate' | 'excluded';
export interface Evidence {source: string; text: string; term?: string; start?: number; end?: number}
export interface Tag {dimension: Dimension; category: string; label: string; status: TagStatus; source: string; reason: string; evidence: Evidence[]}
export interface Notice {notice_id: number; title: string; buyer: string; notice_type: string; project_key?: string; publish_at?: string | null; updated_at: number | string; analysis_status: string; material_complete: boolean; tags: Tag[]}
export interface NoticeMaterial {notice_id: number; title: string; buyer?: string; body?: string; attachments?: {att_id: number; filename: string; text: string}[]}
export interface NoticeDetail extends Notice {notice: NoticeMaterial; rule_tags: Tag[]; model_tags: Tag[]; manual_tags: Tag[]; warnings: string[]; history: Record<string, unknown>[]}
export interface Page<T> {items: T[]; total: number; page?: number; page_size?: number}
export interface Scheduler {paused: boolean; model_paused: boolean; llm_enabled: boolean; llm_rpm: number}
export interface SyncState {cursor?: number | string; error?: string; enabled?: boolean; updated_at?: number | string}
export interface Overview {total_notices: number; tagged_notices: number; model_confirmed: number; model_reviewed?: number; needs_review: number; material_incomplete: number; counts_by_status: Record<string, number>; business_distribution: {name: string; count: number}[]; product_distribution: {name: string; count: number}[]; daily: {date: number | string; count: number}[]; metrics: Record<string, unknown>; scheduler: Scheduler; knowledge_id?: string; sync?: Record<string, SyncState>}
export interface Project {project_key: string; title?: string; notice_count: number; tags: Tag[]; updated_at: number | string}
export interface Task {id: string; notice_id: number; kind: string; status: string; priority: number; attempts: number; error?: string; created_at: number | string; updated_at: number | string}
export interface BusinessLine {business_domain: string; business_line: string; body_md: string}
export interface BusinessKeyword {business_line: string; word: string; kind: string; term_family?: string | null; enabled: boolean}
export interface ProductKeyword {category: string; word: string; strength: 'strong' | 'weak'; enabled: boolean; exclude_patterns: string[]}
export interface Knowledge {version: string; lines: BusinessLine[]; keywords: BusinessKeyword[]; product_keywords: ProductKeyword[]; quality: 'full' | 'keywords_only'; source_rubric?: Record<string, unknown>; warnings?: string[]}
