import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({
  assignments: vi.fn(),
  request: vi.fn(),
  mine: vi.fn(),
  sessions: vi.fn(),
  recoverySessions: vi.fn(),
  inspect: vi.fn(),
}))
vi.mock('../src/api/client', () => ({
  api: { assignments: mocks.assignments },
  request: mocks.request,
  id: (v: string) => v,
  ApiError: class ApiError extends Error {
    constructor(public status: number) {
      super('api error')
    }
  },
}))
vi.mock('../src/api/marking', () => ({
  marking: { mine: mocks.mine },
  taskStatus: { assigned: '待领取' },
}))
vi.mock('../src/api/exam-configuration', () => ({ configuration: { sessions: mocks.sessions } }))
vi.mock('../src/api/recovery', () => ({
  recovery: { sessions: mocks.recoverySessions, inspect: mocks.inspect },
}))
vi.mock('../src/auth', () => ({ identity: { name: '合成教师' } }))
vi.mock('../src/navigation', () => ({
  navigationAccess: { papers: true, exams: true, myMarking: true, exceptions: true },
}))
import Dashboard from '../src/views/Dashboard.vue'
beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset())
  mocks.assignments.mockResolvedValue([])
  mocks.mine.mockResolvedValue({ items: [], counts: [], has_next: false })
  mocks.sessions.mockResolvedValue({ items: [] })
  mocks.recoverySessions.mockResolvedValue({ items: [] })
})
function open() {
  return mount(Dashboard, {
    global: { stubs: { RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' } } },
  })
}
it('shows service failures distinctly from empty authorized data', async () => {
  mocks.assignments.mockRejectedValue(new Error('unavailable'))
  mocks.recoverySessions.mockRejectedValue(new Error('unavailable'))
  const wrapper = open()
  await flushPromises()
  expect(wrapper.text()).toContain('暂时无法读取作业')
  expect(wrapper.text()).toContain('当前无法读取异常')
  expect(wrapper.text()).not.toContain('还没有可查看的作业')
  wrapper.unmount()
})
it('shows only accessible results and labels a partial task page accurately', async () => {
  mocks.assignments.mockResolvedValue([
    {
      id: 'a',
      title: '合成练习',
      subject: '数学',
      grade_level: '高一',
      status: 'published',
      created_at: '2026-10-01T00:00:00Z',
    },
  ])
  mocks.request.mockResolvedValue({ counts: [{ expected: 4, submitted: 3 }] })
  mocks.mine.mockResolvedValue({
    items: [],
    counts: [{ status: 'assigned', count: 3 }],
    has_next: true,
  })
  mocks.recoverySessions.mockResolvedValue({
    items: [{ id: 's', title: '数学', event_title: '合成考试' }],
  })
  mocks.inspect.mockResolvedValue({ total: 2 })
  const wrapper = open()
  await flushPromises()
  expect(wrapper.text()).toContain('合成练习')
  expect(wrapper.text()).toContain('已交 3 / 应交 4')
  expect(wrapper.text()).toContain('本页没有待处理任务，可查看其他页')
  expect(wrapper.text()).toContain('2 项')
  expect(wrapper.find('a[href="/exceptions?session=s"]').exists()).toBe(true)
  wrapper.unmount()
})
