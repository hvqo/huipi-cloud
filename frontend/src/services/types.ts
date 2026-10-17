/**
 * 业务数据访问层合同：UI 页面 → 业务 Service → Data Provider。
 *
 * 两种 Provider 实现：
 * - real：真实 FastAPI（src/services/real），仅调用 OpenAPI 生成类型中存在的接口；
 * - mock：本地合成数据服务（src/services/mock），仅用于 `npm run dev:demo` 演示模式。
 *
 * 后端尚不存在的聚合能力以 `UnsupportedCapabilityError` 显式表达，
 * 页面据此渲染“待后端支持”的诚实状态，绝不伪造数据。
 */
import type { Schema } from '../api/client'

export type S = Schema

/** 某项能力在当前数据源（真实后端）中不可用。 */
export class UnsupportedCapabilityError extends Error {
  constructor(
    public capability: string,
    public summary: string,
  ) {
    super(`后端暂未提供「${summary}」所需接口（${capability}）。`)
    this.name = 'UnsupportedCapabilityError'
  }
}

export interface PageQuery {
  limit?: number
  offset?: number
}

export interface AssignmentListQuery extends PageQuery {
  keyword?: string
  status?: 'draft' | 'published' | 'all'
}

/* ---------- 缺口 DTO（前端先行定义，待后端实现后对齐） ---------- */

/** 一次提交内逐题的人工核对进度。后端缺口：建议新增聚合端点。 */
export interface QuestionReviewProgress {
  question_id: string
  question_number: number
  answer_presence: 'unreviewed' | 'response_present' | 'response_absent' | 'uncertain'
  latest_revision: number
  latest_source_matches: boolean
}

export interface SubmissionReviewProgress {
  submission_id: string
  assignment_id: string
  student_ref: string
  questions: QuestionReviewProgress[]
}

/** AI 候选评分列表项。后端缺口：建议新增 proposal 列表端点。 */
export interface GradeProposalSummary {
  proposal_id: string
  question_id: string
  question_number: number
  grading_status: 'completed_candidate' | 'grading_abstained'
  source_status: 'current' | 'source_stale' | 'unavailable'
  proposed_total_score: string | null
  max_score: string | null
  latest_review_status: 'accepted' | 'revised' | 'rejected' | 'regrading_required' | null
  latest_review_revision: number
  has_review_history: boolean
  created_at: string
}

export interface WorkbenchTask {
  kind: 'review' | 'grading' | 'parse_alert'
  title: string
  description: string
  href: string
  assignment_title: string
  student_ref?: string
  created_at: string
}

export interface WorkbenchDigest {
  assignments_total: number
  assignments_published: number
  submissions_recent: number
  parsing_active: number
  parse_alerts: number
  /** 统计口径说明；演示模式与真实模式的口径不同，必须如实展示。 */
  scope_note: string
  tasks: WorkbenchTask[]
  recent_submissions: {
    submission: S['SubmissionRead']
    assignment_title: string
  }[]
  recent_ops: { id: string; summary: string; detail: string; at: string }[]
}

/** 教学知识库。后端缺口：HTTP 管理端点尚未开放（现为受控服务操作）。 */
export interface KnowledgeDocSummary {
  id: string
  title: string
  category: string
  file_type: string
  size_bytes: number
  status: 'pending' | 'indexing' | 'indexed' | 'failed'
  chunk_count: number
  version: number
  assignment_id: string | null
  created_at: string
}

export interface KnowledgeDocDetail extends KnowledgeDocSummary {
  chunks: { id: string; text: string; page_hint: string }[]
}

export interface RetrievalHit {
  chunk_id: string
  doc_id: string
  doc_title: string
  score: number
  text: string
}

export interface AssistantMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
  toolCalls?: { name: string; status: 'running' | 'done' | 'failed'; summary: string }[]
  citations?: {
    index: number
    doc_id: string
    doc_title: string
    chunk_id: string
    quote: string
  }[]
}

