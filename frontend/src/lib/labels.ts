import { decisions, reasons } from './review'

/** 业务状态 → 自然中文标签的统一映射。普通教师界面不出现技术字段。 */

export const parsingLabels: Record<string, string> = {
  pending: '待解析',
  running: '解析中',
  retry_wait: '等待重试',
  succeeded: '解析完成',
  failed: '解析失败',
}

export const canonicalLabels: Record<string, string> = {
  not_generated: '尚未生成',
  failed: '生成失败',
  available: '已生成',
}

export const alignmentLabels: Record<string, string> = {
  not_generated: '尚未对齐',
  complete: '对齐完成',
  review_required: '需人工核对',
}

export const matchingLabels: Record<string, string> = {
  aligned: '已关联',
  review_required: '需核对',
  not_observed: '未观察到题号',
  unmatched: '未匹配',
}

export const presenceLabels = decisions

export const reasonLabels = reasons

export const questionTypeLabels: Record<string, string> = {
  single_choice: '单选题',
  multiple_choice: '多选题',
  fill_blank: '填空题',
  short_answer: '简答题',
  essay: '解答题',
}

export const assignmentStatusLabels: Record<string, string> = {
  draft: '草稿',
  published: '已发布',
}

export const adminActionLabels: Record<string, string> = {
  teacher_registered: '教师登记',
  teacher_activation_changed: '启停变更',
  assignment_membership_granted: '授予作业权限',
  assignment_membership_revoked: '撤销作业权限',
}

export const gradingStatusLabels: Record<string, string> = {
  completed_candidate: '已出候选',
  grading_abstained: '模型弃权',
}

export const sourceStatusLabels: Record<string, string> = {
  current: '来源有效',
  source_stale: '来源已过期',
  unavailable: '来源不可用',
}

export const reviewStatusLabels: Record<string, string> = {
  accepted: '已接受候选',
  revised: '人工修订',
  rejected: '已拒绝',
  regrading_required: '要求重新评分',
}

export const knowledgeStatusLabels: Record<string, string> = {
  pending: '等待处理',
  indexing: '索引中',
  indexed: '已索引',
  failed: '处理失败',
}

export const systemStatusLabels: Record<string, string> = {
  ok: '正常',
  degraded: '异常',
  unknown: '未知',
  not_integrated: '未接入',
}

export function label(map: Record<string, string>, value: string | null | undefined): string {
  if (value == null) return '—'
  return map[value] || value
}

/** 本地时间格式：10-17 14:02 */
export function formatDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 完整日期：2026-10-17 */
export function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 相对时间：刚刚 / 5 分钟前 / 2 小时前 / 昨天 18:07 / 10-14 */
export function formatRelative(value: string, now = Date.now()): string {
  const time = new Date(value).getTime()
  if (Number.isNaN(time)) return value
  const diff = now - time
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (diff < 172_800_000) return `昨天 ${formatDateTime(value).split(' ')[1]}`
  return formatDateTime(value).split(' ')[0]
}

/** 分数字符串展示：9.50 → 9.5；非法输入原样返回。 */
export function formatScore(value: string | null | undefined): string {
  if (value == null) return '—'
  const trimmed = value.trim()
  if (!/^\d+(\.\d+)?$/.test(trimmed)) return value
  return trimmed.replace(/\.?0+$/, '') || '0'
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${bytes} B`
}
