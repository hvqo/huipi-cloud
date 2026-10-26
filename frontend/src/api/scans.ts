import { request, id, type Schema } from './client'
export const scans = {
  sessions: (offset = 0, sid = '') =>
    request<Schema['SessionPage']>(
      `/api/v1/scan-sessions?offset=${offset}&limit=20${sid ? `&exam_session_id=${id(sid)}` : ''}`,
    ),
  batches: (sid: string, offset = 0) =>
    request<Schema['BatchPage']>(
      `/api/v1/exam-sessions/${id(sid)}/scan-batches?offset=${offset}&limit=20`,
    ),
  create: (sid: string, requestId: string, title: string) =>
    request<Schema['BatchRead']>(`/api/v1/exam-sessions/${id(sid)}/scan-batches`, {
      body: JSON.stringify({ request_id: requestId, title }),
    }),
  files: (bid: string) => request<Schema['FilePage']>(`/api/v1/scan-batches/${id(bid)}/files`),
  upload: (bid: string, requestId: string, file: File) => {
    const body = new FormData()
    body.set('request_id', requestId)
    body.set('file', file)
    return request<Schema['FileRead']>(`/api/v1/scan-batches/${id(bid)}/files`, {
      body,
      timeoutMs: 180000,
    })
  },
  pages: (fid: string, offset: number) =>
    request<Schema['PagePage']>(`/api/v1/scan-files/${id(fid)}/pages?offset=${offset}&limit=20`),
  preview: (pid: string) =>
    request<Blob>(`/api/v1/scan-pages/${id(pid)}/preview`, {
      blob: true,
      maxBytes: 2 * 1024 * 1024,
    }),
  original: (fid: string) =>
    request<Blob>(`/api/v1/scan-files/${id(fid)}/original`, {
      blob: true,
      maxBytes: 33 * 1024 * 1024,
      timeoutMs: 60000,
    }),
}