export interface AssistantConversation {
  id: string
  title: string
  updated_at: string
  message_count: number
}

export interface InsightsSummary {
  range_label: string
  scope_note: string
  assignments_total: number
  submissions_total: number
  submissions_recent: number
  parse_succeeded: number
  parse_failed: number
  parse_pending: number
  review_done: number
  review_pending: number
  grading_reviewed: number
  grading_pending: number
  series: { date: string; submissions: number; reviews: number }[]
  workload: { assignment_title: string; submissions: number; reviewed: number; pending: number }[]
}

export interface SystemModuleStatus {
  key: string
  name: string
  status: 'ok' | 'degraded' | 'unknown' | 'not_integrated'
  detail: string
}

/** 教师账号记录：真实模式仅有合同字段（内部账号标识等）；演示模式附加合成 display_name。 */
export type TeacherRecord = S['AdminUserSummary'] & { display_name?: string | null }

export interface AdminOverview {
  teachers_total: number
  teachers_active: number
  /** 真实模式暂无按账号汇总的授权统计（后端缺口），为 null。 */
  authorizations_active: number | null
  scope_note: string
  recent_audit: S['AdminAuditSummary'][]
  modules: SystemModuleStatus[]
}

/* ---------- Service 合同 ---------- */

export interface MeService {
  current(): Promise<S['CurrentUser']>
}

export interface AssignmentsService {
  list(
    query: AssignmentListQuery,
  ): Promise<{ records: S['AssignmentSummary'][]; has_next: boolean }>
  create(body: S['AssignmentCreate']): Promise<S['AssignmentDetail']>
  detail(aid: string): Promise<S['AssignmentDetail']>
  workbench(aid: string, offset?: number, questionId?: string): Promise<S['AssignmentWorkbench']>
  publish(aid: string): Promise<S['AssignmentDetail']>
  addQuestion(aid: string, body: S['QuestionCreate']): Promise<S['QuestionRead']>
  /** 真实后端尚无题目正文更新端点（仅新增/答案/细则），仅演示模式可编辑正文。 */
  updateQuestion(aid: string, qid: string, body: QuestionEditable): Promise<S['QuestionRead']>
  replaceAnswerKey(qid: string, content: string): Promise<S['AnswerKeyRead']>
  replaceRubric(qid: string, body: S['RubricReplace']): Promise<S['RubricRead']>
  publishCheck(aid: string): Promise<PublishCheckResult>
  removeAssignment(aid: string): Promise<void>
}

/** 题目正文中演示模式可编辑的字段；真实模式后端缺口见 updateQuestion。 */
export interface QuestionEditable {
  question_number: number
  question_type: S['QuestionType']
  stem: string
  max_score: string
}

export interface PublishCheckItem {
  question_id: string
  question_number: number
  ok: boolean
  issues: string[]
}

export interface PublishCheckResult {
  ready: boolean
  items: PublishCheckItem[]
}

export interface SubmissionsService {
  list(
    aid: string,
    query: PageQuery,
  ): Promise<{ records: S['SubmissionRead'][]; has_next: boolean }>
  create(
    aid: string,
    input: { student_ref: string; file: File },
  ): Promise<{ created: boolean; submission: S['SubmissionRead'] }>
  detail(sid: string, signal?: AbortSignal): Promise<S['SubmissionRead']>
  parsingStatus(sid: string, signal?: AbortSignal): Promise<S['ParsingTaskStatusRead']>
  canonicalStatus(sid: string, signal?: AbortSignal): Promise<S['CanonicalDocumentStatusRead']>
  page(sid: string, pageNumber: number, signal?: AbortSignal): Promise<S['CanonicalPageRead']>
  alignment(sid: string, signal?: AbortSignal): Promise<S['AlignmentSummaryRead'] | null>
  questionAlignment(
    sid: string,
    qid: string,
    signal?: AbortSignal,
  ): Promise<S['QuestionAlignmentRead']>
  file(sid: string, signal?: AbortSignal): Promise<Blob>
}

