import { currentDemoRole } from '../../app/demo-auth'
import { allowedReasons, sourceMatches } from '../../lib/review'
import { addDecimals, compareDecimals, isValidDecimal, sumDecimals } from '../../lib/decimal'
import type {
  AdminService,
  AssistantMessage,
  AssistantService,
  AssignmentsService,
  GradingService,
  HuipiServices,
  InsightsService,
  KnowledgeService,
  MeService,
  PageQuery,
  ReviewsService,
  RetrievalHit,
  S,
  SubmissionsService,
  SystemService,
  TeacherRecord,
  WorkbenchTask,
} from '../types'
import { buildCanonical } from './canonical'
import { renderExamPage } from './files'
import { loadDemoFile, saveDemoFile } from './file-store'
import {
  demoError,
  demoState,
  idempotent,
  logAudit,
  logOp,
  nextId,
  persist,
  type DemoAssignment,
  type DemoCanonical,
  type DemoQuestion,
  type DemoSubmission,
} from './state'

/**
 * 演示模式数据服务：完整实现各业务 Service，读写同一份共享演示状态。
 * 合同语义与真实后端一致（request_id 幂等、expected_revision 乐观并发、
 * 409 冲突、来源校验），演示操作会真实更新状态并在刷新后保留。
 */

const CODE_POINTS = (text: string) => Array.from(text).length

function currentUser() {
  const role = currentDemoRole()
  if (!role) throw demoError(401, '请先选择演示身份。')
  const state = demoState()
  const user = state.users.find((u) =>
    role === 'admin' ? u.role === 'admin' : u.role === 'teacher',
  )
  if (!user || !user.is_active) throw demoError(403, '演示账号不存在，请重置演示环境。')
  return user
}

function isAdmin() {
  return currentUser().role === 'admin'
}

function visibleAssignments() {
  const state = demoState()
  const user = currentUser()
  if (user.role === 'admin') return state.assignments
  const allowed = new Set(
    state.memberships
      .filter((m) => m.user_id === user.id && m.is_active && !m.revoked_at)
      .map((m) => m.assignment_id),
  )
  return state.assignments.filter((a) => allowed.has(a.id))
}

function membershipOf(userId: string, aid: string) {
  return (
    demoState().memberships.find((m) => m.assignment_id === aid && m.user_id === userId) ?? null
  )
}

function canWrite(aid: string) {
  const user = currentUser()
  if (user.role === 'admin') return true
  const membership = membershipOf(user.id, aid)
  return !!membership && membership.can_write && membership.is_active && !membership.revoked_at
}

function assertVisible(aid: string) {
  const assignment = visibleAssignments().find((a) => a.id === aid)
  if (!assignment) throw demoError(404, '资源不可用或没有访问权限。')
  return assignment
}

function assertDraft(aid: string) {
  const a = assertVisible(aid)
  if (!canWrite(aid)) throw demoError(403, '当前账号对该作业没有写权限。')
  if (a.status !== 'draft') throw demoError(409, '已发布作业不能修改题目、答案和评分细则。')
  return a
}
function assertSubmission(sid: string, qid?: string) {
  const sub = demoState().submissions.find((s) => s.id === sid)
  if (!sub) throw demoError(404, '提交不存在或无权访问。')
  assertVisible(sub.assignment_id)
  if (qid && demoState().questions[qid]?.assignment_id !== sub.assignment_id)
    throw demoError(404, '题目与提交不属于同一作业。')
  return sub
}
function assignmentQuestions(aid: string) {
  const state = demoState()
  const assignment = state.assignments.find((a) => a.id === aid)
  if (!assignment) return []
  return assignment.question_ids
    .map((qid) => state.questions[qid]!)
    .filter(Boolean)
    .sort((a, b) => a.question_number - b.question_number)
}

/* ---------------- 映射器 ---------------- */

function toSummary(a: DemoAssignment): S['AssignmentSummary'] {
  return {
    id: a.id,
    title: a.title,
    subject: a.subject,
    grade_level: a.grade_level,
    description: a.description,
    status: a.status,
    created_at: a.created_at,
    updated_at: a.updated_at,
  }
}

function toQuestionRead(q: DemoQuestion): S['QuestionRead'] {
  const state = demoState()
  const key = state.answerKeys[q.id]
  return {
    id: q.id,
    assignment_id: q.assignment_id,
    question_number: q.question_number,
    question_type: q.question_type,
    stem: q.stem,
    max_score: q.max_score,
    created_at: q.created_at,
    updated_at: q.updated_at,
    answer_key: key
      ? {
          id: `ak-${q.id}`,
          question_id: q.id,
          answer_content: key.content,
          source: 'teacher',
          created_at: q.created_at,
          updated_at: key.updated_at,
        }
      : null,
    rubric_criteria: (state.rubrics[q.id] ?? [])
      .slice()
      .sort((a, b) => a.sort_order - b.sort_order)
      .map((c) => ({
        id: c.id,
        question_id: c.question_id,
        description: c.description,
        points: c.points,
        sort_order: c.sort_order,
      })),
  }
}

function toSubmissionRead(s: DemoSubmission): S['SubmissionRead'] {
  return {
    id: s.id,
    assignment_id: s.assignment_id,
    student_ref: s.student_ref,
    status: 'submitted',
    created_at: s.created_at,
    updated_at: s.updated_at,
    submitted_at: s.submitted_at,
    file: {
      id: `file-${s.id}`,
      original_filename: s.file_name,
      content_type: s.file_type,
      size_bytes: s.file_size,
      sha256: `sha-f-${s.id}`,
      created_at: s.created_at,
    },
    parsing_task: {
      id: s.parsing.task_id,
      status: s.parsing.status,
      created_at: s.created_at,
      updated_at: s.updated_at,
    },
  }
}

function sourceOf(sid: string): S['ReviewSourceVersion'] {
  const canonical = demoState().canonicals[sid]!
  return {
    canonical_document_id: canonical.document_id,
    canonical_sha256: canonical.canonical_sha256,
    alignment_artifact_id: canonical.alignment_artifact_id,
    alignment_sha256: canonical.alignment_sha256,
    aligner_version: canonical.aligner_version,
    assignment_questions_digest: canonical.questions_digest,
  }
}

/* ---------------- Me ---------------- */

const me: MeService = {
  async current() {
    const user = currentUser()
    const caps: S['Capability'][] =
      user.role === 'admin'
        ? [
            'assignment:create',
            'assignment:read_scoped',
            'assignment:write_scoped',
            'review:write_scoped',
            'assignment:access_all',
            'teacher:manage',
            'membership:manage',
            'audit:read',
          ]
        : [
            'assignment:create',
            'assignment:read_scoped',
            'assignment:write_scoped',
            'review:write_scoped',
          ]
    return {
      user_id: user.id,
      display_name: user.name,
      role: user.role,
      capabilities: caps,
      is_active: true,
    }
  },
}

/* ---------------- Assignments ---------------- */

