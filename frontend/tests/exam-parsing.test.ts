import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => s }))
import { examParsing } from '../src/api/exam-parsing'
const aid = '11111111-1111-4111-8111-111111111111'
beforeEach(() => request.mockReset())
describe('受保护答卷解析合同', () => {
  it('发起仅包含原请求和答卷revision，不允许发送学生或存储位置', async () => {
    const body = { request_id: aid, expected_revision: 2 }
    await examParsing.start(aid, body)
    expect(request).toHaveBeenCalledWith(`/api/v1/answer-scripts/${aid}/parsing`, {
      body: JSON.stringify(body),
      timeoutMs: 100000,
    })
  })
  it('当前状态来自后端，不使用等待时间推测成功', async () => {
    request.mockResolvedValue({ status: 'pending', current: true })
    expect((await examParsing.current(aid)).status).toBe('pending')
  })
  it('结果按具体job和原零基页序读取', async () => {
    await examParsing.page(aid, 1)
    expect(request).toHaveBeenCalledWith(`/api/v1/exam-parsing-jobs/${aid}/pages/1`, {
      timeoutMs: 100000,
    })
  })
})
