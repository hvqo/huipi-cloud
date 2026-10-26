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
    body?: string | FormData
    signal?: AbortSignal
    blob?: boolean
    timeoutMs?: number
    maxBytes?: number
    errorCode?: boolean
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
  const timer = setTimeout(
    cancel,
    Math.min(options.timeoutMs || (options.blob ? 30000 : 15000), 180000),
  )
  let accepted = false
  try {
    const response = await fetch(path, {
      method: options.body ? 'POST' : 'GET',
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(typeof options.body === 'string' ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(options.body ? { body: options.body } : {}),
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
    })
    accepted = response.ok
    if (response.status === 401) {
      await expireAuth()
      throw new ApiError(401, 'invalid_token')
    }
    if (!response.ok) {
      let code = 'api_error'
      if (options.errorCode) {
        try {
          const bytes = await readBounded(response, 8192)
          const data = JSON.parse(new TextDecoder().decode(bytes)) as { detail?: unknown }
          if (typeof data.detail === 'string' && /^[a-z0-9_]{1,80}$/.test(data.detail))
            code = data.detail
        } catch {
          /* Never display arbitrary error bodies or validation input. */
        }
      }
      throw new ApiError(response.status, code, !!options.body && response.status >= 500)
    }
    const bytes = await readBounded(
      response,
      Math.min(
        options.maxBytes || (options.blob ? 21 * 1024 * 1024 : 2 * 1024 * 1024),
        33 * 1024 * 1024,
      ),
    )
    return (
      options.blob
        ? new Blob([bytes.buffer as ArrayBuffer], {
            type: response.headers.get('content-type') || 'application/octet-stream',
          })
        : JSON.parse(new TextDecoder().decode(bytes))
    ) as T
  } catch (error) {
    if (error instanceof ApiError) {
      if (options.body && accepted) error.uncertain = true
      throw error
    }
    throw new ApiError(503, 'network_or_timeout', !!options.body)
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
}