const assignments: AssignmentsService = {
  async list(query) {
    let rows = visibleAssignments()
    const keyword = query.keyword?.trim()
    if (keyword)
      rows = rows.filter(
        (a) =>
          a.title.includes(keyword) ||
          a.subject.includes(keyword) ||
          a.grade_level.includes(keyword),
      )
    if (query.status && query.status !== 'all') rows = rows.filter((a) => a.status === query.status)
    rows = rows.slice().sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    const limit = query.limit ?? 12
    const offset = query.offset ?? 0
    const page = rows.slice(offset, offset + limit)
    return { records: page.map(toSummary), has_next: offset + limit < rows.length }
  },
  async create(body) {
    const state = demoState()
    const user = currentUser()
    const id = nextId('asg')
    const now = new Date().toISOString()
    state.assignments.push({
      id,
      owner_id: user.id,
      title: body.title,
      subject: body.subject,
      grade_level: body.grade_level,
      description: body.description ?? null,
      status: 'draft',
      created_at: now,
      updated_at: now,
      question_ids: [],
    })
    state.memberships.push({
      assignment_id: id,
      user_id: user.id,
      can_write: true,
      is_active: true,
      created_at: now,
      revoked_at: null,
    })
    logOp('创建了作业', `${body.title}（草稿）`)
    persist()
    return {
      id,
      title: body.title,
      subject: body.subject,
      grade_level: body.grade_level,
      description: body.description ?? null,
      status: 'draft',
      created_at: now,
      updated_at: now,
      questions: [],
    }
  },
  async detail(aid) {
    const assignment = assertVisible(aid)
    return {
      ...toSummary(assignment),
      questions: assignmentQuestions(aid).map(toQuestionRead),
    }
  },
  async workbench(aid, offset = 0, questionId) {
    const state = demoState()
    const assignment = assertVisible(aid)
    let questions = assignmentQuestions(aid)
    if (questionId) questions = questions.filter((q) => q.id === questionId)
    const limit = 20
    const page = questions.slice(offset, offset + limit)
    return {
      assignment: toSummary(assignment),
      can_write: canWrite(aid),
      questions: page.map((q) => {
        const criteria = (state.rubrics[q.id] ?? [])
          .slice()
          .sort((a, b) => a.sort_order - b.sort_order)
        return {
          id: q.id,
          question_number: q.question_number,
          question_type: q.question_type,
          stem: q.stem,
          max_score: q.max_score,
          rubric_criteria: criteria.slice(0, 5).map((c) => ({
            id: c.id,
            question_id: c.question_id,
            description: c.description,
            points: c.points,
            sort_order: c.sort_order,
          })),
          rubric_count: criteria.length,
          rubric_truncated: criteria.length > 5,
        }
      }),
      offset,
      limit,
      has_next: offset + limit < questions.length,
      next_offset: offset + limit < questions.length ? offset + limit : null,
    }
  },
  async publish(aid) {
    const assignment = assertVisible(aid)
    if (!canWrite(aid)) throw demoError(403, '当前账号对该作业没有写权限。')
    const check = await assignments.publishCheck(aid)
    if (!check.ready) throw demoError(409, '作业尚未满足发布条件。')
    assignment.status = 'published'
    assignment.updated_at = new Date().toISOString()
    logOp('发布了作业', assignment.title)
    persist()
    return { ...toSummary(assignment), questions: assignmentQuestions(aid).map(toQuestionRead) }
  },
  async addQuestion(aid, body) {
    const state = demoState()
    const assignment = assertVisible(aid)
    if (!canWrite(aid)) throw demoError(403, '当前账号对该作业没有写权限。')
    assertDraft(aid)
    if (
      body.question_number < 1 ||
      assignmentQuestions(aid).some((q) => q.question_number === body.question_number)
    )
      throw demoError(409, '题号必须为正整数且不可重复。')
    if (
      !isValidDecimal(String(body.max_score)) ||
      compareDecimals(String(body.max_score), '0') <= 0
    )
      throw demoError(422, '满分应为正数。')
    const id = nextId(`q-${aid.slice(4)}`)
    const now = new Date().toISOString()
    state.questions[id] = {
      id,
      assignment_id: aid,
      question_number: body.question_number,
      question_type: body.question_type,
      stem: body.stem,
      max_score: String(body.max_score),
      created_at: now,
      updated_at: now,
    }
    state.rubrics[id] = []
    assignment.question_ids.push(id)
    assignment.updated_at = now
    logOp('新增了题目', `${assignment.title} · 第 ${body.question_number} 题`)
    persist()
    return toQuestionRead(state.questions[id]!)
  },
  async updateQuestion(aid, qid, body) {
    const state = demoState()
    const assignment = assertVisible(aid)
    const question = state.questions[qid]
    if (!question || question.assignment_id !== aid)
      throw demoError(404, '题目不存在或不属于该作业。')
    if (!canWrite(aid)) throw demoError(403, '当前账号对该作业没有写权限。')
    assertDraft(aid)
    if (
      assignmentQuestions(aid).some(
        (q) => q.id !== qid && q.question_number === body.question_number,
      ) ||
      body.question_number < 1
    )
      throw demoError(409, '题号重复或不合法。')
    if (!isValidDecimal(body.max_score) || compareDecimals(body.max_score, '0') <= 0)
      throw demoError(422, '满分不合法。')
    question.question_number = body.question_number
    question.question_type = body.question_type
    question.stem = body.stem
    question.max_score = body.max_score
    question.updated_at = new Date().toISOString()
    assignment.updated_at = question.updated_at
    persist()
    return toQuestionRead(question)
  },
  async replaceAnswerKey(qid, content) {
    const state = demoState()
    const question = state.questions[qid]
    if (!question) throw demoError(404, '题目不存在。')
    assertDraft(question.assignment_id)
    if (!content.trim()) throw demoError(422, '标准答案不能为空。')
    state.answerKeys[qid] = { content, updated_at: new Date().toISOString() }
    logOp('更新了标准答案', `第 ${question.question_number} 题`)
    persist()
    return toQuestionRead(question).answer_key!
  },
  async replaceRubric(qid, body) {
    const state = demoState()
    const question = state.questions[qid]
    if (!question) throw demoError(404, '题目不存在。')
    const total = sumDecimals(body.criteria.map((c) => String(c.points)))
    if (compareDecimals(total, question.max_score) > 0)
      throw demoError(422, `评分项合计 ${total} 分超过题目满分 ${question.max_score} 分。`)
    assertDraft(question.assignment_id)
    if (
      !body.criteria.length ||
      body.criteria.some(
        (c) =>
          !c.description.trim() ||
          !isValidDecimal(String(c.points)) ||
          compareDecimals(String(c.points), '0') <= 0,
      )
    )
      throw demoError(422, '请配置合法的评分项和分值。')
    state.rubrics[qid] = body.criteria.map((c, index) => ({
      id: `crit-${qid}-${index + 1}`,
      question_id: qid,
      description: c.description,
      points: String(c.points),
      sort_order: c.sort_order,
    }))
    logOp('更新了评分细则', `第 ${question.question_number} 题`)
    persist()
    return { criteria: toQuestionRead(question).rubric_criteria }
  },
  async publishCheck(aid) {
    const state = demoState()
    assertVisible(aid)
    const items = assignmentQuestions(aid).map((q) => {
      const issues: string[] = []
      if (!(state.rubrics[q.id] ?? []).length) issues.push('未配置评分细则')
      if (!state.answerKeys[q.id]?.content) issues.push('未设置标准答案')
      if (
        sumDecimals((state.rubrics[q.id] ?? []).map((c) => c.points)) !== sumDecimals([q.max_score])
      )
        issues.push('评分项总分与题目满分不一致')
      return {
        question_id: q.id,
        question_number: q.question_number,
        ok: issues.length === 0,
        issues,
      }
    })
    return { ready: items.length > 0 && items.every((item) => item.ok), items }
  },
  async removeAssignment(aid) {
    const state = demoState()
    const assignment = assertVisible(aid)
    if (assignment.owner_id !== currentUser().id)
      throw demoError(403, '仅作业创建者可删除（演示）。')
    for (const submission of state.submissions.filter((s) => s.assignment_id === aid))
      delete state.canonicals[submission.id]
    const removedIds = new Set(
      state.submissions.filter((s) => s.assignment_id === aid).map((s) => s.id),
    )
    state.proposals = state.proposals.filter((p) => !removedIds.has(p.submission_id))
    for (const key of Object.keys(state.reviews))
      if (removedIds.has(key.split(':')[0]!)) delete state.reviews[key]
    state.submissions = state.submissions.filter((s) => s.assignment_id !== aid)
    for (const qid of assignment.question_ids) {
      delete state.questions[qid]
      delete state.rubrics[qid]
      delete state.answerKeys[qid]
    }
    state.assignments = state.assignments.filter((a) => a.id !== aid)
    state.memberships = state.memberships.filter((m) => m.assignment_id !== aid)
    logOp('删除了作业', assignment.title)
    persist()
  },
}

/* ---------------- Submissions ---------------- */

const scheduledParsing = new Set<string>()
function scheduleParseTransition(submissionId: string) {
  if (scheduledParsing.has(submissionId)) return
  scheduledParsing.add(submissionId)
  // 演示解析：约 4 秒后进入解析完成，并生成 Canonical。
  window.setTimeout(() => {
    const state = demoState()
    const submission = state.submissions.find((s) => s.id === submissionId)
    if (!submission || submission.parsing.status !== 'running') return
    submission.parsing.status = 'succeeded'
    submission.parsing.finished_at = new Date().toISOString()
    submission.updated_at = submission.parsing.finished_at
    const questions = assignmentQuestions(submission.assignment_id)
    const script = questions.map((q) => ({
      qid: q.id,
      number: q.question_number,
      stem: q.stem,
      answer: '学生作答：演示合成作答内容，过程完整、书写清晰。',
    }))
    const { canonical, answerPointers, stemPointers } = buildCanonical(submissionId, script)
    const aligned: DemoCanonical['aligned'] = {}
    for (const q of questions) {
      const answer = answerPointers[q.id]
      aligned[q.id] = {
        matching_status: 'aligned',
        evidence_pointers: [
          { pointer: stemPointers[q.id]!, page_number: 1, text_start: 0, text_end: 2 },
          ...(answer
            ? [
                {
                  pointer: answer.pointer,
                  page_number: answer.page,
                  text_start: 0,
                  text_end: CODE_POINTS(answer.text),
                },
              ]
            : []),
        ],
      }
    }
    canonical.aligned = aligned
    state.canonicals[submissionId] = canonical
    persist()
  }, 4000)
}

