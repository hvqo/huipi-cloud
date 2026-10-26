import { reactive } from 'vue'
import { id, request, type Schema } from './client'
export type Access = Schema['SchoolAccess']
export const schoolAccess = reactive<Access>({
  can_read: false,
  can_manage: false,
  can_preview: false,
  can_confirm: false,
})
export function resetSchoolAccess() {
  Object.assign(schoolAccess, {
    can_read: false,
    can_manage: false,
    can_preview: false,
    can_confirm: false,
  })
}
export const loadSchoolAccess = (signal?: AbortSignal) =>
  request<Access>('/api/v1/me/school-access', { signal, errorCode: true })
export type Kind = 'academic-years' | 'academic-terms' | 'grade-cohorts' | 'subjects' | 'classes'
const kinds: Kind[] = ['academic-years', 'academic-terms', 'grade-cohorts', 'subjects', 'classes']
const base = '/api/v1/organization'
const query = (values: Record<string, string | number | boolean | undefined>) => {
  const q = new URLSearchParams()
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== '') q.set(key, String(value))
  return '?' + q.toString()
}
const get = <T>(path: string, signal?: AbortSignal) =>
  request<T>(base + path, { signal, errorCode: true })
export const school = {
  years: (offset = 0, signal?: AbortSignal) =>
    get<Schema['AcademicYearPage']>('/academic-years' + query({ offset, limit: 25 }), signal),
  terms: (year: string, signal?: AbortSignal) =>
    get<Schema['AcademicTermPage']>(
      '/academic-terms' + query({ academic_year_id: year, limit: 100 }),
      signal,
    ),
  cohorts: (offset = 0, signal?: AbortSignal) =>
    get<Schema['GradeCohortPage']>('/grade-cohorts' + query({ offset, limit: 25 }), signal),
  subjects: (offset = 0, signal?: AbortSignal) =>
    get<Schema['SubjectPage']>('/subjects' + query({ offset, limit: 25 }), signal),
  classes: (year: string, cohort: string, date: string, offset = 0, signal?: AbortSignal) =>
    get<Schema['SchoolClassPage']>(
      '/class-summaries' +
        query({ academic_year_id: year, cohort_id: cohort, as_of: date, offset, limit: 25 }),
      signal,
    ),
  students: (
    cls: string,
    text: string,
    date: string,
    offset = 0,
    signal?: AbortSignal,
    year?: string,
    cohort?: string,
  ) =>
    get<Schema['StudentPage']>(
      '/students' +
        query({
          class_id: cls,
          query: text,
          as_of: date,
          academic_year_id: year,
          cohort_id: cohort,
          offset,
          limit: 25,
        }),
      signal,
    ),
  enrollments: (student: string, offset = 0, signal?: AbortSignal) =>
    get<Schema['ClassEnrollmentPage']>(
      `/students/${id(student)}/enrollments` + query({ limit: 25, offset }),
      signal,
    ),
  history: (offset = 0, signal?: AbortSignal) =>
    get<Schema['StudentImportBatchPage']>(
      '/student-imports' + query({ offset, limit: 25 }),
      signal,
    ),
  batch: (batch: string, signal?: AbortSignal) =>
    get<Schema['StudentImportPreviewResult']>(`/student-imports/${id(batch)}`, signal),
  rows: (batch: string, offset = 0, signal?: AbortSignal) =>
    get<Schema['StudentImportRowPage']>(
      `/student-imports/${id(batch)}/rows` + query({ offset, limit: 25 }),
      signal,
    ),
  template: (mode: Schema['StudentImportPreviewResult']['mode'], signal?: AbortSignal) =>
    request<Blob>(base + '/student-import-template' + query({ mode }), {
      signal,
      blob: true,
      maxBytes: 1024,
      errorCode: true,
    }),
  create: (kind: Kind, body: string, signal?: AbortSignal) => {
    if (!kinds.includes(kind)) throw new Error('无效组织类型。')
    return request<Schema['MutationResult']>(base + '/' + kind, {
      body,
      signal,
      errorCode: true,
      timeoutMs: 35000,
    })
  },
  preview: (body: FormData, signal?: AbortSignal) =>
    request<Schema['StudentImportPreviewResult']>(base + '/student-imports', {
      body,
      signal,
      errorCode: true,
      timeoutMs: 35000,
    }),
  confirm: (batch: string, body: string, signal?: AbortSignal) =>
    request<Schema['StudentImportConfirmResult']>(base + `/student-imports/${id(batch)}/confirm`, {
      body,
      signal,
      errorCode: true,
      timeoutMs: 35000,
    }),
}
