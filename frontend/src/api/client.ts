import { accessToken, expireAuth } from '../auth'
import type { components } from './generated'
export type Schema = components['schemas']
export type Review = Schema['ReviewInspection']
export type ReviewBody = Schema['ReviewRequest']
export type Region = Schema['ReviewRegionSelection']
export type Node = Schema['CanonicalContentNode']
export type Page = Schema['CanonicalPageRead']
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public uncertain = false,
  ) {
    super(
      (
        {
          401: '登录已失效，请重新登录。',
          403: '当前身份不能执行此操作。',
          404: '资源不可用或没有访问权限。',
          409: '来源或审核版本发生变化，请刷新并重新核对。',
          422: '输入或证据范围不符合要求。',
          413: '内容超过安全读取上限。',
          503: '服务暂不可用；保存结果可能尚未确认。',
        } as Record<number, string>
      )[status] || '请求未完成，请稍后重试。',
    )
  }
}
export async function readBounded(response: Response, maximum: number) {
  const reader = response.body?.getReader()
  if (!reader) return new Uint8Array()
  const chunks: Uint8Array[] = []
  let length = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      length += value.byteLength
      if (length > maximum) throw new ApiError(413, 'response_limit')
      chunks.push(value)
    }
  } catch (error) {
    await reader.cancel()
    throw error
  } finally {
    reader.releaseLock()
  }
  const data = new Uint8Array(length)
  let offset = 0
  for (const chunk of chunks) {
    data.set(chunk, offset)
    offset += chunk.byteLength
  }
  return data
}
export async function request<T>(
  path: string,
  options: {
    body?: string
    signal?: AbortSignal
    blob?: boolean
    form?: FormData
    method?: string
  } = {},
): Promise<T> {
  if (!/^\/api\/v1\//.test(path) || path.includes('://') || path.includes('\\'))
    throw new ApiError(400, 'invalid_api_path')
  const token = accessToken()
  if (!token) throw new ApiError(401, 'authentication_required')
  const controller = new AbortController()
  const cancel = () => controller.abort()
  options.signal?.addEventListener('abort', cancel, { once: true })
  if (options.signal?.aborted) cancel()
  const timer = setTimeout(cancel, options.blob ? 30000 : 15000)
  let accepted = false
  const wrote =
    !!options.body || !!options.form || (options.method !== undefined && options.method !== 'GET')
  const method = options.method || (wrote ? 'POST' : 'GET')
  try {
    const response = await fetch(path, {
      method,
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(options.body ? { body: options.body } : {}),
      ...(options.form ? { body: options.form } : {}),
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
    })
    accepted = response.ok
    if (response.status === 401) {
      await expireAuth()
      throw new ApiError(401, 'invalid_token')
    }
    if (!response.ok)
      throw new ApiError(response.status, 'api_error', wrote && response.status >= 500)
    if (response.status === 204 || method === 'DELETE') return undefined as T
    const bytes = await readBounded(response, options.blob ? 21 * 1024 * 1024 : 2 * 1024 * 1024)
    return (
      options.blob
        ? new Blob([bytes.buffer as ArrayBuffer], {
            type: response.headers.get('content-type') || 'application/octet-stream',
          })
        : JSON.parse(new TextDecoder().decode(bytes))
    ) as T
  } catch (error) {
    if (error instanceof ApiError) {
      if (wrote && accepted) error.uncertain = true
      throw error
    }
    throw new ApiError(503, 'network_or_timeout', wrote)
  } finally {
    clearTimeout(timer)
    options.signal?.removeEventListener('abort', cancel)
  }
}
export const id = (value: string) => encodeURIComponent(value)
export const api = {
  assignments: (offset = 0, signal?: AbortSignal) =>
    request<Schema['AssignmentSummary'][]>(`/api/v1/assignments?limit=12&offset=${offset}`, {
      signal,
    }),
  create: (body: Schema['AssignmentCreate']) =>
    request<Schema['AssignmentDetail']>('/api/v1/assignments', { body: JSON.stringify(body) }),
  workbench: (aid: string, offset = 0, signal?: AbortSignal, questionId?: string) =>
    request<Schema['AssignmentWorkbench']>(
      `/api/v1/assignments/${id(aid)}/workbench?limit=20&offset=${offset}${questionId ? `&question_id=${id(questionId)}` : ''}`,
      { signal },
    ),
  submissions: (aid: string, offset = 0, signal?: AbortSignal) =>
    request<Schema['SubmissionRead'][]>(
      `/api/v1/assignments/${id(aid)}/submissions?limit=12&offset=${offset}`,
      { signal },
    ),
  submission: (sid: string, signal?: AbortSignal) =>
    request<Schema['SubmissionRead']>(`/api/v1/submissions/${id(sid)}`, { signal }),
  parsing: (sid: string, signal?: AbortSignal) =>
    request<Schema['ParsingTaskStatusRead']>(`/api/v1/submissions/${id(sid)}/parsing-task`, {
      signal,
    }),
  alignment: (sid: string, signal?: AbortSignal) =>
    request<Schema['AlignmentSummaryRead']>(`/api/v1/submissions/${id(sid)}/answer-alignment`, {
      signal,
    }),
  questionAlignment: (sid: string, qid: string, signal?: AbortSignal) =>
    request<Schema['QuestionAlignmentRead']>(
      `/api/v1/submissions/${id(sid)}/answer-alignment/questions/${id(qid)}`,
      { signal },
    ),
  canonical: (sid: string, signal?: AbortSignal) =>
    request<Schema['CanonicalDocumentStatusRead']>(
      `/api/v1/submissions/${id(sid)}/canonical-document`,
      { signal },
    ),
  page: (sid: string, page: number, signal?: AbortSignal) =>
    request<Page>(`/api/v1/submissions/${id(sid)}/canonical-document/pages/${page}`, { signal }),
  file: (sid: string, signal?: AbortSignal) =>
    request<Blob>(`/api/v1/submissions/${id(sid)}/file`, { blob: true, signal }),
  review: (sid: string, qid: string, offset = 0, signal?: AbortSignal, confirmedOffset = 0) =>
    request<Review>(
      `/api/v1/submissions/${id(sid)}/answer-reviews/questions/${id(qid)}?evidence_limit=50&evidence_offset=${offset}&confirmed_limit=50&confirmed_offset=${confirmedOffset}`,
      { signal },
    ),
  save: (sid: string, qid: string, body: string) =>
    request<Schema['ReviewWriteResult']>(
      `/api/v1/submissions/${id(sid)}/answer-reviews/questions/${id(qid)}`,
      { body },
    ),
  history: (sid: string, qid: string, offset = 0, signal?: AbortSignal) =>
    request<Schema['ReviewHistory']>(
      `/api/v1/submissions/${id(sid)}/answer-reviews?question_id=${id(qid)}&limit=10&offset=${offset}`,
      { signal },
    ),

  /* ---- 以下为教师工作台扩展端点（均来自 OpenAPI 生成合同） ---- */
  me: (signal?: AbortSignal) => request<Schema['CurrentUser']>('/api/v1/me', { signal }),
  health: (signal?: AbortSignal) => request<{ status: string }>('/api/v1/health', { signal }),
  assignmentDetail: (aid: string, signal?: AbortSignal) =>
    request<Schema['AssignmentDetail']>(`/api/v1/assignments/${id(aid)}`, { signal }),
  publishAssignment: (aid: string) =>
    request<Schema['AssignmentDetail']>(`/api/v1/assignments/${id(aid)}/publish`, {
      body: '{}',
    }),
  addQuestion: (aid: string, body: Schema['QuestionCreate']) =>
    request<Schema['QuestionRead']>(`/api/v1/assignments/${id(aid)}/questions`, {
      body: JSON.stringify(body),
    }),
  replaceAnswerKey: (qid: string, body: Schema['AnswerKeyReplace']) =>
    request<Schema['AnswerKeyRead']>(`/api/v1/questions/${id(qid)}/answer-key`, {
      body: JSON.stringify(body),
      method: 'PUT',
    }),
  replaceRubric: (qid: string, body: Schema['RubricReplace']) =>
    request<Schema['RubricRead']>(`/api/v1/questions/${id(qid)}/rubric`, {
      body: JSON.stringify(body),
      method: 'PUT',
    }),
  createSubmission: (aid: string, input: { student_ref: string; file: File }) => {
    const form = new FormData()
    form.append('student_ref', input.student_ref)
    form.append('file', input.file)
    return request<Schema['SubmissionRead']>(`/api/v1/assignments/${id(aid)}/submissions`, { form })
  },

  /* ---- 管理员端点（docs/product/teacher-admin-contract.md） ---- */
  teacherPage: (limit = 25, offset = 0, signal?: AbortSignal) =>
    request<Schema['TeacherPage']>(`/api/v1/admin/teachers?limit=${limit}&offset=${offset}`, {
      signal,
    }),
  registerTeacher: (body: Schema['TeacherRegistration']) =>
    request<Schema['AdminOperationResult']>('/api/v1/admin/teachers', {
      body: JSON.stringify(body),
    }),
  setTeacherActive: (uid: string, body: Schema['TeacherActivation']) =>
    request<Schema['AdminOperationResult']>(`/api/v1/admin/teachers/${id(uid)}`, {
      body: JSON.stringify(body),
      method: 'PATCH',
    }),
  teacherMemberships: (uid: string, offset = 0, signal?: AbortSignal) =>
    request<Schema['MembershipPage']>(
      `/api/v1/admin/teachers/${id(uid)}/memberships?limit=50&offset=${offset}`,
      { signal },
    ),
  grantMembership: (aid: string, uid: string, body: Schema['MembershipGrant']) =>
    request<Schema['MembershipWriteResult']>(`/api/v1/assignments/${id(aid)}/members/${id(uid)}`, {
      body: JSON.stringify(body),
      method: 'PUT',
    }),
  revokeMembership: (aid: string, uid: string, requestId: string) =>
    request<void>(`/api/v1/assignments/${id(aid)}/members/${id(uid)}?request_id=${id(requestId)}`, {
      method: 'DELETE',
    }),
  auditEvents: (
    query: {
      target_user_id?: string
      assignment_id?: string
      request_id?: string
      limit?: number
      offset?: number
    },
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams()
    if (query.target_user_id) params.set('target_user_id', query.target_user_id)
    if (query.assignment_id) params.set('assignment_id', query.assignment_id)
    if (query.request_id) params.set('request_id', query.request_id)
    params.set('limit', String(query.limit ?? 25))
    params.set('offset', String(query.offset ?? 0))
    return request<Schema['AdminAuditPage']>(`/api/v1/admin/audit-events?${params}`, { signal })
  },

  /* ---- 候选评分审核端点（docs/development/grade-review-http.md） ---- */
  gradeInspect: (sid: string, qid: string, pid: string, evidenceOffset = 0, signal?: AbortSignal) =>
    request<Schema['GradeReviewHTTPInspection']>(
      `/api/v1/submissions/${id(sid)}/questions/${id(qid)}/grade-proposals/${id(pid)}/review?evidence_limit=10&evidence_offset=${evidenceOffset}`,
      { signal },
    ),
  gradeHistory: (sid: string, qid: string, pid: string, offset = 0, signal?: AbortSignal) =>
    request<Schema['GradeReviewHTTPHistory']>(
      `/api/v1/submissions/${id(sid)}/questions/${id(qid)}/grade-proposals/${id(pid)}/reviews?limit=5&offset=${offset}`,
      { signal },
    ),
  gradeSave: (sid: string, qid: string, pid: string, body: string) =>
    request<Schema['GradeReviewHTTPWriteResult']>(
      `/api/v1/submissions/${id(sid)}/questions/${id(qid)}/grade-proposals/${id(pid)}/reviews`,
      { body },
    ),
}
