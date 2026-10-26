import { beforeEach, describe, expect, it, vi } from 'vitest'
const { request } = vi.hoisted(() => ({ request: vi.fn() }))
vi.mock('../src/api/client', () => ({ request, id: (s: string) => s }))
import {
  school,
  schoolAccess,
  loadSchoolAccess,
  resetSchoolAccess,
  type Kind,
} from '../src/api/school'
beforeEach(() => {
  request.mockReset()
  resetSchoolAccess()
})
describe('学校合同适配器', () => {
  it('能力来自服务端当前身份，无客户端role或teacher_id', async () => {
    request.mockResolvedValue({
      can_read: false,
      can_manage: false,
      can_preview: false,
      can_confirm: false,
    })
    await loadSchoolAccess()
    expect(request.mock.calls[0]![0]).toBe('/api/v1/me/school-access')
    expect(request.mock.calls[0]![1]).not.toHaveProperty('body')
    expect(schoolAccess.can_read).toBe(false)
  })
  it('分页查询学生，明确传递业务日期和筛选，不在浏览器全量加载', async () => {
    await school.students('class', '合成', '2026-10-26', 25)
    const url = new URL(request.mock.calls[0]![0], 'https://test.invalid')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      class_id: 'class',
      query: '合成',
      as_of: '2026-10-26',
      offset: '25',
      limit: '25',
    })
  })
  it('三种CSV模板均由服务端生成并有界读取', async () => {
    for (const mode of ['student_only', 'enrollment_only', 'student_with_enrollment'] as const)
      await school.template(mode)
    expect(request.mock.calls).toHaveLength(3)
    for (const call of request.mock.calls)
      expect(call[1]).toMatchObject({ blob: true, maxBytes: 1024 })
  })
  it('预检使用原FormData，显式确认是另一请求，重试保持相同正文', async () => {
    const body = new FormData()
    body.set('request_id', 'fixed')
    body.set('file', new File(['synthetic'], 'a.csv'))
    await school.preview(body)
    expect(request).toHaveBeenCalledTimes(1)
    expect(request.mock.calls[0]![1].body).toBe(body)
    const confirmation = JSON.stringify({
      request_id: 'confirm',
      expected_input_sha256: 'a'.repeat(64),
    })
    await school.confirm('batch', confirmation)
    await school.confirm('batch', confirmation)
    expect(request.mock.calls[1]).toEqual(request.mock.calls[2])
  })
  it('组织写入限定为固定字典，不能指定任意URL或操作授权', () => {
    expect(() => school.create('https://evil.invalid' as Kind, '{}')).toThrow()
    expect(() => school.create('../security/grants' as Kind, '{}')).toThrow()
    expect(request).not.toHaveBeenCalled()
  })
  it('能力重置不留下上一个登录者的入口', () => {
    Object.assign(schoolAccess, {
      can_read: true,
      can_manage: true,
      can_preview: true,
      can_confirm: true,
    })
    resetSchoolAccess()
    expect(Object.values(schoolAccess)).toEqual([false, false, false, false])
  })
})