const submissions: SubmissionsService = {
  async list(aid, query: PageQuery) {
    assertVisible(aid)
    const rows = demoState()
      .submissions.filter((s) => s.assignment_id === aid)
      .sort((a, b) => b.submitted_at.localeCompare(a.submitted_at))
    const limit = query.limit ?? 12
    const offset = query.offset ?? 0
    const page = rows.slice(offset, offset + limit)
    return { records: page.map(toSubmissionRead), has_next: offset + limit < rows.length }
  },
  async create(aid, input) {
    const state = demoState()
    assertVisible(aid)
    if (!canWrite(aid)) throw demoError(403, '当前账号对该作业没有写权限。')
    if (assertVisible(aid).status !== 'published') throw demoError(409, '只有已发布作业可以提交。')
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(input.student_ref))
      throw demoError(422, '学生标识格式不正确。')
    if (
      state.submissions.some((s) => s.assignment_id === aid && s.student_ref === input.student_ref)
    )
      throw demoError(409, '该学生已提交过这份作业。')
    if (input.file.size > 20 * 1024 * 1024) throw demoError(413, '文件超过20MiB上限。')
    const header = new Uint8Array(await input.file.slice(0, 8).arrayBuffer())
    const ext = input.file.name.split('.').pop()?.toLowerCase()
    const valid =
      (ext === 'pdf' &&
        input.file.type === 'application/pdf' &&
        new TextDecoder().decode(header).startsWith('%PDF-')) ||
      (['jpg', 'jpeg'].includes(ext || '') &&
        input.file.type === 'image/jpeg' &&
        header[0] === 255 &&
        header[1] === 216 &&
        header[2] === 255) ||
      (ext === 'png' &&
        input.file.type === 'image/png' &&
        header[0] === 137 &&
        header[1] === 80 &&
        header[2] === 78 &&
        header[3] === 71)
    if (!valid) throw demoError(415, '文件扩展名、类型与内容不一致，仅支持PDF、JPG或PNG。')
    const id = nextId('sub')
    const now = new Date().toISOString()
    await saveDemoFile(id, input.file)
    state.submissions.push({
      id,
      assignment_id: aid,
      student_ref: input.student_ref,
      file_name: input.file.name,
      file_type: input.file.type,
      file_size: input.file.size,
      uploaded: true,
      submitted_at: now,
      created_at: now,
      updated_at: now,
      parsing: {
        task_id: `pt-${id}`,
        status: 'running',
        attempt_count: 1,
        max_attempts: 3,
        started_at: now,
        finished_at: null,
        next_run_at: null,
        last_error_code: null,
        last_error_message: null,
      },
    })
    logOp(
      '上传了学生答卷',
      `${state.assignments.find((a) => a.id === aid)!.title} · ${input.student_ref}`,
    )
    persist()
    scheduleParseTransition(id)
    return {
      created: true,
      submission: toSubmissionRead(state.submissions.find((s) => s.id === id)!),
    }
  },
  async detail(sid) {
    const submission = demoState().submissions.find((s) => s.id === sid)
    if (!submission) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(submission.assignment_id)
    return toSubmissionRead(submission)
  },
  async parsingStatus(sid) {
    assertSubmission(sid)
    const submission = demoState().submissions.find((s) => s.id === sid)
    if (!submission) throw demoError(404, '资源不可用或没有访问权限。')
    return {
      task_id: submission.parsing.task_id,
      submission_id: sid,
      status: submission.parsing.status,
      attempt_count: submission.parsing.attempt_count,
      max_attempts: submission.parsing.max_attempts,
      created_at: submission.created_at,
      started_at: submission.parsing.started_at,
      finished_at: submission.parsing.finished_at,
      next_run_at: submission.parsing.next_run_at,
      last_error_code: submission.parsing.last_error_code,
      last_error_message: submission.parsing.last_error_message,
    }
  },
  async canonicalStatus(sid) {
    assertSubmission(sid)
    const state = demoState()
    const submission = state.submissions.find((s) => s.id === sid)
    if (!submission) throw demoError(404, '资源不可用或没有访问权限。')
    const canonical = state.canonicals[sid]
    return {
      submission_id: sid,
      status: canonical ? ('available' as const) : ('not_generated' as const),
      source_parsing_status: submission.parsing.status,
      failure_code: null,
      canonical_document: canonical
        ? {
            document_id: canonical.document_id,
            source_artifact_id: canonical.source_artifact_id,
            schema_version: 'demo-v1',
            normalizer_version: 'demo-normalizer-0.9',
            source_parser: 'demo-parser',
            source_parser_version: '1.0',
            source_sha256: `sha-src-${sid}`,
            canonical_sha256: canonical.canonical_sha256,
            page_count: canonical.page_count,
            block_count: canonical.block_count,
            created_at: submission.created_at,
          }
        : null,
    }
  },
  async page(sid, pageNumber) {
    assertSubmission(sid)
    const canonical = demoState().canonicals[sid]
    if (!canonical) throw demoError(404, '作答内容尚未生成。')
    const page = canonical.pages[pageNumber - 1]
    if (!page) throw demoError(404, '页面不存在。')
    return page
  },
  async alignment(sid) {
    assertSubmission(sid)
    const state = demoState()
    const canonical = state.canonicals[sid]
    const submission = state.submissions.find((s) => s.id === sid)
    if (!submission) throw demoError(404, '资源不可用或没有访问权限。')
    if (!canonical) return null
    const questions = assignmentQuestions(submission.assignment_id)
    const statuses = questions.map(
      (q) => canonical.aligned[q.id]?.matching_status ?? 'not_observed',
    )
    return {
      submission_id: sid,
      assignment_id: submission.assignment_id,
      status: statuses.includes('review_required')
        ? ('review_required' as const)
        : ('complete' as const),
      canonical_document_id: canonical.document_id,
      aligner_version: canonical.aligner_version,
      assignment_questions_digest: canonical.questions_digest,
      question_count: questions.length,
      aligned_count: statuses.filter((s) => s === 'aligned').length,
      review_required_count: statuses.filter((s) => s === 'review_required').length,
      unmatched_count: 0,
      not_observed_count: statuses.filter((s) => s === 'not_observed').length,
    }
  },
  async questionAlignment(sid, qid) {
    assertSubmission(sid, qid)
    const state = demoState()
    const canonical = state.canonicals[sid]
    const submission = state.submissions.find((s) => s.id === sid)
    const question = state.questions[qid]
    if (!canonical || !submission || !question) throw demoError(404, '对齐结果不可用。')
    assertVisible(submission.assignment_id)
    const record = canonical.aligned[qid] ?? {
      matching_status: 'not_observed' as const,
      evidence_pointers: [],
    }
    const answerPointer = record.evidence_pointers.find((p) => p.text_end && p.text_end > 2)
    const answerMeta = answerPointer ? canonical.pointers[answerPointer.pointer] : null
    return {
      submission_id: sid,
      assignment_id: submission.assignment_id,
      canonical_document_id: canonical.document_id,
      answer: {
        question_id: qid,
        question_number: question.question_number,
        question_type: question.question_type,
        matching_status: record.matching_status,
        evidence: record.evidence_pointers
          .filter((p) => canonical.pointers[p.pointer]?.text)
          .slice(0, 1)
          .map((p, index) => ({
            candidate_id: `cand-${qid}-${index}`,
            question_number: question.question_number,
            question_id: qid,
            marker_kind: 'explicit_chinese' as const,
            marker_text: '题号',
            page_index: p.page_number - 1,
            source_block_id: canonical.pointers[p.pointer]?.block_id ?? 'blk',
            reading_order: index,
            content_pointer: p.pointer,
            text_start: p.text_start ?? 0,
            text_end: p.text_end ?? 0,
            matching_status: 'aligned' as const,
          })),
        source_regions: answerPointer
          ? [
              {
                page_index: answerPointer.page_number - 1,
                source_block_id: canonical.pointers[answerPointer.pointer]?.block_id ?? 'blk',
                reading_order: 0,
                bbox: [64, 150, 730, 260] as [number, number, number, number],
                source_type: 'demo_text',
                normalized_type: 'text',
                content_pointer: answerPointer.pointer,
                text_start: answerPointer.text_start,
                text_end: answerPointer.text_end,
                text: answerMeta?.text ?? null,
                asset_refs: [],
              },
            ]
          : [],
        text_projection: answerMeta?.text ?? '',
        asset_refs: [],
      },
    }
  },
  async file(sid) {
    const state = demoState()
    const submission = state.submissions.find((s) => s.id === sid)
    if (!submission) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(submission.assignment_id)
    if (submission.uploaded) return loadDemoFile(sid)
    const canonical = state.canonicals[sid]
    const questions: { number: number; stem: string; answer?: string }[] = []
    if (canonical) {
      for (const page of canonical.pages) {
        for (const block of page.page.blocks) {
          const value = block.content.value
          if (!value) continue
          if (/^\d+[.、]/.test(value)) questions.push({ number: questions.length + 1, stem: value })
          else if (questions.length) questions[questions.length - 1]!.answer = value
        }
      }
    } else {
      const assignmentQuestions2 = assignmentQuestions(submission.assignment_id)
      for (const q of assignmentQuestions2)
        questions.push({ number: q.question_number, stem: q.stem })
    }
    const assignment = state.assignments.find((a) => a.id === submission.assignment_id)!
    return renderExamPage({
      title: assignment.title,
      subject: `${assignment.subject} · ${assignment.grade_level}`,
      studentRef: submission.student_ref,
      questions,
    })
  },
}

/* ---------------- Reviews ---------------- */

