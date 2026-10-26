import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { beforeEach, afterEach, it, expect, vi } from 'vitest'
import { createRouter, createMemoryHistory } from 'vue-router'
const mocks = vi.hoisted(() => ({
  load: vi.fn(),
  api: Object.fromEntries(
    [
      'years',
      'cohorts',
      'subjects',
      'terms',
      'classes',
      'students',
      'enrollments',
      'history',
      'batch',
      'rows',
      'template',
      'create',
      'preview',
      'confirm',
    ].map((n) => [n, vi.fn()]),
  ),
}))
vi.mock('../src/api/school', async () => {
  const { reactive } = await import('vue')
  return {
    school: mocks.api,
    schoolAccess: reactive({
      can_read: false,
      can_manage: false,
      can_preview: false,
      can_confirm: false,
    }),
    loadSchoolAccess: mocks.load,
  }
})
import School from '../src/views/School.vue'
import { ApiError } from '../src/api/client'
let w: VueWrapper
const page = { records: [], limit: 25, offset: 0, has_next: false, next_offset: null }
const preview = {
  batch_id: 'batch',
  status: 'previewed',
  can_confirm: true,
  mode: 'student_with_enrollment',
  row_count: 1,
  counts: { row_total: 1, valid_rows: 1 },
  input_sha256: 'a'.repeat(64),
  errors: [],
  warnings: [],
  truncated: false,
}
beforeEach(() => {
  Object.values(mocks.api).forEach((m) => m.mockReset().mockResolvedValue(page))
  mocks.load
    .mockReset()
    .mockResolvedValue({ can_read: true, can_manage: false, can_preview: true, can_confirm: true })
  mocks.api.years!.mockResolvedValue({
    ...page,
    records: [
      { id: 'year', display_name: '合成学年', start_date: '2026-09-01', end_date: '2027-07-10' },
    ],
  })
  mocks.api.preview!.mockResolvedValue(preview)
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})
afterEach(() => w?.unmount())
async function setup() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/school', component: School },
      { path: '/exams', component: { template: '<div/>' } },
    ],
  })
  await router.push('/school')
  w = mount(School, {
    global: {
      plugins: [router],
      stubs: {
        ElButton: {
          props: ['disabled', 'nativeType'],
          template: '<button :disabled="disabled" :type="nativeType||\'button\'"><slot/></button>',
        },
        ElDialog: { props: ['modelValue'], template: '<div v-if="modelValue"><slot/></div>' },
      },
    },
  })
  await flushPromises()
}
function button(name: string) {
  return w.findAll('button').find((b) => b.text() === name)!
}
async function upload() {
  await button('学生导入').trigger('click')
  await flushPromises()
  const el = w.get('input[type=file]').element as HTMLInputElement
  Object.defineProperty(el, 'files', {
    value: [
      new File(
        ['student_code,name,class_code,valid_from\n001,合成,C1,2026-09-01'],
        'synthetic.csv',
      ),
    ],
    configurable: true,
  })
  await w.get('input[type=file]').trigger('change')
  await button('预检CSV').trigger('click')
  await flushPromises()
}
it('预检不会自动确认；阻塞错误禁止正式入库', async () => {
  mocks.api.preview!.mockResolvedValue({
    ...preview,
    can_confirm: false,
    counts: { error_rows: 1, valid_rows: 0 },
  })
  await setup()
  await upload()
  expect(w.text()).toContain('预检结果 · 尚未导入')
  expect(mocks.api.confirm).not.toHaveBeenCalled()
  expect(button('确认正式导入').attributes('disabled')).toBeDefined()
})
it('确认权限独立，只有预检权限不显示确认按钮', async () => {
  mocks.load.mockResolvedValue({
    can_read: true,
    can_manage: false,
    can_preview: true,
    can_confirm: false,
  })
  await setup()
  await upload()
  expect(w.text()).toContain('预检结果 · 尚未导入')
  expect(button('确认正式导入')).toBeUndefined()
})
it('COMMIT结果未知保留原request_id，只允许人工重试；过期预检要求重新预检', async () => {
  mocks.api
    .confirm!.mockRejectedValueOnce(new ApiError(503, 'organization_commit_outcome_unknown', true))
    .mockRejectedValueOnce(new ApiError(409, 'student_import_preview_stale'))
  await setup()
  await upload()
  await button('确认正式导入').trigger('click')
  await flushPromises()
  expect(w.text()).toContain('保存结果尚未核实')
  await button('重试原请求').trigger('click')
  await flushPromises()
  expect(mocks.api.confirm!.mock.calls[0]!.slice(0, 2)).toEqual(
    mocks.api.confirm!.mock.calls[1]!.slice(0, 2),
  )
  expect(w.text()).toContain('必须重新预检后确认')
  expect(button('确认正式导入').attributes('disabled')).toBeDefined()
})
it('普通建卷身份不能读取组织数据或出现管理操作', async () => {
  mocks.load.mockResolvedValue({
    can_read: false,
    can_manage: false,
    can_preview: false,
    can_confirm: false,
  })
  await setup()
  expect(w.text()).toContain('当前没有学校组织读取权限')
  expect(mocks.api.years).not.toHaveBeenCalled()
  expect(w.find('input[type=file]').exists()).toBe(false)
})
