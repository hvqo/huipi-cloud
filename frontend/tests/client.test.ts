import { afterEach, expect, it, vi } from 'vitest'
const auth = vi.hoisted(() => ({
  accessToken: vi.fn((): string | null => 'ephemeral-test-token'),
  expireAuth: vi.fn(),
}))
vi.mock('../src/auth', () => auth)
import { ApiError, readBounded, request } from '../src/api/client'
afterEach(() => {
  vi.unstubAllGlobals()
  auth.accessToken.mockReturnValue('ephemeral-test-token')
  vi.clearAllMocks()
})
it('sends Bearer only in the header and forbids arbitrary endpoints', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response('{}', { headers: { 'content-type': 'application/json' } }))
  vi.stubGlobal('fetch', fetch)
  await request('/api/v1/assignments')
  expect(fetch.mock.calls[0]![0]).toBe('/api/v1/assignments')
  expect(fetch.mock.calls[0]![1].headers.Authorization).toBe('Bearer ephemeral-test-token')
  await expect(request('https://evil.invalid/api/v1/assignments')).rejects.toBeInstanceOf(ApiError)
  expect(fetch).toHaveBeenCalledTimes(1)
})
it('does not make anonymous requests and clears expired credentials on 401', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }))
  vi.stubGlobal('fetch', fetch)
  auth.accessToken.mockReturnValue(null)
  await expect(request('/api/v1/assignments')).rejects.toMatchObject({ status: 401 })
  expect(fetch).not.toHaveBeenCalled()
  auth.accessToken.mockReturnValue('test')
  await expect(request('/api/v1/assignments')).rejects.toMatchObject({ status: 401 })
  expect(auth.expireAuth).toHaveBeenCalledOnce()
})
it.each([403, 404, 409, 422, 503])(
  'keeps %i status distinct without exposing the server error body',
  async (status) => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('private-source-and-db-internals', { status })),
    )
    await expect(request('/api/v1/x', { body: '{}' })).rejects.toMatchObject({
      status,
      uncertain: status === 503,
    })
  },
)
it('bounds streaming response bytes', async () => {
  await expect(readBounded(new Response('123456'), 5)).rejects.toMatchObject({ status: 413 })
})
it('passes caller cancellation and classifies an interrupted POST as unknown', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      (_path, options: RequestInit) =>
        new Promise((_resolve, reject) => {
          options.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          )
        }),
    ),
  )
  const controller = new AbortController(),
    result = request('/api/v1/x', { body: '{}', signal: controller.signal })
  controller.abort()
  await expect(result).rejects.toMatchObject({ uncertain: true })
})

it('does not claim rollback when a successful POST response cannot be consumed', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue(new Response('x'.repeat(2 * 1024 * 1024 + 1), { status: 201 })),
  )
  await expect(request('/api/v1/x', { body: '{}' })).rejects.toMatchObject({ uncertain: true })
})
