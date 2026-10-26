import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => s }))
import { examResults } from '../src/api/exam-results'
const sid = '11111111-1111-4111-8111-111111111111'
beforeEach(() => request.mockReset())
describe('教师整卷成绩只读合同', () => {
  it('班级筛选发送给服务端，不在前端累加候选分数', async () => {
    request.mockResolvedValue({ students: [{ total: null, subtotal: '8.00' }] })
    const result = await examResults.preview(sid, sid, 20)
    expect(result.students[0]?.total).toBeNull()
    expect(request.mock.calls[0]?.[0]).toContain('class_id=')
    expect(request.mock.calls[0]?.[0]).toContain('offset=20')
  })
  it('服务端拒绝不会变成空表或假成功', async () => {
    const failure = { code: 'resource_not_found_or_forbidden' }
    request.mockImplementationOnce(async () => {
      throw failure
    })
    let received: unknown
    try {
      await examResults.preview(sid, sid, 0)
    } catch (error) {
      received = error
    }
    expect(received).toBe(failure)
  })
  it('导出是有界受保护下载，不使用对象存储直链', async () => {
    request.mockResolvedValue(new Blob())
    await examResults.export(sid, sid)
    expect(request.mock.calls[0]?.[0]).toContain('/results-preview.xlsx?class_id=')
    expect(request.mock.calls[0]?.[1]).toMatchObject({ blob: true, maxBytes: 8 * 1024 * 1024 })
  })
})
