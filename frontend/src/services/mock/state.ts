import type { S } from '../types'
import type { AssistantMessage } from '../types'
import { DEMO_STATE_KEY } from '../../app/demo-auth'
import { seedDemoState } from './seed'

/**
 * 演示模式的共享业务状态。
 *
 * - 持久化策略：localStorage（键 huipi.demo.state.v1）。刷新后保留演示期间的
 *   全部修改；「重置演示环境」会清空并重新生成种子数据。
 * - 所有 Mock 服务都读写同一份状态，作业/题目/提交/复核/授权/审计之间的关系保持一致。
 */

export interface DemoUser {
  id: string
  name: string
  role: 'teacher' | 'admin'
  is_active: boolean
  subject: string
  created_at: string
}

export interface DemoAssignment {
  id: string
  owner_id: string
  title: string
  subject: string
  grade_level: string
  description: string | null
  status: 'draft' | 'published'
  created_at: string
  updated_at: string
  question_ids: string[]
}

export interface DemoQuestion {
  id: string
  assignment_id: string
  question_number: number
  question_type: S['QuestionType']
  stem: string
  max_score: string
  created_at: string
  updated_at: string
}

export interface DemoRubricCriterion {
  id: string
  question_id: string
  description: string
  points: string
  sort_order: number
}

export interface DemoAnswerKey {
  content: string
  updated_at: string
}

export interface DemoSubmission {
  id: string
  assignment_id: string
  student_ref: string
  file_name: string
  file_type: string
  file_size: number
  submitted_at: string
  created_at: string
  updated_at: string
  parsing: {
    task_id: string
    status: 'pending' | 'running' | 'retry_wait' | 'succeeded' | 'failed'
    attempt_count: number
    max_attempts: number
    started_at: string | null
    finished_at: string | null
    next_run_at: string | null
    last_error_code: string | null
    last_error_message: string | null
  }
}

export interface DemoAlignedQuestion {
  matching_status: 'aligned' | 'review_required' | 'not_observed'
  /** 关联的来源节点指针（题干 + 作答）。 */
  evidence_pointers: {
    pointer: string
    page_number: number
    text_start: number | null
    text_end: number | null
  }[]
}

export interface DemoCanonical {
  document_id: string
  source_artifact_id: string
  canonical_sha256: string
  aligner_version: string
  alignment_artifact_id: string
  alignment_sha256: string
  questions_digest: string
  page_count: number
  block_count: number
  pages: S['CanonicalPageRead'][]
  aligned: Record<string, DemoAlignedQuestion>
  /** 指针 → 节点元数据（供证据摘要与页面定位）。 */
  pointers: Record<
    string,
    { page_number: number; block_id: string; normalized_type: string; text: string | null }
  >
}

export interface DemoReviewRecord {
  source: S['ReviewSourceVersion']
  history: S['RevisionSummary'][]
  /** decision_id → 该次决定的证据区域（用于 confirmed_regions 投影）。 */
  regions: Record<string, S['ReviewRegionSelection'][]>
}

export interface DemoProposalCriterion {
  criterion_id: string
  proposed_points: string
  rationale: string
}

export interface DemoProposal {
  id: string
  submission_id: string
  question_id: string
  grading_status: 'completed_candidate' | 'grading_abstained'
  source_status: 'current' | 'source_stale' | 'unavailable'
  proposed_total_score: string | null
  max_score: string | null
  criteria: DemoProposalCriterion[]
  /** 每个评分项对应的原文证据坐标（指向本题 Canonical 节点）。 */
  evidence: {
    criterion_id: string
    content_pointer: string
    page_number: number
    text_start: number
    text_end: number
    quote: string
  }[]
  abstain_reason: string | null
  created_at: string
  reviews: S['GradeRevisionSummary'][]
}

export interface DemoMembership {
  assignment_id: string
  user_id: string
  can_write: boolean
  is_active: boolean
  created_at: string
  revoked_at: string | null
}

export interface DemoKnowledgeDoc {
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
  chunks: { id: string; text: string; page_hint: string }[]
}

export interface DemoConversation {
  id: string
  title: string
  updated_at: string
  messages: AssistantMessage[]
}

export interface DemoOpsLog {
  id: string
  summary: string
  detail: string
  at: string
}

export interface DemoState {
  version: number
  seeded_at: string
  users: DemoUser[]
  assignments: DemoAssignment[]
  questions: Record<string, DemoQuestion>
  rubrics: Record<string, DemoRubricCriterion[]>
  answerKeys: Record<string, DemoAnswerKey>
  submissions: DemoSubmission[]
  canonicals: Record<string, DemoCanonical>
  reviews: Record<string, DemoReviewRecord>
  proposals: DemoProposal[]
  memberships: DemoMembership[]
  audit: S['AdminAuditSummary'][]
  knowledge: DemoKnowledgeDoc[]
  conversations: DemoConversation[]
  ops: DemoOpsLog[]
  counters: Record<string, number>
  /** request_id → 已提交结果（幂等重放）。 */
  requestCache: Record<string, unknown>
}

const STORAGE_KEY = DEMO_STATE_KEY
const STATE_VERSION = 1

let state: DemoState | null = null

export function demoState(): DemoState {
  if (state) return state
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as DemoState
      if (parsed.version === STATE_VERSION) {
        state = parsed
        return state
      }
    }
  } catch {
    /* 本地数据损坏时直接重建演示环境 */
  }
  state = seedDemoState()
  persist()
  return state
}

export function persist(): void {
  if (!state) return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {
    /* 存储配额不足时演示修改仅保留在当前会话内存中 */
  }
}

export function resetDemoState(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* 忽略 */
  }
  state = null
}

export function nextId(prefix: string): string {
  const current = demoState()
  current.counters[prefix] = (current.counters[prefix] || 0) + 1
  return `${prefix}-${current.counters[prefix]}`
}

/** 追加教师端动态（工作台“最近教学操作”）。 */
export function logOp(summary: string, detail: string): void {
  const current = demoState()
  current.ops.unshift({ id: nextId('op'), summary, detail, at: new Date().toISOString() })
  current.ops = current.ops.slice(0, 30)
}

/** 追加管理审计事件。 */
export function logAudit(input: {
  action: S['AdminAction']
  actor_user_id: string
  target_user_id: string
  assignment_id?: string | null
  state?: Record<string, boolean>
}): S['AdminAuditSummary'] {
  const current = demoState()
  const event: S['AdminAuditSummary'] = {
    operation_id: nextId('opid'),
    request_id: nextId('req'),
    action: input.action,
    target_user_id: input.target_user_id,
    assignment_id: input.assignment_id ?? null,
    state: input.state ?? {},
    created_at: new Date().toISOString(),
    actor_user_id: input.actor_user_id,
  }
  current.audit.unshift(event)
  current.audit = current.audit.slice(0, 200)
  return event
}

/** 校验 request_id 幂等：重复提交返回原结果，不同内容返回 409。 */
export function idempotent<T>(requestId: string, content: unknown, run: () => T): T {
  const current = demoState()
  const cached = current.requestCache[requestId] as { content: unknown; result: T } | undefined
  if (cached) {
    if (JSON.stringify(cached.content) !== JSON.stringify(content)) {
      const error = new Error('同一请求标识已被不同内容使用，请刷新后重试。')
      Object.assign(error, { status: 409 })
      throw error
    }
    return cached.result
  }
  const result = run()
  current.requestCache[requestId] = { content, result }
  return result
}

export function demoError(status: number, message: string): Error {
  const error = new Error(message)
  Object.assign(error, { status })
  return error
}
