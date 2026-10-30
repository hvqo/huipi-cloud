import { expect, it, vi } from 'vitest'
import { User, WebStorageStateStore, InMemoryWebStorage } from 'oidc-client-ts'
import { manager, identity, initializeAuth, accessToken, safeReturn } from '../src/auth'
it('fails closed without an IdP and has no URL-token or localStorage login', async () => {
  expect(manager).toBe(null)
  await initializeAuth()
  expect(identity.authenticated).toBe(false)
  expect(accessToken()).toBe(null)
  expect(localStorage.length).toBe(0)
  expect(safeReturn('https://evil.invalid')).toBe('/dashboard')
  expect(safeReturn('//evil.invalid')).toBe('/dashboard')
})
it('uses an in-memory token store and recognizes expired tokens', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-01-01T00:00:00Z'))
  const user = new User({
    access_token: 'ephemeral',
    token_type: 'Bearer',
    profile: {
      iss: 'issuer',
      sub: 'subject',
      aud: 'frontend',
      exp: Date.now() / 1000 + 10,
      iat: Date.now() / 1000,
    },
    expires_at: Date.now() / 1000 + 10,
  })
  const store = new WebStorageStateStore({ store: new InMemoryWebStorage() })
  await store.set('user', user.toStorageString())
  expect(localStorage.length).toBe(0)
  expect(user.expired).toBe(false)
  vi.advanceTimersByTime(11000)
  expect(user.expired).toBe(true)
  vi.useRealTimers()
})

it('allows the scan login destination without allowing external return targets', () => {
  expect(safeReturn('/dashboard')).toBe('/dashboard')
  expect(safeReturn('/scans')).toBe('/scans')
  expect(safeReturn('//evil.invalid/scans')).toBe('/dashboard')
  expect(safeReturn('/scans\\evil.invalid')).toBe('/dashboard')
})

it('accepts only internal exam configuration and results login destinations', () => {
  expect(safeReturn('/exams')).toBe('/exams')
  expect(safeReturn('/exam-results')).toBe('/exam-results')
  expect(safeReturn('/scans?session=synthetic')).toBe('/scans?session=synthetic')
  expect(safeReturn('//evil.invalid/exams')).toBe('/dashboard')
  expect(safeReturn('/exams\\evil.invalid')).toBe('/dashboard')
})
