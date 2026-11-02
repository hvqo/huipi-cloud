import { afterEach, expect, it, vi } from 'vitest'
import { identity, initializeAuth, login, logout, safeReturn } from '../src/auth'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

it('fails closed on an unavailable session and does not store credentials in browser storage', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 401 })))
  await initializeAuth()
  expect(identity.authenticated).toBe(false)
  expect(localStorage.length).toBe(0)
  expect(safeReturn('https://evil.invalid')).toBe('/dashboard')
  expect(safeReturn('//evil.invalid')).toBe('/dashboard')
})

it('submits account and password to the same-origin session endpoint and honours forced reset', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response(
        JSON.stringify({ user_id: 'u1', display_name: '教师', must_change_password: true }),
        { status: 200 },
      ),
    )
  vi.stubGlobal('fetch', fetch)
  await login('teacher@example.invalid', 'synthetic-long-password')
  expect(identity.authenticated).toBe(true)
  expect(identity.mustChangePassword).toBe(true)
  expect(fetch.mock.calls[0]![0]).toBe('/api/v1/auth/login')
  expect(fetch.mock.calls[0]![1].credentials).toBe('same-origin')
  expect(localStorage.length).toBe(0)
})

it('logout fails closed if the server cannot revoke the session', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 503 })))
  await expect(logout()).rejects.toThrow('退出未完成')
})

it('allows internal destinations and rejects external redirects', () => {
  expect(safeReturn('/dashboard')).toBe('/dashboard')
  expect(safeReturn('/scans?session=synthetic')).toBe('/scans?session=synthetic')
  expect(safeReturn('/exam-results')).toBe('/exam-results')
  expect(safeReturn('//evil.invalid/scans')).toBe('/dashboard')
  expect(safeReturn('/scans\\evil.invalid')).toBe('/dashboard')
})
