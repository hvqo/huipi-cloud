import { request, id, type Schema } from './client'
export const examParsing = {
  start: (aid: string, body: Schema['ParseRequest']) =>
    request<Schema['ParseReceipt']>(`/api/v1/answer-scripts/${id(aid)}/parsing`, {
      body: JSON.stringify(body),
      timeoutMs: 100000,
    }),
  current: (aid: string) =>
    request<Schema['ParseStatus']>(`/api/v1/answer-scripts/${id(aid)}/parsing`),
  page: (jid: string, number: number) =>
    request<Schema['ParsePage']>(`/api/v1/exam-parsing-jobs/${id(jid)}/pages/${number}`, {
      timeoutMs: 100000,
    }),
}