const reviews: ReviewsService = {
  async inspect(sid, qid, _evidenceOffset = 0, _confirmedOffset = 0) {
    assertSubmission(sid, qid)
    const state = demoState()
    const canonical = state.canonicals[sid]
    const submission = state.submissions.find((s) => s.id === sid)
    const question = state.questions[qid]
    if (!canonical || !submission || !question) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(submission.assignment_id)
    const record = state.reviews[`${sid}:${qid}`]
    const currentSource = sourceOf(sid)
    const latest = record?.history[record.history.length - 1] ?? null
    const aligned = canonical.aligned[qid]
    const evidence: S['EvidenceSummary'][] = (aligned?.evidence_pointers ?? []).map((p) => {
      const meta = canonical.pointers[p.pointer]
      return {
        content_pointer: p.pointer,
        page_number: meta?.page_number ?? p.page_number,
        source_block_id: meta?.block_id ?? 'blk',
        text_start: p.text_start,
        text_end: p.text_end,
        normalized_type: meta?.normalized_type ?? 'text',
        asset_count: meta?.normalized_type === 'image' ? 1 : 0,
        canonical_page_path: `/api/v1/submissions/${sid}/canonical-document/pages/${meta?.page_number ?? 1}`,
      }
    })
    const confirmed: S['ConfirmedRegionSummary'][] = []
    if (record && latest) {
      for (const region of record.regions[latest.decision_id] ?? []) {
        const meta = canonical.pointers[region.content_pointer]
        confirmed.push({
          role:
            latest.decision === 'response_present'
              ? 'response_regions'
              : latest.decision === 'response_absent'
                ? 'excluded_prompt_regions'
                : 'uncertain_regions',
          content_pointer: region.content_pointer,
          page_number: meta?.page_number ?? 1,
          text_start: region.text_start ?? null,
          text_end: region.text_end ?? null,
        })
      }
    }
    return {
      submission_id: sid,
      question_id: qid,
      source: currentSource,
      alignment_status: aligned?.matching_status ?? 'not_observed',
      answer_presence: latest?.decision ?? 'unreviewed',
      expected_revision: record?.history.length ?? 0,
      latest,
      latest_source_matches: latest ? sourceMatches(latest.source, currentSource) : false,
      evidence,
      evidence_offset: 0,
      evidence_truncated: false,
      next_evidence_offset: null,
      confirmed_regions: confirmed,
      confirmed_offset: 0,
      confirmed_truncated: false,
      next_confirmed_offset: null,
      meaning: 'human_answer_presence_not_grading',
    }
  },
  async history(sid, qid, offset = 0) {
    assertSubmission(sid, qid)
    const state = demoState()
    const record = state.reviews[`${sid}:${qid}`]
    const limit = 10
    return {
      submission_id: sid,
      records: (record?.history ?? []).slice(offset, offset + limit),
      offset,
      limit,
      has_next: offset + limit < (record?.history.length ?? 0),
      next_offset: offset + limit < (record?.history.length ?? 0) ? offset + limit : null,
      source_validity: 'not_checked',
    }
  },
  async save(sid, qid, body) {
    assertSubmission(sid, qid)
    const state = demoState()
    const canonical = state.canonicals[sid]
    const submission = state.submissions.find((s) => s.id === sid)
    if (!canonical || !submission) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(submission.assignment_id)
    if (!canWrite(submission.assignment_id)) throw demoError(403, '当前账号对该作业没有写权限。')
    const parsed = JSON.parse(body) as S['ReviewRequest']
    return idempotent(parsed.request_id, { sid, qid, ...parsed }, () => {
      const key = `${sid}:${qid}`
      const record = state.reviews[key] ?? { source: sourceOf(sid), history: [], regions: {} }
      const currentSource = sourceOf(sid)
      if (parsed.expected_revision !== record.history.length)
        throw demoError(409, '来源或他人的核对记录已更新，请刷新后重新核对。')
      if (!sourceMatches(parsed.expected_source, currentSource))
        throw demoError(409, '来源版本已变化，请刷新后重新选取证据。')
      if (!allowedReasons[parsed.decision].includes(parsed.reason_codes[0] ?? ''))
        throw demoError(422, '判断原因与决定不一致。')
      const regions = [
        ...(parsed.response_regions ?? []),
        ...(parsed.excluded_prompt_regions ?? []),
        ...(parsed.uncertain_regions ?? []),
      ]
      if (!regions.length && parsed.decision !== 'uncertain')
        throw demoError(422, '请选择真实证据范围，未选中文字不等于学生没有作答。')
      const allowed = canonical.aligned[qid]?.evidence_pointers ?? []
      for (const region of regions) {
        const node = canonical.pointers[region.content_pointer]
        const image =
          node?.normalized_type === 'image' && region.text_start == null && region.text_end == null
        if (
          !node ||
          !allowed.some((e) => e.pointer === region.content_pointer) ||
          (!image &&
            (region.text_start == null ||
              region.text_end == null ||
              region.text_start < 0 ||
              region.text_end <= region.text_start ||
              region.text_end > CODE_POINTS(node.text ?? '')))
        )
          throw demoError(422, '证据必须来自本题已定位的独立来源；文字需使用合法字符范围。')
      }
      for (let i = 0; i < regions.length; i++)
        for (let j = i + 1; j < regions.length; j++) {
          const a = regions[i]!,
            b = regions[j]!
          if (
            a.content_pointer === b.content_pointer &&
            (a.text_start == null ||
              b.text_start == null ||
              (a.text_start < b.text_end! && b.text_start < a.text_end!))
          )
            throw demoError(422, '同一文字范围不能重复标注。')
        }
      const revision = record.history.length + 1
      const decisionId = nextId('dec')
      const user = currentUser()
      const summary: S['RevisionSummary'] = {
        decision_id: decisionId,
        question_id: qid,
        decision: parsed.decision,
        revision,
        request_id: parsed.request_id,
        supersedes_decision_id: record.history[record.history.length - 1]?.decision_id ?? null,
        created_at: new Date().toISOString(),
        reviewer_ref: `teacher_${user.id.replace(/-/g, '')}`,
        reviewer_kind: 'authenticated_teacher',
        reviewer_user_id: user.id,
        source: currentSource,
        region_counts: {
          response_regions: parsed.decision === 'response_present' ? regions.length : 0,
          excluded_prompt_regions: parsed.decision === 'response_absent' ? regions.length : 0,
          uncertain_regions: parsed.decision === 'uncertain' ? regions.length : 0,
        },
        reason_codes: parsed.reason_codes,
      }
      record.history.push(summary)
      record.regions[decisionId] = regions
      record.source = currentSource
      state.reviews[key] = record
      const question = state.questions[qid]
      logOp(
        '保存了作答核对记录',
        `${state.assignments.find((a) => a.id === submission.assignment_id)!.title} · ${submission.student_ref} · 第 ${question?.question_number ?? '?'} 题（R${revision}）`,
      )
      persist()
      return { created: true, record: summary, source_validity: 'validated_at_commit' }
    })
  },
  async progress(sid) {
    const state = demoState()
    const submission = state.submissions.find((s) => s.id === sid)
    if (!submission) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(submission.assignment_id)
    const questions = assignmentQuestions(submission.assignment_id)
    return {
      submission_id: sid,
      assignment_id: submission.assignment_id,
      student_ref: submission.student_ref,
      questions: questions.map((q) => {
        const record = state.reviews[`${sid}:${q.id}`]
        const latest = record?.history[record.history.length - 1]
        return {
          question_id: q.id,
          question_number: q.question_number,
          answer_presence: latest?.decision ?? 'unreviewed',
          latest_revision: record?.history.length ?? 0,
          latest_source_matches: latest ? sourceMatches(latest.source, sourceOf(sid)) : false,
        }
      }),
    }
  },
  async digest() {
    const state = demoState()
    const visible = visibleAssignments()
    const weekAgo = Date.now() - 7 * 86_400_000
    let submissionsRecent = 0
    let parsingActive = 0
    let parseAlerts = 0
    const recent: { submission: S['SubmissionRead']; assignment_title: string }[] = []
    const tasks: WorkbenchTask[] = []
    for (const assignment of visible) {
      const rows = state.submissions
        .filter((s) => s.assignment_id === assignment.id)
        .sort((a, b) => b.submitted_at.localeCompare(a.submitted_at))
      for (const submission of rows) {
        recent.push({
          submission: toSubmissionRead(submission),
          assignment_title: assignment.title,
        })
        if (new Date(submission.submitted_at).getTime() >= weekAgo) submissionsRecent++
        const status = submission.parsing.status
        if (status === 'pending' || status === 'running' || status === 'retry_wait') parsingActive++
        if (status === 'failed' || status === 'retry_wait') parseAlerts++
      }
      // 待核对任务：已生成 Canonical 且仍有未核对题目的提交。
      for (const submission of rows) {
        const canonical = state.canonicals[submission.id]
        if (!canonical) continue
        const questions = assignmentQuestions(assignment.id)
        const pending = questions.filter(
          (q) => !state.reviews[`${submission.id}:${q.id}`]?.history.length,
        )
        if (pending.length) {
          tasks.push({
            kind: 'review',
            title: `${assignment.title} · ${submission.student_ref}`,
            description: `${pending.length} 题待人工核对`,
            href: `/assignments/${assignment.id}/submissions/${submission.id}`,
            assignment_title: assignment.title,
            student_ref: submission.student_ref,
            created_at: submission.submitted_at,
          })
        }
        if (tasks.filter((t) => t.kind === 'review').length >= 4) break
      }
    }
    // 解析异常提醒
    for (const submission of state.submissions) {
      if (!visible.some((a) => a.id === submission.assignment_id)) continue
      if (submission.parsing.status === 'failed' || submission.parsing.status === 'retry_wait') {
        tasks.push({
          kind: 'parse_alert',
          title: `${state.assignments.find((a) => a.id === submission.assignment_id)!.title} · ${submission.student_ref}`,
          description:
            submission.parsing.status === 'failed'
              ? '解析失败：原始文件版式无法识别'
              : '解析等待重试中，已自动重试 2 次',
          href: `/assignments/${submission.assignment_id}/submissions/${submission.id}`,
          assignment_title: state.assignments.find((a) => a.id === submission.assignment_id)!.title,
          student_ref: submission.student_ref,
          created_at: submission.submitted_at,
        })
      }
    }
    // 候选评分待审核
    for (const proposal of state.proposals) {
      const submission = state.submissions.find((s) => s.id === proposal.submission_id)
      if (!submission || !visible.some((a) => a.id === submission.assignment_id)) continue
      if (proposal.reviews.length) continue
      tasks.push({
        kind: 'grading',
        title: `AI 候选待审核 · ${state.assignments.find((a) => a.id === submission.assignment_id)!.title}`,
        description: `${submission.student_ref} · 第 ${state.questions[proposal.question_id]?.question_number ?? '?'} 题${proposal.grading_status === 'grading_abstained' ? '（模型弃权）' : ''}`,
        href: `/assignments/${submission.assignment_id}/submissions/${submission.id}/questions/${proposal.question_id}/grading`,
        assignment_title: state.assignments.find((a) => a.id === submission.assignment_id)!.title,
        student_ref: submission.student_ref,
        created_at: proposal.created_at,
      })
    }
    recent.sort((a, b) => b.submission.submitted_at.localeCompare(a.submission.submitted_at))
    return {
      assignments_total: visible.length,
      assignments_published: visible.filter((a) => a.status === 'published').length,
      submissions_recent: submissionsRecent,
      parsing_active: parsingActive,
      parse_alerts: parseAlerts,
      scope_note: '演示数据：按当前可访问作业的提交批次统计，趋势图最多展示365天（合成数据）。',
      tasks: tasks.slice(0, 8),
      recent_submissions: recent.slice(0, 8),
      recent_ops: state.ops
        .slice(0, 6)
        .map((op) => ({ id: op.id, summary: op.summary, detail: op.detail, at: op.at })),
    }
  },
}

