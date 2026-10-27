import { id, request, type Schema } from './client'

export const omr = {
  paper: (pid: string) => request<Schema['TemplateRead']>(`/api/v1/omr/papers/${id(pid)}`),
  create: (pid: string, body: Schema['TemplateCreate']) =>
    request<Schema['WriteReceipt']>(`/api/v1/omr/papers/${id(pid)}`, {
      body: JSON.stringify(body),
    }),
  print: (tid: string) =>
    request<Blob>(`/api/v1/omr/templates/${id(tid)}/print.pdf`, { blob: true }),
  current: (sid: string) =>
    request<Schema['CurrentRecognition']>(`/api/v1/answer-scripts/${id(sid)}/omr`),
  recognize: (sid: string, body: Schema['RecognitionCreate']) =>
    request<Schema['WriteReceipt']>(`/api/v1/answer-scripts/${id(sid)}/omr`, {
      body: JSON.stringify(body),
      timeoutMs: 120000,
      errorCode: true,
    }),
  inspect: (rid: string) =>
    request<Schema['RecognitionRead']>(`/api/v1/omr/recognitions/${id(rid)}`),
  image: (rid: string, overlay = true) =>
    request<Blob>(`/api/v1/omr/recognitions/${id(rid)}/image?overlay=${overlay}`, {
      blob: true,
      maxBytes: 2 * 1024 * 1024,
    }),
  confirm: (rid: string, body: Schema['ConfirmRequest']) =>
    request<Schema['WriteReceipt']>(`/api/v1/omr/recognitions/${id(rid)}/confirm`, {
      body: JSON.stringify(body),
      timeoutMs: 120000,
    }),
}
