import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => s }))
import { examGrading } from '../src/api/exam-grading'
const identifier = '11111111-1111-4111-8111-111111111111'
beforeEach(() => request.mockReset())
describe('考试评分受控合同', () => {
  it('从具体解析任务准备题目，不传学生或对象路径', async () => {
    await examGrading.initialize(identifier, identifier)
    expect(request).toHaveBeenCalledWith(
      `/api/v1/exam-parsing-jobs/${identifier}/grading-workspace`,
      {
        body: JSON.stringify({ request_id: identifier }),
        timeoutMs: 100000,
      },
    )
  })
  it('评分只发送幂等身份和当前复核版本', async () => {
    const body = { request_id: identifier, expected_answer_revision: 2 }
    await examGrading.mutate(identifier, 'scoring', body)
    expect(request).toHaveBeenCalledWith(`/api/v1/exam-grading-workspaces/${identifier}/scoring`, {
      body: JSON.stringify(body),
      timeoutMs: 100000,
    })
  })
  it('真实后端失败不会被替换为成功结果', async () => {
    const failure = { code: 'source_changed' }
    request.mockImplementationOnce(async () => {
      throw failure
    })
    let received: unknown
    try {
      await examGrading.details(identifier)
    } catch (error) {
      received = error
    }
    expect(received).toBe(failure)
  })
})