/* ---------------- Grading ---------------- */

function makeGradeRevision(input: {
  pid: string
  revision: number
  status: S['GradeReviewRequest']['review_status']
  proposed: string | null
  reviewed: string | null
  criteria: {
    criterion_id: string
    proposed: string
    reviewed: string
    changed: boolean
    max: string
  }[]
  reasonCodes: S['GradeReviewRequest']['review_reason_codes']
  reviewerId: string
  at: string
  requestId?: string
}): S['GradeRevisionSummary'] {
  return {
    review_id: nextId('gr'),
    proposal_id: input.pid,
    grade_input_artifact_id: `gi-${input.pid}`,
    proposal_sha256: `sha-p-${input.pid}`,
    grade_input_sha256: `sha-gi-${input.pid}`,
    review_revision: input.revision,
    supersedes_review_id:
      demoState()
        .proposals.find((p) => p.id === input.pid)
        ?.reviews.at(-1)?.review_id ?? null,
    reviewer_ref: `teacher_${input.reviewerId.replace(/-/g, '')}`,
    reviewer_user_id: input.reviewerId,
    reviewer_kind: 'authenticated_teacher',
    review_reason_codes: input.reasonCodes ?? [],
    review_comment_present: false,
    request_id: input.requestId ?? nextId('greq'),
    review_status: input.status,
    proposed_total_score: input.proposed,
    reviewed_total_score: input.reviewed,
    criterion_scores: input.criteria.map((c) => ({
      criterion_id: c.criterion_id,
      proposed_points: c.proposed,
      reviewed_points: c.reviewed,
      max_points: c.max,
      changed: c.changed,
    })),
    created_at: input.at,
    grade_published: false,
  }
}

