import { id, request } from './client'
export interface MarkTask {
  task_id: string
  workspace_id: string
  job_id: string
  question_number: number
  student_name: string
  student_code: string
  class_name: string
  assignee_user_id: string
  status: string
  stored_status: string
  revision: number
  generation: number
  review_id: string | null
}
export interface TaskIndex {
  id: string
  revision: number
  status: string
  assignee_user_id: string
}
export interface PoolQuestion {
  question_id: string
  number: number
  workspace_id: string | null
  task: TaskIndex | null
}
export interface PoolScript {
  job_id: string
  status: string
  student_name?: string
  student_code?: string
  class_name?: string
  variant?: string
  questions: PoolQuestion[]
}
export const marking = {
  sessions: (offset = 0) =>
    request<{ items: { id: string; title: string; event_title: string }[]; has_next: boolean }>(
      `/api/v1/marking-sessions?offset=${offset}`,
    ),
  pool: (sid: string, offset = 0) =>
    request<{ items: PoolScript[]; has_next: boolean }>(
      `/api/v1/exam-sessions/${id(sid)}/marking-pool?offset=${offset}`,
      { timeoutMs: 100000 },
    ),
  teachers: (sid: string) =>
    request<{ items: { user_id: string; label: string }[] }>(
      `/api/v1/exam-sessions/${id(sid)}/marking-teachers`,
    ),
  progress: (sid: string, offset = 0) =>
    request<{
      items: MarkTask[]
      has_next: boolean
      counts: { assignee_user_id: string; status: string; count: number }[]
      counts_note: string
    }>(`/api/v1/exam-sessions/${id(sid)}/marking-progress?offset=${offset}`, { timeoutMs: 100000 }),
  mine: (offset = 0) =>
    request<{ items: MarkTask[]; has_next: boolean; counts: { status: string; count: number }[] }>(
      `/api/v1/my-marking-tasks?offset=${offset}`,
      {
        timeoutMs: 100000,
      },
    ),
  task: (tid: string) =>
    request<MarkTask>(`/api/v1/marking-tasks/${id(tid)}`, { timeoutMs: 100000 }),
  preview: (tid: string, pid: string) =>
    request<Blob>(`/api/v1/marking-tasks/${id(tid)}/pages/${id(pid)}/preview`, {
      blob: true,
      maxBytes: 2 * 1024 ** 2,
    }),
  write: (path: string, body: string) => {
    if (
      !/^\/(exam-sessions\/[a-f0-9-]+\/(marking-assignments|marking-jobs\/[a-f0-9-]+\/prepare)|marking-tasks\/[a-f0-9-]+\/(claim|complete|cancel))$/.test(
        path,
      )
    )
      throw new Error('无效阅卷操作')
    return request<Record<string, unknown>>('/api/v1' + path, {
      body,
      timeoutMs: 100000,
      errorCode: true,
    })
  },
}
export const taskStatus: Record<string, string> = {
  assigned: '待领取',
  in_progress: '批改中',
  completed: '已完成',
  cancelled: '已撤回',
  superseded: '已重派',
  invalidated: '来源或授权已失效',
}
