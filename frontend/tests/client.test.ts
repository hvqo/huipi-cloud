import { afterEach, expect, it, vi } from 'vitest'
const auth = vi.hoisted(() => ({
  csrfToken: vi.fn(() => 'csrf-test-token'),
  expireAuth: vi.fn(),
}))
vi.mock('../src/auth', () => auth)
import { ApiError, readBounded, request } from '../src/api/client'
afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})
it('uses same-origin cookies and forbids arbitrary endpoints', async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response('{}', { headers: { 'content-type': 'application/json' } }))
  vi.stubGlobal('fetch', fetch)
  await request('/api/v1/assignments')
  expect(fetch.mock.calls[0]![0]).toBe('/api/v1/assignments')
  expect(fetch.mock.calls[0]![1].headers.Authorization).toBeUndefined()
  expect(fetch.mock.calls[0]![1].credentials).toBe('same-origin')
  await expect(request('https://evil.invalid/api/v1/assignments')).rejects.toBeInstanceOf(ApiError)
  expect(fetch).toHaveBeenCalledTimes(1)
})
it('clears expired session state on 401', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }))
  vi.stubGlobal('fetch', fetch)
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

it('scan multipart lets the browser set its boundary and sends no JSON header', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response('{}'))
  vi.stubGlobal('fetch', fetch)
  const { scans } = await import('../src/api/scans')
  const file = new File(['%PDF-synthetic'], 'mixed.pdf', { type: 'application/pdf' })
  await scans.upload('batch', 'request', file)
  const options = fetch.mock.calls[0]![1]
  expect(options.body).toBeInstanceOf(FormData)
  expect(options.body.get('request_id')).toBe('request')
  expect(options.body.get('file')).toBe(file)
  expect(options.headers).not.toHaveProperty('Content-Type')
  expect(options.credentials).toBe('same-origin')
  expect(options.headers['X-Huipi-CSRF']).toBe('csrf-test-token')
  expect(options.redirect).toBe('error')
})

it('school errors opt in only to bounded public codes, never arbitrary private bodies', async () => {
  for (const [detail, code] of [
    ['student_import_preview_stale', 'student_import_preview_stale'],
    ['Student 001 / private SQL', 'api_error'],
    ['x'.repeat(81), 'api_error'],
  ] as const) {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail }), { status: 409 })),
    )
    await expect(
      request('/api/v1/organization/student-imports/x/confirm', { body: '{}', errorCode: true }),
    ).rejects.toMatchObject({ status: 409, code })
  }
})
