import { beforeEach, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => encodeURIComponent(s) }))
import { marking } from '../src/api/marking'
beforeEach(() => request.mockReset())
it('任务写入只能使用固定操作路径，不能发任意SQL或授权请求', () => {
  for (const path of ['https://invalid', '/security/grants', '/marking-tasks/../../admin/claim'])
    expect(() => marking.write(path, '{}')).toThrow()
  expect(request).not.toHaveBeenCalled()
})
it('相同任务请求人工重试保持request_id及revision正文', async () => {
  const path = '/marking-tasks/11111111-1111-4111-8111-111111111111/complete',
    body = JSON.stringify({ request_id: 'fixed', expected_revision: 2 })
  await marking.write(path, body)
  await marking.write(path, body)
  expect(request.mock.calls[0]).toEqual(request.mock.calls[1])
  expect(request.mock.calls[0]![1]).toMatchObject({ body, errorCode: true })
})
it('只通过具体任务预览其页面，不读取整个混班PDF', async () => {
  await marking.preview('task', 'page')
  expect(request.mock.calls[0]).toEqual([
    '/api/v1/marking-tasks/task/pages/page/preview',
    { blob: true, maxBytes: 2097152 },
  ])
})
it('个人任务有分页，不在客户端提交教师身份', async () => {
  await marking.mine(25)
  expect(request.mock.calls[0]![0]).toBe('/api/v1/my-marking-tasks?offset=25')
  expect(request.mock.calls[0]![1]).toEqual({ timeoutMs: 100000 })
  expect(JSON.stringify(request.mock.calls[0])).not.toMatch(/teacher_id|user_id|role/)
})
