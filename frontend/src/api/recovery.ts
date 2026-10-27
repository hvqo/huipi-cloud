import { id, request, type Schema } from './client'
export type RecoveryItem = Schema['RecoveryItem']
export type RecoveryView = Schema['RecoveryView']
export const recovery = {
  sessions: (offset = 0) =>
    request<{ items: { id: string; title: string; event_title: string }[]; has_next: boolean }>(
      `/api/v1/recovery-sessions?offset=${offset}`,
    ),
  inspect: (sid: string, offset = 0, kind = '', summary = false) =>
    request<RecoveryView>(
      `/api/v1/exam-sessions/${id(sid)}/exceptions?offset=${offset}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}${summary ? '&summary=true' : ''}`,
      { timeoutMs: 30000 },
    ),
}
export const exceptionNames: Record<string, string> = {
  upload_failed: '文件接收失败',
  upload_interrupted: '接收中断',
  unclaimed_page: '未认领扫描页',
  duplicate_page_check: '疑似重复页',
  identity_pending: '身份待确认',
  incomplete_script: '缺页/页序待核对',
  source_changed: '来源已变化',
  parsing_failed: '解析失败',
  parsing_invalidated: '解析失效',
  parsing_not_started: '待启动解析',
  answer_unconfirmed: '作答区域待核对',
  answer_absent: '已核对未观察到作答',
  scoring_failed: 'AI评分失败',
  scoring_abstained: 'AI评分弃权',
  scoring_invalidated: '评分失效',
  task_invalidated: '阅卷任务失效',
}
