import { api, ApiError, type Schema } from '../../api/client'
import {
  UnsupportedCapabilityError,
  type AdminService,
  type AssignmentsService,
  type AssistantService,
  type GradingService,
  type HuipiServices,
  type InsightsService,
  type KnowledgeService,
  type MeService,
  type PageQuery,
  type ReviewsService,
  type SubmissionsService,
  type SystemService,
  type TeacherRecord,
  type WorkbenchDigest,
} from '../types'

/**
 * 真实 FastAPI Provider。
 *
 * 只调用 OpenAPI 生成合同中存在的端点；后端尚未提供的聚合能力
 * 抛出 UnsupportedCapabilityError，由页面渲染“待后端支持”的诚实状态，
 * 绝不返回伪造数据，也不在接口失败时回退到演示数据。
 */

const unsupported = (capability: string, summary: string): never => {
  throw new UnsupportedCapabilityError(capability, summary)
}

const me: MeService = {
  current: () => api.me(),
}

const assignments: AssignmentsService = {
  async list(query) {
    // 后端列表接口暂无关键词/状态查询参数：有关键条件时拉取最近 100 条并在客户端过滤。
    if (query.keyword || (query.status && query.status !== 'all')) {
      const rows = await api.assignments(0)
      const keyword = query.keyword?.trim()
      const filtered = rows.filter((row) => {
        const hit =
          !keyword ||
          row.title.includes(keyword) ||
          row.subject.includes(keyword) ||
          row.grade_level.includes(keyword)
        return hit && (!query.status || query.status === 'all' || row.status === query.status)
      })
      return { records: filtered, has_next: false }
    }
    const limit = query.limit ?? 12
    const offset = query.offset ?? 0
    const records = await api.assignments(offset)
    return { records, has_next: records.length >= limit }
  },
  create: (body) => api.create(body),
  detail: (aid) => api.assignmentDetail(aid),
  workbench: (aid, offset = 0, questionId) => api.workbench(aid, offset, undefined, questionId),
  publish: (aid) => api.publishAssignment(aid),
  addQuestion: (aid, body) => api.addQuestion(aid, body),
  updateQuestion: () => unsupported('question_update', '题目正文修改'),
  replaceAnswerKey: (qid, content) => api.replaceAnswerKey(qid, { answer_content: content }),
  replaceRubric: (qid, body) => api.replaceRubric(qid, body),
  async publishCheck(aid) {
    const detail = await api.assignmentDetail(aid)
    const items = detail.questions.map((question) => {
      const issues: string[] = []
      if (!question.rubric_criteria.length) issues.push('未配置评分细则')
      if (!question.answer_key?.answer_content) issues.push('未设置标准答案')
      return {
        question_id: question.id,
        question_number: question.question_number,
        ok: issues.length === 0,
        issues,
      }
    })
    return { ready: items.every((item) => item.ok), items }
  },
  removeAssignment: () => unsupported('assignment_delete', '删除作业'),
}

const submissions: SubmissionsService = {
  async list(aid, query: PageQuery) {
    const limit = query.limit ?? 12
    const offset = query.offset ?? 0
    const records = await api.submissions(aid, offset)
    return { records, has_next: records.length >= limit }
  },
  async create(aid, input) {
    const submission = await api.createSubmission(aid, input)
    return { created: true, submission }
  },
  detail: (sid) => api.submission(sid),
  parsingStatus: (sid) => api.parsing(sid),
  canonicalStatus: (sid) => api.canonical(sid),
  page: (sid, pageNumber) => api.page(sid, pageNumber),
  async alignment(sid) {
    try {
      return await api.alignment(sid)
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) return null
      throw error
    }
  },
  questionAlignment: (sid, qid) => api.questionAlignment(sid, qid),
  file: (sid) => api.file(sid),
}

/** 工作台摘要：由最近更新作业的分页提交组合而成（非全局聚合），口径必须如实展示。 */
async function composeDigest(): Promise<WorkbenchDigest> {
  const assignmentsRows = await api.assignments(0)
  const published = assignmentsRows.filter((row) => row.status === 'published')
  const recent = [...published].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 5)
  const merged: { submission: Schema['SubmissionRead']; assignment_title: string }[] = []
  let parsingActive = 0
  let parseAlerts = 0
  const now = Date.now()
  let recentCount = 0
  for (const assignment of recent) {
    const rows = await api.submissions(assignment.id, 0)
    for (const submission of rows) {
      merged.push({ submission, assignment_title: assignment.title })
      const status = submission.parsing_task.status
      if (status === 'pending' || status === 'running' || status === 'retry_wait') parsingActive++
      if (status === 'failed') parseAlerts++
      if (now - new Date(submission.submitted_at).getTime() <= 7 * 24 * 3600 * 1000) recentCount++
    }
  }
  merged.sort((a, b) => b.submission.submitted_at.localeCompare(a.submission.submitted_at))
  return {
    assignments_total: assignmentsRows.length,
    assignments_published: assignmentsRows.filter((row) => row.status === 'published').length,
    submissions_recent: recentCount,
    parsing_active: parsingActive,
    parse_alerts: parseAlerts,
    scope_note:
      '真实模式：最新提交与解析统计来自最近更新的 5 个已发布作业（组合查询），不是全局列表；待办任务与教学动态需要后端聚合接口，暂不展示。',
    tasks: [],
    recent_submissions: merged.slice(0, 10),
    recent_ops: [],
  }
}

