import { id, request, type Schema } from './client'
const base = '/api/v1/exam-configuration'
export const configuration = {
  options: (offset = 0) =>
    request<Schema['ConfigurationOptions']>(`${base}/options?offset=${offset}`),
  events: (offset = 0) =>
    request<Schema['ConfigurationEventPage']>(`${base}/events?offset=${offset}`),
  sessions: (event = '', offset = 0) =>
    request<Schema['ConfigurationSessionPage']>(
      `${base}/sessions?offset=${offset}${event ? `&event_id=${id(event)}` : ''}`,
    ),
  session: (sid: string) =>
    request<Schema['ConfigurationSessionView']>(`${base}/sessions/${id(sid)}`),
  papers: (offset = 0) =>
    request<Schema['ConfigurationPaperPage']>(`${base}/papers?offset=${offset}`, {
      timeoutMs: 60000,
    }),
  rosters: (sid: string) =>
    request<Schema['ConfigurationRosterHistory']>(`${base}/sessions/${id(sid)}/rosters`),
  roster: (rid: string, offset = 0) =>
    request<Schema['ConfigurationRosterPage']>(
      `${base}/rosters/${id(rid)}?offset=${offset}&limit=100`,
    ),
  candidates: (sid: string, body: Schema['ConfigurationCandidateRequest']) =>
    request<Schema['ConfigurationCandidatePage']>(`${base}/sessions/${id(sid)}/roster-preview`, {
      body: JSON.stringify(body),
      timeoutMs: 60000,
    }),
  write: <T>(path: string, body: unknown) => {
    if (
      !/^\/(events|sessions|rosters)(\/|$)/.test(path) ||
      path.includes('..') ||
      path.includes('://')
    )
      throw new Error('无效操作。')
    return request<T>(base + path, {
      body: JSON.stringify(body),
      timeoutMs: 125000,
      maxBytes: 2 * 1024 * 1024,
    })
  },
}