const grading: GradingService = {
  async list(sid, qid) {
    assertSubmission(sid, qid)
    const state = demoState()
    const submission = state.submissions.find((s) => s.id === sid)
    const question = state.questions[qid]
    if (!submission || !question) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(submission.assignment_id)
    return state.proposals
      .filter((p) => p.submission_id === sid && p.question_id === qid)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map((p) => ({
        proposal_id: p.id,
        question_id: qid,
        question_number: question.question_number,
        grading_status: p.grading_status,
        source_status: p.source_status,
        proposed_total_score: p.proposed_total_score,
        max_score: p.max_score,
        latest_review_status: p.reviews[p.reviews.length - 1]?.review_status ?? null,
        latest_review_revision: p.reviews.length,
        has_review_history: p.reviews.length > 0,
        created_at: p.created_at,
      }))
  },
  async inspect(sid, qid, pid, _evidenceOffset = 0) {
    assertSubmission(sid, qid)
    const state = demoState()
    const proposal = state.proposals.find(
      (p) => p.id === pid && p.submission_id === sid && p.question_id === qid,
    )
    if (!proposal) throw demoError(404, '资源不可用或没有访问权限。')
    assertVisible(state.submissions.find((s) => s.id === sid)!.assignment_id)
    const latest = proposal.reviews[proposal.reviews.length - 1] ?? null
    const criteria = proposal.criteria.map((c) => {
      const criterion = (state.rubrics[qid] ?? []).find((r) => r.id === c.criterion_id)
      return {
        criterion_id: c.criterion_id,
        proposed_points: c.proposed_points,
        max_points: criterion?.points ?? '0.00',
      }
    })
    return {
      proposal_id: pid,
      candidate_rationales: proposal.criteria.map((c) => ({
        criterion_id: c.criterion_id,
        rationale: c.rationale,
      })),
      grading_status: proposal.grading_status,
      source_status: proposal.source_status,
      grade_input_artifact_id: `gi-${pid}`,
      grade_input_sha256: `sha-gi-${pid}`,
      proposal_sha256: `sha-p-${pid}`,
      submission_id: sid,
      question_id: qid,
      proposed_total_score: proposal.proposed_total_score,
      max_score: proposal.max_score,
      criterion_scores: criteria,
      latest_review_revision: proposal.reviews.length,
      latest_review_status: latest?.review_status ?? null,
      reviewer_identity_authenticated: proposal.reviews.some(
        (r) => r.reviewer_kind === 'authenticated_teacher',
      ),
      grade_published: false,
      latest,
      regrading_required: latest?.review_status === 'regrading_required',
      has_review_history: proposal.reviews.length > 0,
      evidence_locations: proposal.evidence.map((e) => ({
        criterion_id: e.criterion_id,
        segment_index: [...new Set(proposal.evidence.map((item) => item.content_pointer))].indexOf(
          e.content_pointer,
        ),
        content_pointer: e.content_pointer,
        page_number: e.page_number,
        text_start: e.text_start,
        text_end: e.text_end,
        canonical_text_start: e.text_start,
        canonical_text_end: e.text_end,
        canonical_page_path: `/api/v1/submissions/${sid}/canonical-document/pages/${e.page_number}`,
      })),
      evidence_offset: 0,
      evidence_truncated: false,
      next_evidence_offset: null,
    }
  },
  async history(sid, qid, pid, offset = 0) {
    assertSubmission(sid, qid)
    const state = demoState()
    const proposal = state.proposals.find(
      (p) => p.id === pid && p.submission_id === sid && p.question_id === qid,
    )
    if (!proposal) throw demoError(404, '资源不可用或没有访问权限。')
    const limit = 5
    const records = proposal.reviews
      .slice()
      .reverse()
      .slice(offset, offset + limit)
    return {
      proposal_id: pid,
      records,
      limit,
      offset,
      has_next: offset + limit < proposal.reviews.length,
      next_offset: offset + limit < proposal.reviews.length ? offset + limit : null,
      source_validity: 'not_checked',
      grade_published: false,
    }
  },
  async save(sid, qid, pid, body) {
    assertSubmission(sid, qid)
    const state = demoState()
    const proposal = state.proposals.find(
      (p) => p.id === pid && p.submission_id === sid && p.question_id === qid,
    )
    if (!proposal) throw demoError(404, '资源不可用或没有访问权限。')
    const submission = state.submissions.find((s) => s.id === sid)!
    if (!canWrite(submission.assignment_id)) throw demoError(403, '当前账号对该作业没有写权限。')
    if (proposal.source_status === 'source_stale')
      throw demoError(409, '候选来源已过期，不能追加新的评分审核。')
    const parsed = JSON.parse(body) as S['GradeReviewRequest']
    return idempotent(parsed.request_id, { sid, qid, pid, ...parsed }, () => {
      if (parsed.expected_revision !== proposal.reviews.length)
        throw demoError(409, '该候选的审核已更新，请刷新后重新处理。')
      const user = currentUser()
      const now = new Date().toISOString()
      const criteria = proposal.criteria.map((c) => {
        const criterion = (state.rubrics[qid] ?? []).find((r) => r.id === c.criterion_id)
        const review = (parsed.criterion_reviews ?? []).find(
          (r) => r.criterion_id === c.criterion_id,
        )
        return { base: c, criterion, review }
      })
      if (parsed.review_status === 'accepted') {
        if (proposal.grading_status === 'grading_abstained')
          throw demoError(422, '模型弃权的候选不能被直接接受。')
        const record = makeGradeRevision({
          pid,
          revision: proposal.reviews.length + 1,
          status: 'accepted',
          proposed: proposal.proposed_total_score,
          reviewed: proposal.proposed_total_score,
          criteria: criteria.map((c) => ({
            criterion_id: c.base.criterion_id,
            proposed: c.base.proposed_points,
            reviewed: c.base.proposed_points,
            changed: false,
            max: c.criterion?.points ?? '0.00',
          })),
          reasonCodes: [],
          reviewerId: user.id,
          at: now,
          requestId: parsed.request_id,
        })
        proposal.reviews.push(record)
        logOp(
          '接受了 AI 候选评分',
          `第 ${state.questions[qid]?.question_number ?? '?'} 题 · 候选 ${proposal.proposed_total_score ?? '—'} 分`,
        )
        persist()
        return { created: true, record, grade_published: false }
      }
      if (parsed.review_status === 'revised') {
        if (proposal.grading_status === 'grading_abstained')
          throw demoError(422, '模型弃权的候选没有可修订的分数。')
        const rubric = state.rubrics[qid] ?? []
        if (
          (parsed.criterion_reviews ?? []).length !== rubric.length ||
          new Set(parsed.criterion_reviews?.map((r) => r.criterion_id)).size !== rubric.length ||
          parsed.criterion_reviews?.some((r) => !rubric.some((c) => c.id === r.criterion_id))
        )
          throw demoError(422, '修订必须提交全部评分项的完整快照。')
        let reviewedTotal = '0.00'
        const rows: {
          criterion_id: string
          proposed: string
          reviewed: string
          changed: boolean
          max: string
        }[] = []
        for (const item of criteria) {
          const points = String(item.review?.reviewed_points ?? '')
          if (!isValidDecimal(points))
            throw demoError(
              422,
              `评分项「${item.criterion?.description ?? item.base.criterion_id}」分值格式不正确。`,
            )
          if (compareDecimals(points, item.criterion?.points ?? '0.00') > 0)
            throw demoError(422, `评分项「${item.criterion?.description ?? ''}」超过上限。`)
          if (compareDecimals(points, item.base.proposed_points) !== 0) {
            if (!item.review?.rationale?.trim()) throw demoError(422, '修改评分项时需填写理由。')
            const refs = item.review.evidence_references ?? []
            const pointers = [...new Set(proposal.evidence.map((e) => e.content_pointer))]
            if (compareDecimals(points, '0') > 0 && !refs.some((r) => r.kind === 'quote'))
              throw demoError(422, '正分修订需要可追溯的原文引文。')
            for (const ref of refs)
              if (ref.kind === 'quote') {
                const pointer = pointers[ref.segment_index]
                const text = pointer ? demoState().canonicals[sid]?.pointers[pointer]?.text : null
                if (
                  !text ||
                  ref.text_start < 0 ||
                  ref.text_end <= ref.text_start ||
                  ref.text_end > CODE_POINTS(text) ||
                  Array.from(text).slice(ref.text_start, ref.text_end).join('') !== ref.quote
                )
                  throw demoError(422, '引文与评分输入范围不一致。')
              }
          }
          reviewedTotal = addDecimals(reviewedTotal, points)
          rows.push({
            criterion_id: item.base.criterion_id,
            proposed: item.base.proposed_points,
            reviewed: points,
            changed: compareDecimals(points, item.base.proposed_points) !== 0,
            max: item.criterion?.points ?? '0.00',
          })
        }
        const changed = rows.filter((r) => r.changed)
        if (changed.length && !(parsed.review_reason_codes ?? []).length)
          throw demoError(422, '修改评分项时必须填写原因。')
        const record = makeGradeRevision({
          pid,
          revision: proposal.reviews.length + 1,
          status: 'revised',
          proposed: proposal.proposed_total_score,
          reviewed: reviewedTotal,
          criteria: rows,
          reasonCodes: parsed.review_reason_codes ?? [],
          reviewerId: user.id,
          at: now,
          requestId: parsed.request_id,
        })
        proposal.reviews.push(record)
        logOp(
          '修订了 AI 候选评分',
          `第 ${state.questions[qid]?.question_number ?? '?'} 题 · 修订后 ${reviewedTotal} 分`,
        )
        persist()
        return { created: true, record, grade_published: false }
      }
      // rejected / regrading_required
      if (!(parsed.review_reason_codes ?? []).length) throw demoError(422, '请选择处理原因。')
      const record = makeGradeRevision({
        pid,
        revision: proposal.reviews.length + 1,
        status: parsed.review_status,
        proposed: proposal.proposed_total_score,
        reviewed: null,
        criteria: criteria.map((c) => ({
          criterion_id: c.base.criterion_id,
          proposed: c.base.proposed_points,
          reviewed: c.base.proposed_points,
          changed: false,
          max: c.criterion?.points ?? '0.00',
        })),
        reasonCodes: parsed.review_reason_codes ?? [],
        reviewerId: user.id,
        at: now,
        requestId: parsed.request_id,
      })
      proposal.reviews.push(record)
      logOp(
        parsed.review_status === 'rejected' ? '拒绝了 AI 候选评分' : '要求重新评分',
        `第 ${state.questions[qid]?.question_number ?? '?'} 题`,
      )
      persist()
      return { created: true, record, grade_published: false }
    })
  },
}

/* ---------------- Admin ---------------- */

