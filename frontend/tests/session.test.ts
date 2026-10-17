import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const { me } = vi.hoisted(() => ({ me: vi.fn() }))
vi.mock('../src/services', () => ({ services: async () => ({ me: { current: me } }) }))
import { acceptOidcSession, session, refreshSession, signInDemo } from '../src/app/session'
import { identity, logout, safeReturn } from '../src/auth'
import { ApiError } from '../src/api/client'
beforeEach(() => {
  me.mockReset()
  sessionStorage.clear()
  identity.authenticated = true
  identity.actor = 'issuer|subject'
  identity.name = ''
  session.role = ''
  session.capabilities = []
})
afterEach(() => {
  identity.authenticated = false
  session.authenticated = false
  session.ready = false
})
it('PKCE回调后读取真实/me，再允许角色入口，不根据前端声明赋权', async () => {
  me.mockResolvedValue({
    user_id: 'server-user',
    role: 'admin',
    capabilities: ['admin:read'],
    display_name: null,
  })
  await acceptOidcSession()
  expect(session).toMatchObject({
    authenticated: true,
    ready: true,
    role: 'admin',
    actor: 'issuer|subject',
    userId: 'server-user',
  })
  expect(session.capabilities).toEqual(['admin:read'])
})
it('档案读取失败立即丢弃上次角色和能力，阻断页面而不降级演示', async () => {
  session.role = 'admin'
  session.capabilities = ['admin:write']
  session.userId = 'previous-user'
  me.mockRejectedValue(new ApiError(503, 'synthetic_unavailable'))
  await refreshSession()
  expect(session.role).toBe('')
  expect(session.capabilities).toEqual([])
  expect(session.userId).toBe('')
  expect(session.provisionError).not.toBe('')
})
it('显式退出清除作答和评分的待确认正文及候选目标，不清除其他应用数据', async () => {
  sessionStorage.setItem('huipi.review.actor.s.q', 'synthetic-body')
  sessionStorage.setItem('huipi.grading.actor.s.q', 'synthetic-body')
  sessionStorage.setItem('huipi.grading.actor.s.q.target', 'p')
  sessionStorage.setItem('other-application', 'keep')
  await logout()
  expect(Object.keys(sessionStorage)).toEqual(['other-application'])
})
it('角色入口返回地址只允许站内白名单，拒绝外部URL和反斜杠', () => {
  expect(safeReturn('/')).toBe('/')
  expect(safeReturn('/admin/teachers')).toBe('/admin/teachers')
  for (const path of ['https://evil.invalid', '//evil.invalid', '/\\evil.invalid', '/not-allowed'])
    expect(safeReturn(path)).toBe('/assignments')
})

it('真实模式直接调用演示登录也不能建立身份', async () => {
  await expect(signInDemo('admin')).rejects.toThrow('禁止')
  expect(session.role).toBe('')
})
