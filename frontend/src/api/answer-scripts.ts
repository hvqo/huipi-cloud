import { request, id, type Schema } from './client'
const session = (sid: string) => `/api/v1/exam-sessions/${id(sid)}`
export const scripts = {
  pages: (sid: string, offset: number, unclaimed: boolean, batchId: string) =>
    request<Schema['SourcePageList']>(
      `${session(sid)}/script-pages?offset=${offset}&limit=20&unclaimed=${unclaimed}${batchId ? `&batch_id=${id(batchId)}` : ''}`,
    ),
  list: (sid: string, offset = 0) =>
    request<Schema['ScriptList']>(`${session(sid)}/answer-scripts?offset=${offset}&limit=10`),
  candidates: (sid: string, query: string, offset = 0) =>
    request<Schema['CandidateList']>(
      `${session(sid)}/identity-candidates?q=${encodeURIComponent(query)}&offset=${offset}&limit=20`,
    ),
  create: (sid: string, body: Schema['ScriptCreate']) =>
    request<Schema['ScriptReceipt']>(`${session(sid)}/answer-scripts`, {
      body: JSON.stringify(body),
    }),
  change: (aid: string, body: Schema['ScriptChange']) =>
    request<Schema['ScriptReceipt']>(`/api/v1/answer-scripts/${id(aid)}/versions`, {
      body: JSON.stringify(body),
      timeoutMs: 100000,
    }),
  inspect: (aid: string) => request<Schema['ScriptRead']>(`/api/v1/answer-scripts/${id(aid)}`),
  history: (aid: string, offset = 0) =>
    request<Schema['VersionList']>(
      `/api/v1/answer-scripts/${id(aid)}/versions?offset=${offset}&limit=10`,
    ),
}
