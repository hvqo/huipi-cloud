import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => s }))
import { papers } from '../src/api/papers'
const pid = '11111111-1111-4111-8111-111111111111'
beforeEach(() => request.mockReset())
describe('试卷管理API合同', () => {
  it('试卷详情、来源和历史使用独立受控接口', async () => {
    await papers.detail(pid)
    await papers.sources(20)
    await papers.history(pid)
    expect(request.mock.calls.map((x) => x[0])).toEqual([
      `/api/v1/papers/${pid}`,
      '/api/v1/papers/sources?offset=20',
      `/api/v1/papers/${pid}/history?offset=0`,
    ])
  })
  it('重试保留request_id、原始正文和双摘要', async () => {
    const body = {
      request_id: pid,
      expected_revision: 1,
      expected_content_sha256: 'a'.repeat(64),
      expected_mapping_digest: 'b'.repeat(64),
    }
    await papers.write(`/${pid}/lock`, body)
    await papers.write(`/${pid}/lock`, body)
    expect(request.mock.calls[0]).toEqual(request.mock.calls[1])
    expect(JSON.parse(request.mock.calls[0]![1].body)).toEqual(body)
  })
  it('不能调用任意URL、旧作业写入或授权接口', () => {
    for (const path of ['https://invalid', `/${pid}/grant`, `/../assignments/${pid}`])
      expect(() => papers.write(path, {})).toThrow()
  })
})
