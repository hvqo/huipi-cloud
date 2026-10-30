import { beforeEach, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({
  paper: vi.fn(),
  options: vi.fn(),
  events: vi.fn(),
  sessions: vi.fn(),
  scans: vi.fn(),
  managed: vi.fn(),
  mine: vi.fn(),
  recovery: vi.fn(),
  results: vi.fn(),
  paperChoices: vi.fn(),
}))
vi.mock('../src/api/papers', () => ({ papers: { list: mocks.paper } }))
vi.mock('../src/api/exam-configuration', () => ({
  configuration: {
    options: mocks.options,
    events: mocks.events,
    sessions: mocks.sessions,
    papers: mocks.paperChoices,
  },
}))
vi.mock('../src/api/scans', () => ({ scans: { sessions: mocks.scans } }))
vi.mock('../src/api/marking', () => ({ marking: { sessions: mocks.managed, mine: mocks.mine } }))
vi.mock('../src/api/recovery', () => ({ recovery: { sessions: mocks.recovery } }))
vi.mock('../src/api/exam-results', () => ({ examResults: { sessions: mocks.results } }))
import { identity } from '../src/auth'
import { clearNavigation, loadNavigation, navigationAccess } from '../src/navigation'
beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset().mockResolvedValue({ items: [] }))
  mocks.paper.mockResolvedValue({ items: [], can_create: false })
  mocks.options.mockResolvedValue({ can_create_event: false })
  mocks.mine.mockResolvedValue({ items: [], counts: [] })
  identity.actor = 'teacher-a'
  clearNavigation()
})
it('does not infer scoped access from a role or a successful empty list', async () => {
  await loadNavigation(identity.actor)
  expect(Object.values(navigationAccess).every((value) => !value)).toBe(true)
})
it('uses server returned capabilities and accessible resources for navigation', async () => {
  mocks.paper.mockResolvedValue({ items: [], can_create: true })
  mocks.sessions.mockResolvedValue({ items: [{ id: 'session' }] })
  mocks.mine.mockResolvedValue({ items: [], counts: [{ status: 'assigned', count: 2 }] })
  mocks.results.mockResolvedValue({ items: [{ session_id: 'session' }] })
  await loadNavigation(identity.actor)
  expect(navigationAccess.papers).toBe(true)
  expect(navigationAccess.exams).toBe(true)
  expect(navigationAccess.myMarking).toBe(true)
  expect(navigationAccess.results).toBe(true)
  expect(navigationAccess.scans).toBe(false)
})
it('fails closed when capabilities cannot be retrieved and ignores old identity responses', async () => {
  let resolve!: (value: unknown) => void
  mocks.paper.mockReturnValue(
    new Promise((r) => {
      resolve = r
    }),
  )
  const old = loadNavigation(identity.actor)
  identity.actor = 'teacher-b'
  clearNavigation()
  resolve({ items: [{ id: 'old-paper' }], can_create: true })
  await old
  expect(navigationAccess.papers).toBe(false)
  mocks.paper.mockRejectedValue(new Error('unavailable'))
  await loadNavigation(identity.actor)
  expect(navigationAccess.papers).toBe(false)
})