export interface ReviewsService {
  inspect(
    sid: string,
    qid: string,
    evidenceOffset?: number,
    confirmedOffset?: number,
    signal?: AbortSignal,
  ): Promise<S['ReviewInspection']>
  history(
    sid: string,
    qid: string,
    offset?: number,
    signal?: AbortSignal,
  ): Promise<S['ReviewHistory']>
  save(sid: string, qid: string, body: string): Promise<S['ReviewWriteResult']>
  /** 逐题核对进度聚合；真实后端缺口。 */
  progress(sid: string): Promise<SubmissionReviewProgress>
  /** 工作台摘要。真实后端缺口（首页聚合）。 */
  digest(): Promise<WorkbenchDigest>
}

export interface GradingService {
  /** 候选列表；真实后端缺口（inspect 需先知道 proposal_id）。 */
  list(sid: string, qid: string): Promise<GradeProposalSummary[]>
  inspect(
    sid: string,
    qid: string,
    pid: string,
    evidenceOffset?: number,
  ): Promise<S['GradeReviewHTTPInspection']>
  history(
    sid: string,
    qid: string,
    pid: string,
    offset?: number,
  ): Promise<S['GradeReviewHTTPHistory']>
  save(
    sid: string,
    qid: string,
    pid: string,
    body: string,
  ): Promise<S['GradeReviewHTTPWriteResult']>
}

export interface AdminService {
  teachers(
    query: PageQuery & { keyword?: string; active?: 'active' | 'inactive' | 'all' },
  ): Promise<{ records: TeacherRecord[]; has_next: boolean; total: number }>
  register(body: S['TeacherRegistration']): Promise<S['AdminOperationResult']>
  setActive(uid: string, body: S['TeacherActivation']): Promise<S['AdminOperationResult']>
  memberships(
    uid: string,
    offset?: number,
  ): Promise<{ records: S['AssignmentMembershipSummary'][]; has_next: boolean }>
  grant(
    aid: string,
    uid: string,
    body: S['MembershipGrant'] & { request_id: string },
  ): Promise<S['MembershipWriteResult']>
  revoke(aid: string, uid: string, requestId: string): Promise<void>
  audit(
    query: PageQuery & { target_user_id?: string; assignment_id?: string; request_id?: string },
  ): Promise<{ records: S['AdminAuditSummary'][]; has_next: boolean }>
  overview(): Promise<AdminOverview>
  /** 可授权的作业清单（管理员全局可见）。 */
  assignableAssignments(): Promise<S['AssignmentSummary'][]>
}

export interface KnowledgeService {
  list(): Promise<KnowledgeDocSummary[]>
  detail(id: string): Promise<KnowledgeDocDetail>
  upload(input: {
    file: File
    category: string
    assignment_id?: string | null
  }): Promise<KnowledgeDocSummary>
  remove(id: string): Promise<void>
  retrieve(query: string): Promise<RetrievalHit[]>
}

export interface AssistantService {
  conversations(): Promise<AssistantConversation[]>
  messages(conversationId: string): Promise<AssistantMessage[]>
  /** 演示模式合成回答；真实后端缺口，绝不伪造 AI 回答。 */
  send(conversationId: string, content: string, signal?: AbortSignal): Promise<AssistantMessage>
  newConversation(): Promise<AssistantConversation>
  removeConversation(id: string): Promise<void>
}

export interface InsightsService {
  summary(range: '7d' | '30d' | 'all'): Promise<InsightsSummary>
}

export interface SystemService {
  modules(): Promise<SystemModuleStatus[]>
  health(): Promise<{ status: string }>
}

export interface HuipiServices {
  me: MeService
  assignments: AssignmentsService
  submissions: SubmissionsService
  reviews: ReviewsService
  grading: GradingService
  admin: AdminService
  knowledge: KnowledgeService
  assistant: AssistantService
  insights: InsightsService
  system: SystemService
}
