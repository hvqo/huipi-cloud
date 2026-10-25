import { id, request, type Schema } from './client'
export const examResults = {
  sessions: (event = '', offset = 0) =>
    request<Schema['SessionsRead']>(
      `/api/v1/exam-result-sessions?offset=${offset}${event ? `&exam_event_id=${id(event)}` : ''}`,
    ),
  preview: (sid: string, classId = '', offset = 0) =>
    request<Schema['Preview']>(
      `/api/v1/exam-sessions/${id(sid)}/results-preview?offset=${offset}&limit=20${classId ? `&class_id=${id(classId)}` : ''}`,
      { timeoutMs: 65000, maxBytes: 4 * 1024 * 1024 },
    ),
  export: (sid: string, classId = '') =>
    request<Blob>(
      `/api/v1/exam-sessions/${id(sid)}/results-preview.xlsx${classId ? `?class_id=${id(classId)}` : ''}`,
      { blob: true, timeoutMs: 65000, maxBytes: 8 * 1024 * 1024 },
    ),
}
