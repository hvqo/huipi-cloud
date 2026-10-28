import { id, request, type Schema } from './client'

export interface AttendanceRow {
  participation_id: string
  student_name: string
  student_code: string
  exam_number: string | null
  class_id: string
  class_name: string
  status: 'present' | 'absent' | 'unverified'
  revision: number
  reason: string | null
}
export interface Version {
  id: string
  version_no: number
  revision: number
  status: 'pending' | 'approved' | 'published' | 'cancelled'
  reason: string
  snapshot_sha256: string
  supersedes_id: string | null
  created_at: string
  published_at: string | null
}
export interface FinalRead {
  version: Pick<Version, 'id' | 'version_no' | 'status' | 'published_at' | 'supersedes_id'>
  result: Omit<Schema['Preview'], 'published'> & { published: boolean }
  statistics: {
    completion_definition: string
    classes: { class_id: string; class_name: string; total: number; present: number; absent: number; missing_reviews: number; completion_rate: string }[]
    papers: { class_id: string; class_name: string; paper_version_id: string; variant: string; max_points: string; count: number; average: string; highest: string; lowest: string; distribution: Record<string, number>; questions: { mapping_id: string; number: number; score_rate: string }[] }[]
  } | null
}
const root = (sid: string) => `/api/v1/exam-sessions/${id(sid)}/final-results`
export const finalResults = {
  attendance: (sid: string, classId = '') => request<{ items: AttendanceRow[]; roster_revision_id: string }>(`${root(sid)}/attendance${classId ? `?class_id=${id(classId)}` : ''}`),
  versions: (sid: string) => request<{ items: Version[]; has_more: boolean }>(`${root(sid)}/versions`),
  read: (sid: string, version: string, classId = '') => request<FinalRead>(`${root(sid)}/versions/${id(version)}${classId ? `?class_id=${id(classId)}` : ''}`, { timeoutMs: 65000, maxBytes: 4 * 1024 * 1024 }),
  export: (sid: string, version: string, classId = '') => request<Blob>(`${root(sid)}/versions/${id(version)}/export.xlsx${classId ? `?class_id=${id(classId)}` : ''}`, { blob: true, timeoutMs: 65000, maxBytes: 8 * 1024 * 1024 }),
  mutate: (path: string, body: object) => request<Record<string, unknown>>(path, { body: JSON.stringify(body), timeoutMs: 65000, errorCode: true }),
  root,
}
