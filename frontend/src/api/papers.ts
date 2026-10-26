import { id, request, type Schema } from './client'
const base = '/api/v1/papers'
export const papers = {
  list: (offset = 0) =>
    request<Schema['PaperWebPage']>(`${base}?offset=${offset}`, { timeoutMs: 60000 }),
  sources: (offset = 0) => request<Schema['PaperWebSources']>(`${base}/sources?offset=${offset}`),
  detail: (pid: string) =>
    request<Schema['PaperWebDetail']>(`${base}/${id(pid)}`, {
      timeoutMs: 60000,
      maxBytes: 3 * 1024 * 1024,
    }),
  history: (pid: string, offset = 0) =>
    request<Schema['PaperWebPage']>(`${base}/${id(pid)}/history?offset=${offset}`, {
      timeoutMs: 60000,
    }),
  write: (path: string, body: unknown) => {
    if (!/^(|\/[0-9a-f-]{36}\/(questions|order|lock|fork|retire))$/i.test(path))
      throw new Error('无效试卷操作。')
    return request<Schema['PaperWebWriteResult']>(base + path, {
      body: JSON.stringify(body),
      timeoutMs: 125000,
    })
  },
}
