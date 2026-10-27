import { beforeEach, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => encodeURIComponent(s) }))
import { recovery, exceptionNames } from '../src/api/recovery'
import { scans } from '../src/api/scans'
beforeEach(() => request.mockReset())
it('异常汇总使用场次和分页，不提交客户端角色或身份', async () => {
  await recovery.inspect('session', 25, 'parsing_failed')
  expect(request.mock.calls[0]![0]).toBe(
    '/api/v1/exam-sessions/session/exceptions?offset=25&kind=parsing_failed',
  )
  expect(request.mock.calls[0]![1]).toEqual({ timeoutMs: 30000 })
})
it('状态徽标请求摘要，不加载全部异常和原件', async () => {
  await recovery.inspect('session', 0, '', true)
  expect(request.mock.calls[0]![0]).toContain('summary=true')
  expect(request.mock.calls[0]![1]).not.toHaveProperty('body')
})
it('失败文件重传保留原始request_id并由原上传端点验证内容', async () => {
  const file = new File(['%PDF-'], 'synthetic.pdf', { type: 'application/pdf' })
  await scans.upload('batch', 'original-request', file)
  const options = request.mock.calls[0]![1]
  expect(request.mock.calls[0]![0]).toBe('/api/v1/scan-batches/batch/files')
  expect(options.body.get('request_id')).toBe('original-request')
  expect(options.body.get('file').name).toBe('synthetic.pdf')
})
it('弃权、未确认与失效各有独立中文标签，不能当成零分', () => {
  expect(exceptionNames.scoring_abstained).toBe('AI评分弃权')
  expect(exceptionNames.identity_pending).toBe('身份待确认')
  expect(exceptionNames.scoring_invalidated).not.toBe(exceptionNames.scoring_failed)
})
