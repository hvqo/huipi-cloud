import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({
  request,
  id: (v: string) => {
    if (!/^[0-9a-f-]{36}$/.test(v)) throw new Error('id')
    return v
  },
}))
import { scripts } from '../src/api/answer-scripts'
const sid = '11111111-1111-4111-8111-111111111111'
beforeEach(() => request.mockReset())
describe('分卷HTTP合同', () => {
  it('只查询明确场次并编码名单搜索', async () => {
    await scripts.candidates(sid, "同名' OR 1=1", 20)
    expect(request).toHaveBeenCalledWith(
      `/api/v1/exam-sessions/${sid}/identity-candidates?q=${encodeURIComponent("同名' OR 1=1")}&offset=20&limit=20`,
    )
  })
  it('草稿保留请求幂等键与页面顺序，不声明学生', async () => {
    const body = {
      request_id: sid,
      page_ids: [sid],
      completeness: 'incomplete' as const,
      reason: '页组核对',
    }
    await scripts.create(sid, body)
    const sent = JSON.parse(request.mock.calls[0]![1].body)
    expect(sent).toEqual(body)
    expect(sent).not.toHaveProperty('student_id')
  })
  it('更正包含乐观锁及有界核验超时', async () => {
    const body = {
      request_id: sid,
      page_ids: [sid],
      completeness: 'complete' as const,
      reason: '重新核对',
      expected_revision: 3,
      action: 'confirm' as const,
      roster_revision_id: sid,
      roster_entry_id: sid,
      session_paper_id: sid,
    }
    await scripts.change(sid, body)
    expect(request).toHaveBeenCalledWith(`/api/v1/answer-scripts/${sid}/versions`, {
      body: JSON.stringify(body),
      timeoutMs: 100000,
    })
  })
  it('未认领页筛选与跨批次入口仍固定在同一场次', async () => {
    await scripts.pages(sid, 20, true, sid)
    expect(request.mock.calls[0]![0]).toBe(
      `/api/v1/exam-sessions/${sid}/script-pages?offset=20&limit=20&unclaimed=true&batch_id=${sid}`,
    )
  })
})