const reviews: ReviewsService = {
  inspect: (sid, qid, evidenceOffset = 0, confirmedOffset = 0) =>
    api.review(sid, qid, evidenceOffset, undefined, confirmedOffset),
  history: (sid, qid, offset = 0) => api.history(sid, qid, offset),
  save: (sid, qid, body) => api.save(sid, qid, body),
  progress: () => unsupported('review_progress', '逐题人工核对进度汇总'),
  digest: () => composeDigest(),
}

const grading: GradingService = {
  list: () => unsupported('grade_proposal_list', 'AI 候选评分列表'),
  inspect: (sid, qid, pid, evidenceOffset = 0) => api.gradeInspect(sid, qid, pid, evidenceOffset),
  history: (sid, qid, pid, offset = 0) => api.gradeHistory(sid, qid, pid, offset),
  save: (sid, qid, pid, body) => api.gradeSave(sid, qid, pid, body),
}

const admin: AdminService = {
  async teachers(query) {
    const page = await api.teacherPage(100, 0)
    let records: TeacherRecord[] = page.records
    const keyword = query.keyword?.trim()
    if (keyword)
      records = records.filter((row) => row.user_id.toLowerCase().includes(keyword.toLowerCase()))
    if (query.active === 'active') records = records.filter((row) => row.is_active)
    if (query.active === 'inactive') records = records.filter((row) => !row.is_active)
    return { records, has_next: page.has_next, total: page.records.length }
  },
  register: (body) => api.registerTeacher(body),
  setActive: (uid, body) => api.setTeacherActive(uid, body),
  async memberships(uid, offset = 0) {
    const page = await api.teacherMemberships(uid, offset)
    return { records: page.records, has_next: page.has_next }
  },
  grant: (aid, uid, body) => api.grantMembership(aid, uid, body),
  revoke: (aid, uid, requestId) => api.revokeMembership(aid, uid, requestId),
  async audit(query) {
    const page = await api.auditEvents({
      target_user_id: query.target_user_id,
      assignment_id: query.assignment_id,
      request_id: query.request_id,
      limit: query.limit ?? 25,
      offset: query.offset ?? 0,
    })
    return { records: page.records, has_next: page.has_next }
  },
  async overview() {
    const [teacherPage, auditPage] = await Promise.all([
      api.teacherPage(100, 0),
      api.auditEvents({ limit: 5, offset: 0 }),
    ])
    const modules = await system.modules()
    return {
      teachers_total: teacherPage.records.length,
      teachers_active: teacherPage.records.filter((row) => row.is_active).length,
      authorizations_active: null,
      scope_note:
        '真实模式：账号数量来自最近一页教师列表（每页最多 100 条）；授权总数需逐账号查询，后端暂未提供汇总。',
      recent_audit: auditPage.records,
      modules,
    }
  },
  async assignableAssignments() {
    return api.assignments(0)
  },
}

const knowledge: KnowledgeService = {
  list: () => unsupported('knowledge_list', '教学知识库列表'),
  detail: () => unsupported('knowledge_detail', '知识库资料详情'),
  upload: () => unsupported('knowledge_upload', '知识库资料上传'),
  remove: () => unsupported('knowledge_remove', '知识库资料删除'),
  retrieve: () => unsupported('knowledge_retrieval', '知识库检索测试'),
}

const assistant: AssistantService = {
  conversations: () => unsupported('assistant_conversations', 'AI 助教会话'),
  messages: () => unsupported('assistant_messages', 'AI 助教消息'),
  send: () => unsupported('assistant_chat', 'AI 助教问答'),
  newConversation: () => unsupported('assistant_chat', 'AI 助教问答'),
  removeConversation: () => unsupported('assistant_chat', 'AI 助教问答'),
}

const insights: InsightsService = {
  summary: () => unsupported('insights_summary', '教学统计汇总'),
}

const system: SystemService = {
  async modules() {
    let apiOk = true
    try {
      await api.health()
    } catch {
      apiOk = false
    }
    return [
      {
        key: 'api',
        name: '应用服务',
        status: apiOk ? 'ok' : 'degraded',
        detail: apiOk ? '健康检查正常' : '健康检查失败，请检查部署',
      },
      {
        key: 'identity',
        name: '身份服务',
        status: 'not_integrated',
        detail: '管理端状态接口尚未提供',
      },
      {
        key: 'storage',
        name: '对象存储',
        status: 'not_integrated',
        detail: '管理端状态接口尚未提供',
      },
      { key: 'ai', name: 'AI 服务', status: 'not_integrated', detail: '管理端状态接口尚未提供' },
      {
        key: 'parsing',
        name: '文档解析',
        status: 'not_integrated',
        detail: '管理端状态接口尚未提供',
      },
    ]
  },
  health: () => api.health(),
}

export function createRealServices(): HuipiServices {
  return {
    me,
    assignments,
    submissions,
    reviews,
    grading,
    admin,
    knowledge,
    assistant,
    insights,
    system,
  }
}