const admin: AdminService = {
  async teachers(query) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    let rows: TeacherRecord[] = state.users
      .filter((u) => u.role === 'teacher')
      .map((u) => ({
        user_id: u.id,
        role: 'teacher' as const,
        is_active: u.is_active,
        created_at: u.created_at,
        display_name: u.name,
      }))
    const keyword = query.keyword?.trim()
    if (keyword)
      rows = rows.filter(
        (r) =>
          r.user_id.toLowerCase().includes(keyword.toLowerCase()) ||
          (r.display_name ?? '').includes(keyword),
      )
    if (query.active === 'active') rows = rows.filter((r) => r.is_active)
    if (query.active === 'inactive') rows = rows.filter((r) => !r.is_active)
    rows = rows.slice().sort((a, b) => b.created_at.localeCompare(a.created_at))
    const limit = query.limit ?? 25
    const offset = query.offset ?? 0
    return {
      records: rows.slice(offset, offset + limit),
      has_next: offset + limit < rows.length,
      total: rows.length,
    }
  },
  async register(body) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    return idempotent(body.request_id, body, () => {
      if (body.issuer !== 'https://idp.huipi-demo.cn')
        throw demoError(422, 'Issuer 与部署配置不一致（演示环境固定为演示 IdP）。')
      if (!body.subject.trim()) throw demoError(422, 'Subject 不能为空。')
      const id = nextId('u-teacher')
      if (state.users.some((u) => u.subject === body.subject))
        throw demoError(409, '该外部身份已登记。')
      state.users.push({
        id,
        name: `演示教师${state.users.filter((u) => u.role === 'teacher').length + 1}`,
        role: 'teacher',
        is_active: true,
        subject: body.subject,
        created_at: new Date().toISOString(),
      })
      const event = logAudit({
        action: 'teacher_registered',
        request_id: body.request_id,
        actor_user_id: currentUser().id,
        target_user_id: id,
        state: { is_active: true },
      })
      persist()
      return {
        operation_id: event.operation_id,
        request_id: event.request_id,
        action: 'teacher_registered',
        target_user_id: id,
        assignment_id: null,
        state: { is_active: true },
        created_at: event.created_at,
      }
    })
  },
  async setActive(uid, body) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    return idempotent(body.request_id, { uid, ...body }, () => {
      const user = state.users.find((u) => u.id === uid && u.role === 'teacher')
      if (!user) throw demoError(404, '教师账号不存在。')
      user.is_active = body.is_active
      for (const membership of state.memberships.filter((m) => m.user_id === uid))
        membership.is_active = body.is_active && !membership.revoked_at
      const event = logAudit({
        action: 'teacher_activation_changed',
        request_id: body.request_id,
        actor_user_id: currentUser().id,
        target_user_id: uid,
        state: { is_active: body.is_active },
      })
      persist()
      return {
        operation_id: event.operation_id,
        request_id: event.request_id,
        action: 'teacher_activation_changed',
        target_user_id: uid,
        assignment_id: null,
        state: { is_active: body.is_active },
        created_at: event.created_at,
      }
    })
  },
  async memberships(uid, offset = 0) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    const rows = state.memberships.filter((m) => m.user_id === uid)
    const limit = 50
    return {
      records: rows.slice(offset, offset + limit).map((m) => ({
        assignment_id: m.assignment_id,
        user_id: m.user_id,
        can_write: m.can_write,
        is_active: m.is_active,
        created_at: m.created_at,
        revoked_at: m.revoked_at,
      })),
      has_next: offset + limit < rows.length,
    }
  },
  async grant(aid, uid, body) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    return idempotent(body.request_id, { aid, uid, can_write: body.can_write }, () => {
      const assignment = state.assignments.find((a) => a.id === aid)
      const user = state.users.find((u) => u.id === uid && u.role === 'teacher')
      if (!assignment || !user) throw demoError(404, '作业或教师不存在。')
      if (!user.is_active) throw demoError(409, '该教师账号已停用，不能分配授权。')
      const now = new Date().toISOString()
      const existing = state.memberships.find((m) => m.assignment_id === aid && m.user_id === uid)
      if (existing) {
        existing.can_write = body.can_write
        existing.revoked_at = null
        existing.is_active = true
        existing.created_at = now
      } else {
        state.memberships.push({
          assignment_id: aid,
          user_id: uid,
          can_write: body.can_write,
          is_active: true,
          created_at: now,
          revoked_at: null,
        })
      }
      const event = logAudit({
        action: 'assignment_membership_granted',
        request_id: body.request_id,
        actor_user_id: currentUser().id,
        target_user_id: uid,
        assignment_id: aid,
        state: { can_write: body.can_write },
      })
      persist()
      return {
        assignment_id: aid,
        user_id: uid,
        can_write: body.can_write,
        operation: {
          operation_id: event.operation_id,
          request_id: event.request_id,
          action: 'assignment_membership_granted',
          target_user_id: uid,
          assignment_id: aid,
          state: { can_write: body.can_write },
          created_at: event.created_at,
        },
      }
    })
  },
  async revoke(aid, uid, requestId) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    return idempotent(requestId, { aid, uid, revoke: true }, () => {
      const membership = state.memberships.find((m) => m.assignment_id === aid && m.user_id === uid)
      if (!membership || membership.revoked_at) throw demoError(404, '该授权不存在或已撤销。')
      membership.revoked_at = new Date().toISOString()
      membership.is_active = false
      logAudit({
        action: 'assignment_membership_revoked',
        request_id: requestId,
        actor_user_id: currentUser().id,
        target_user_id: uid,
        assignment_id: aid,
        state: { is_active: false, can_write: false },
      })
      persist()
    })
  },
  async audit(query) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    let rows = state.audit
    if (query.target_user_id) rows = rows.filter((e) => e.target_user_id === query.target_user_id)
    if (query.assignment_id) rows = rows.filter((e) => e.assignment_id === query.assignment_id)
    if (query.request_id) rows = rows.filter((e) => e.request_id === query.request_id)
    const limit = query.limit ?? 25
    const offset = query.offset ?? 0
    return { records: rows.slice(offset, offset + limit), has_next: offset + limit < rows.length }
  },
  async overview() {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const state = demoState()
    const teachers = state.users.filter((u) => u.role === 'teacher')
    return {
      teachers_total: teachers.length,
      teachers_active: teachers.filter((u) => u.is_active).length,
      authorizations_active: state.memberships.filter((m) => m.is_active && !m.revoked_at).length,
      scope_note: '演示数据：账号、授权与审计均来自演示环境（合成数据）。',
      recent_audit: state.audit.slice(0, 5),
      modules: await system.modules(),
    }
  },
  async assignableAssignments(query = {}) {
    if (!isAdmin()) throw demoError(403, '当前身份不能执行此操作。')
    const all = demoState().assignments.map(toSummary),
      offset = query.offset ?? 0,
      limit = query.limit ?? 50
    return { records: all.slice(offset, offset + limit), has_next: offset + limit < all.length }
  },
}

/* ---------------- Knowledge ---------------- */

const scheduledIndexing = new Set<string>()
function scheduleIndexing(docId: string) {
  if (scheduledIndexing.has(docId)) return
  scheduledIndexing.add(docId)
  window.setTimeout(() => {
    const state = demoState()
    const doc = state.knowledge.find((d) => d.id === docId)
    if (!doc || doc.status !== 'indexing') return
    doc.status = 'indexed'
    if (!doc.chunks.length)
      doc.chunks = [1, 2, 3].map((n) => ({
        id: `${docId}-c${n}`,
        text: `《${doc.title}》第 ${n} 段（演示合成索引内容）：围绕${doc.category}的核心概念展开，包含定义、例题与教学建议，可用于检索演示与 AI 助教引用。`,
        page_hint: `第 ${n} 页`,
      }))
    doc.chunk_count = doc.chunks.length
    persist()
  }, 5000)
}

const knowledge: KnowledgeService = {
  async list() {
    return demoState()
      .knowledge.filter(
        (d) => !d.assignment_id || visibleAssignments().some((a) => a.id === d.assignment_id),
      )
      .map((d) => ({
        id: d.id,
        title: d.title,
        category: d.category,
        file_type: d.file_type,
        size_bytes: d.size_bytes,
        status: d.status,
        chunk_count: d.chunk_count,
        version: d.version,
        assignment_id: d.assignment_id,
        created_at: d.created_at,
      }))
  },
  async detail(id) {
    const doc = demoState().knowledge.find((d) => d.id === id)
    if (
      !doc ||
      (doc.assignment_id && !visibleAssignments().some((a) => a.id === doc.assignment_id))
    )
      throw demoError(404, '资料不存在。')
    return {
      id: doc.id,
      title: doc.title,
      category: doc.category,
      file_type: doc.file_type,
      size_bytes: doc.size_bytes,
      status: doc.status,
      chunk_count: doc.chunk_count,
      version: doc.version,
      assignment_id: doc.assignment_id,
      created_at: doc.created_at,
      chunks: doc.chunks,
    }
  },
  async upload(input) {
    const state = demoState()
    const id = nextId('kd')
    if (input.assignment_id) assertVisible(input.assignment_id)
    if (input.file.size > 20 * 1024 * 1024) throw demoError(413, '资料不能超过20MiB。')
    if (!/\.(pdf|txt|md|docx)$/i.test(input.file.name))
      throw demoError(415, '请选择支持的教学资料格式。')
    const title = input.file.name.replace(/\.[^.]+$/, '')
    const raw = /\.(txt|md)$/i.test(input.file.name) ? await input.file.text() : ''
    if (Array.from(raw).length > 100000) throw demoError(413, '文本资料最多十万字符。')
    const chars = Array.from(raw)
    const chunks = Array.from({ length: Math.ceil(chars.length / 800) }, (_, i) => ({
      id: `${id}-c${i + 1}`,
      text: chars.slice(i * 800, (i + 1) * 800).join(''),
      page_hint: `第${i + 1}段`,
    }))
    state.knowledge.unshift({
      id,
      title,
      category: input.category || '未分类',
      file_type: (input.file.name.split('.').pop() ?? 'pdf').toLowerCase(),
      size_bytes: input.file.size || 120_000,
      status: 'indexing',
      chunk_count: 0,
      version: 1,
      assignment_id: input.assignment_id ?? null,
      created_at: new Date().toISOString(),
      chunks,
    })
    logOp('上传了教学资料', title)
    persist()
    scheduleIndexing(id)
    return {
      id,
      title,
      category: input.category || '未分类',
      file_type: (input.file.name.split('.').pop() ?? 'pdf').toLowerCase(),
      size_bytes: input.file.size || 120_000,
      status: 'indexing',
      chunk_count: 0,
      version: 1,
      assignment_id: input.assignment_id ?? null,
      created_at: new Date().toISOString(),
    }
  },
  async remove(id) {
    const state = demoState()
    const doc = state.knowledge.find((d) => d.id === id)
    if (
      !doc ||
      (doc.assignment_id && !visibleAssignments().some((a) => a.id === doc.assignment_id))
    )
      throw demoError(404, '资料不存在。')
    state.knowledge = state.knowledge.filter((d) => d.id !== id)
    logOp('删除了教学资料', doc.title)
    persist()
  },
  async retrieve(query) {
    const terms = [
      ...new Set(
        (query.toLowerCase().match(/[a-z0-9]+|[\u4e00-\u9fff]+/g) ?? []).flatMap((word) =>
          /^[a-z0-9]+$/.test(word)
            ? [word]
            : Array.from({ length: Math.max(0, word.length - 1) }, (_, i) => word.slice(i, i + 2)),
        ),
      ),
    ]
    if (!terms.length) return []
    const hits: RetrievalHit[] = []
    for (const doc of demoState().knowledge) {
      if (
        doc.status !== 'indexed' ||
        (doc.assignment_id && !visibleAssignments().some((a) => a.id === doc.assignment_id))
      )
        continue
      for (const chunk of doc.chunks) {
        const score = terms.reduce(
          (sum, term) => sum + (chunk.text.toLowerCase().includes(term) ? 1 : 0),
          0,
        )
        if (score > 0)
          hits.push({
            chunk_id: chunk.id,
            doc_id: doc.id,
            doc_title: doc.title,
            score,
            text: chunk.text,
          })
      }
    }
    return hits.sort((a, b) => b.score - a.score).slice(0, 5)
  },
}

