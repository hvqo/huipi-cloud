import { flushPromises, mount } from '@vue/test-utils'
import { expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  sessions: vi.fn(),
  preview: vi.fn(),
  attendance: vi.fn(),
  versions: vi.fn(),
}))
vi.mock('../src/api/exam-results', () => ({ examResults: mocks }))
vi.mock('../src/api/final-results', () => ({ finalResults: mocks }))

import FinalResults from '../src/views/FinalResults.vue'

it('可以翻页选择所有获授权的考试场次', async () => {
  const session = (id: string, title: string) => ({
    session_id: id,
    event_id: `event-${id}`,
    event_title: title,
    title: '数学',
    classes: [],
  })
  mocks.sessions.mockImplementation(async (_event: string, offset: number) => ({
    items: [offset ? session('second', '第二场考试') : session('first', '第一场考试')],
    truncated: offset === 0,
  }))
  mocks.attendance.mockResolvedValue({ items: [] })
  mocks.versions.mockResolvedValue({ items: [] })
  mocks.preview.mockResolvedValue({ students: [], has_more: false, limit: 20 })
  const wrapper = mount(FinalResults, { global: { stubs: { RouterLink: true } } })
  await flushPromises()
  expect(wrapper.get('select').text()).toContain('第一场考试')
  await wrapper.findAll('button').find((button) => button.text() === '下一批场次')!.trigger('click')
  await flushPromises()
  expect(mocks.sessions).toHaveBeenCalledWith('', 20)
  expect(wrapper.get('select').text()).toContain('第二场考试')
  await wrapper.findAll('button').find((button) => button.text() === '上一批场次')!.trigger('click')
  await flushPromises()
  expect(mocks.sessions).toHaveBeenLastCalledWith('', 0)
  wrapper.unmount()
})
