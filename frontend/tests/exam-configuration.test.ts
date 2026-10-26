import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => s }))
import { configuration } from '../src/api/exam-configuration'
const sid = '11111111-1111-4111-8111-111111111111'
beforeEach(() => request.mockReset())
describe('考试配置受控HTTP合同', () => {
  it('按明确考试筛选授权场次，不提交身份或权限字段', async () => {
    await configuration.sessions(sid, 20)
    expect(request.mock.calls[0]?.[0]).toBe(
      `/api/v1/exam-configuration/sessions?offset=20&event_id=${sid}`,
    )
  })
  it('名单预览使用真实班级ID与场次revision，日期来自服务端', async () => {
    const body = {
      request_id: sid,
      class_ids: [sid],
      expected_session_revision: 3,
      offset: 0,
      limit: 100,
    }
    await configuration.candidates(sid, body)
    expect(request.mock.calls[0]?.[1]).toMatchObject({ body: JSON.stringify(body) })
    expect(request.mock.calls[0]?.[1].body).not.toContain('teacher_id')
  })
  it('重试不改写request_id和原始正文', async () => {
    const body = { request_id: sid, expected_revision: 2, lifecycle: 'ready' }
    await configuration.write(`/sessions/${sid}/state`, body)
    await configuration.write(`/sessions/${sid}/state`, body)
    expect(request.mock.calls[0]).toEqual(request.mock.calls[1])
  })
  it('适配器不能请求任意URL或目录', () => {
    expect(() => configuration.write('https://invalid/', {})).toThrow()
    expect(() => configuration.write('/events/../admin', {})).toThrow()
  })
})