/* ---------------- Assistant ---------------- */

function sleep(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('已中断', 'AbortError'))
    const timer = window.setTimeout(resolve, ms)
    signal?.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer)
        reject(new DOMException('已中断', 'AbortError'))
      },
      { once: true },
    )
  })
}

const assistant: AssistantService = {
  async conversations() {
    const state = demoState()
    return state.conversations
      .slice()
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .map((c) => ({
        id: c.id,
        title: c.title,
        updated_at: c.updated_at,
        message_count: c.messages.length,
      }))
  },
  async messages(conversationId) {
    const conversation = demoState().conversations.find((c) => c.id === conversationId)
    if (!conversation) throw demoError(404, '会话不存在。')
    return conversation.messages
  },
  async send(conversationId, content, signal) {
    const state = demoState()
    const conversation = state.conversations.find((c) => c.id === conversationId)
    if (!conversation) throw demoError(404, '会话不存在。')
    const text = content.trim()
    if (!text) throw demoError(422, '请输入问题。')
    conversation.messages.push({
      id: nextId('msg'),
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    })
    if (conversation.title === '新对话') conversation.title = text.slice(0, 18)
    conversation.updated_at = new Date().toISOString()
    persist()
    const userMessageId = conversation.messages.at(-1)!.id
    let answer: AssistantMessage
    try {
      await sleep(500, signal)
      const hits = await knowledge.retrieve(text)
      const toolCalls = [
        {
          name: '检索教学资料',
          status: 'done' as const,
          summary: hits.length ? `命中 ${hits.length} 段相关资料` : '未命中相关资料',
        },
      ]
      await sleep(700, signal)
      if (!hits.length) {
        answer = {
          id: nextId('msg'),
          role: 'assistant',
          content:
            '知识库中暂未找到与该问题直接相关的资料。按照“无证据不回答”的规则，我不能在没有依据的情况下编造教学内容。\n\n建议：先在「教学知识库」上传相关讲义或资料，之后再向我提问，我会基于资料给出可溯源的回答。',
          created_at: new Date().toISOString(),
          toolCalls,
          citations: [],
        }
      } else {
        const lines = hits.map(
          (hit, index) => `[${index + 1}] 《${hit.doc_title}》：${hit.text.slice(0, 60)}…`,
        )
        answer = {
          id: nextId('msg'),
          role: 'assistant',
          content: `根据知识库检索到的资料，整理如下：\n\n${lines.join('\n')}\n\n以上内容均来自知识库已索引资料，点击引用标记可查看原文出处。`,
          created_at: new Date().toISOString(),
          toolCalls,
          citations: hits.map((hit, index) => ({
            index: index + 1,
            doc_id: hit.doc_id,
            doc_title: hit.doc_title,
            chunk_id: hit.chunk_id,
            quote: hit.text.slice(0, 40),
          })),
        }
      }
    } catch (e) {
      conversation.messages = conversation.messages.filter((m) => m.id !== userMessageId)
      persist()
      throw e
    }
    conversation.messages.push(answer)
    conversation.updated_at = new Date().toISOString()
    persist()
    return answer
  },
  async newConversation() {
    const state = demoState()
    const conversation = {
      id: nextId('conv'),
      title: '新对话',
      updated_at: new Date().toISOString(),
      messages: [],
    }
    state.conversations.unshift(conversation)
    persist()
    return {
      id: conversation.id,
      title: conversation.title,
      updated_at: conversation.updated_at,
      message_count: 0,
    }
  },
  async removeConversation(id) {
    const state = demoState()
    state.conversations = state.conversations.filter((c) => c.id !== id)
    persist()
  },
}

/* ---------------- Insights ---------------- */

const insights: InsightsService = {
  async summary(range) {
    const state = demoState()
    const visible = visibleAssignments()
    const oldest = Math.min(
      Date.now(),
      ...state.submissions
        .filter((s) => visible.some((a) => a.id === s.assignment_id))
        .map((s) => new Date(s.submitted_at).getTime()),
    )
    const days =
      range === '7d'
        ? 7
        : range === '30d'
          ? 30
          : Math.min(365, Math.ceil((Date.now() - oldest) / 86400000) + 1)
    const since = Date.now() - days * 86_400_000
    const submissionsAll = state.submissions.filter(
      (s) =>
        visible.some((a) => a.id === s.assignment_id) &&
        (range === 'all' || new Date(s.submitted_at).getTime() >= since),
    )
    const series: { date: string; submissions: number; reviews: number }[] = []
    for (let i = days - 1; i >= 0; i--) {
      const day = new Date(Date.now() - i * 86_400_000)
      const key = day.toISOString().slice(0, 10)
      series.push({
        date: key,
        submissions: submissionsAll.filter((s) => s.submitted_at.slice(0, 10) === key).length,
        reviews: Object.entries(state.reviews)
          .filter(([reviewKey]) => submissionsAll.some((s) => reviewKey.startsWith(s.id + ':')))
          .flatMap(([, r]) => r.history)
          .filter((h) => h.created_at.slice(0, 10) === key).length,
      })
    }
    const workload = visible.map((a) => {
      const rows = submissionsAll.filter((s) => s.assignment_id === a.id)
      const questions = assignmentQuestions(a.id)
      let reviewed = 0
      for (const s of rows) {
        if (!state.canonicals[s.id]) continue
        for (const q of questions) if (state.reviews[`${s.id}:${q.id}`]?.history.length) reviewed++
      }
      const total = rows.filter((s) => state.canonicals[s.id]).length * questions.length
      return {
        assignment_title: a.title,
        submissions: rows.length,
        reviewed,
        pending: total - reviewed,
      }
    })
    const proposals = state.proposals.filter((p) => {
      const submission = state.submissions.find((s) => s.id === p.submission_id)
      return submission && submissionsAll.some((s) => s.id === submission.id)
    })
    return {
      range_label: range === '7d' ? '近 7 天' : range === '30d' ? '近 30 天' : '全部',
      scope_note: '演示数据：按当前可访问作业的提交批次统计，趋势图最多展示365天（合成数据）。',
      assignments_total: visible.length,
      submissions_total: submissionsAll.length,
      submissions_recent: submissionsAll.filter((s) => new Date(s.submitted_at).getTime() >= since)
        .length,
      parse_succeeded: submissionsAll.filter((s) => s.parsing.status === 'succeeded').length,
      parse_failed: submissionsAll.filter((s) => s.parsing.status === 'failed').length,
      parse_pending: submissionsAll.filter((s) =>
        ['pending', 'running', 'retry_wait'].includes(s.parsing.status),
      ).length,
      review_done: Object.keys(state.reviews).filter((key) => {
        const [sid] = key.split(':')
        return (
          submissionsAll.some((s) => s.id === sid) && (state.reviews[key]?.history.length ?? 0) > 0
        )
      }).length,
      review_pending: workload.reduce((sum, w) => sum + w.pending, 0),
      grading_reviewed: proposals.filter((p) => p.reviews.length).length,
      grading_pending: proposals.filter((p) => !p.reviews.length).length,
      series,
      workload,
    }
  },
}

/* ---------------- System ---------------- */

const system: SystemService = {
  async modules() {
    return [
      { key: 'api', name: '应用服务', status: 'ok', detail: '演示环境运行正常' },
      { key: 'identity', name: '身份服务', status: 'ok', detail: '演示 IdP（合成）' },
      { key: 'storage', name: '对象存储', status: 'ok', detail: '演示存储（合成）' },
      {
        key: 'ai',
        name: 'AI 服务',
        status: 'degraded',
        detail: '演示环境模拟：评分模型配额接近上限',
      },
      { key: 'parsing', name: '文档解析', status: 'ok', detail: '演示解析器（合成）' },
      { key: 'retrieval', name: '检索服务', status: 'ok', detail: '演示检索（关键词匹配）' },
    ]
  },
  async health() {
    return { status: 'ok' }
  },
}

function guard<T extends object>(provider: T): T {
  return new Proxy(provider, {
    get(target, key, receiver) {
      const fn = Reflect.get(target, key, receiver)
      return typeof fn === 'function'
        ? async (...args: unknown[]) => {
            currentUser()
            return Reflect.apply(fn, target, args)
          }
        : fn
    },
  })
}
export function createMockServices(): HuipiServices {
  for (const sub of demoState().submissions)
    if (sub.parsing.status === 'running') scheduleParseTransition(sub.id)
  for (const doc of demoState().knowledge) if (doc.status === 'indexing') scheduleIndexing(doc.id)
  return {
    me: guard(me),
    assignments: guard(assignments),
    submissions: guard(submissions),
    reviews: guard(reviews),
    grading: guard(grading),
    admin: guard(admin),
    knowledge: guard(knowledge),
    assistant: guard(assistant),
    insights: guard(insights),
    system: guard(system),
  }
}
