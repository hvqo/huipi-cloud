import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
const mocks = vi.hoisted(() =>
  Object.fromEntries(
    ['review', 'page', 'canonical', 'submission', 'workbench', 'history', 'save', 'file'].map(
      (name) => [name, vi.fn()],
    ),
  ),
)
vi.mock('../src/api/client', async () => ({
  ...(await vi.importActual('../src/api/client')),
  api: mocks,
}))
import { ApiError } from '../src/api/client'
import ReviewPage from '../src/views/teacher/AnswerReview.vue'
import FilePreview from '../src/components/FilePreview.vue'
const source = {
  canonical_document_id: 'doc',
  canonical_sha256: 'a',
  alignment_artifact_id: 'align',
  alignment_sha256: 'b',
  aligner_version: '1',
  assignment_questions_digest: 'c',
}
const view = () => ({
  submission_id: 's',
  question_id: 'q',
  source,
  alignment_status: 'aligned',
  answer_presence: 'unreviewed',
  expected_revision: 0,
  latest: null,
  latest_source_matches: false,
  evidence: [],
  evidence_offset: 0,
  evidence_truncated: false,
  next_evidence_offset: null,
})
const button = {
  props: ['disabled', 'loading'],
  template: '<button :disabled="disabled || loading"><slot /></button>',
}
const dialog = {
  props: ['modelValue'],
  template: '<div v-if="modelValue" role="dialog"><slot /><slot name="footer" /></div>',
}
let wrapper: VueWrapper
beforeEach(() => {
  sessionStorage.clear()
  Object.values(mocks).forEach((mock) => mock.mockReset())
  mocks.review!.mockResolvedValue(view())
  mocks.page!.mockResolvedValue({
    document_id: 'doc',
    page: {
      page_index: 0,
      blocks: [
        {
          block_id: 'block',
          normalized_type: 'text',
          content: {
            normalized_type: 'text',
            value: '第1题 学生作答',
            children: [],
            asset_refs: [],
          },
        },
      ],
    },
  })
  mocks.canonical!.mockResolvedValue({
    canonical_document: { canonical_sha256: 'a', page_count: 1 },
  })
  mocks.submission!.mockResolvedValue({
    assignment_id: 'a',
    student_ref: 'synthetic',
    file: { original_filename: 'test.pdf', content_type: 'application/pdf' },
  })
  mocks.workbench!.mockResolvedValue({
    assignment: { title: '合成作业' },
    can_write: true,
    questions: [{ id: 'q', question_number: 1, stem: '合成题干' }],
  })
  mocks.history!.mockResolvedValue({ records: [], has_next: false })
})
afterEach(() => wrapper?.unmount())
async function setup() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/assignments/:aid/submissions/:sid/questions/:qid/review', component: ReviewPage },
    ],
  })
  router.addRoute({
    path: '/assignments/:aid/submissions/:sid',
    component: { template: '<div />' },
  })
  await router.push('/assignments/a/submissions/s/questions/q/review')
  wrapper = mount(ReviewPage, {
    global: { plugins: [router], stubs: { ElButton: button, ElDialog: dialog } },
  })
  await flushPromises()
}
async function chooseUncertain() {
  await wrapper.get('input[value="uncertain"]').setValue(true)
  await wrapper.get('select').setValue('source_quality_insufficient')
  await findButton('预览并保存复核').trigger('click')
  await wrapper.get('input[type="checkbox"]').setValue(true)
}
function findButton(text: string) {
  return wrapper.findAll('button').find((b) => b.text() === text)!
}

describe('teacher review UI over Mock API', () => {
  it('shows exact Canonical text, current revision and no automatic evidence', async () => {
    await setup()
    expect(wrapper.get('[data-source-pointer]').text()).toBe('第1题 学生作答')
    await findButton('预览并保存复核').trigger('click')
    expect(wrapper.text()).toContain('请选择真实证据')
    expect(mocks.save).not.toHaveBeenCalled()
  })
  it('locks reader writes while retaining read-only legacy history', async () => {
    mocks.workbench!.mockResolvedValue({
      assignment: { title: '合成' },
      can_write: false,
      questions: [{ id: 'q', question_number: 1 }],
    })
    mocks.history!.mockResolvedValue({
      records: [
        {
          decision_id: 'r',
          revision: 1,
          decision: 'uncertain',
          reason_codes: ['source_quality_insufficient'],
          reviewer_kind: 'legacy_cli_claim',
          source,
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    })
    await setup()
    expect(findButton('预览并保存复核').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('历史自声明身份（未认证）')
    expect(wrapper.findAll('button').some((b) => ['编辑', '删除'].includes(b.text()))).toBe(false)
  })
  it('marks an old review as stale without interpreting it as student absence', async () => {
    mocks.review!.mockResolvedValue({
      ...view(),
      latest: { revision: 2 },
      latest_source_matches: false,
    })
    await setup()
    expect(wrapper.text()).toContain('历史决定来源已过期')
    expect(wrapper.text()).toContain('待复核')
  })
  it('keeps and retries identical bytes on 503, then clears the pending draft', async () => {
    mocks
      .save!.mockRejectedValueOnce(new ApiError(503, 'unknown', true))
      .mockResolvedValue({ record: { revision: 1 } })
    await setup()
    await chooseUncertain()
    await findButton('确认保存').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('不代表没有提交')
    const first = mocks.save!.mock.calls[0]![2]
    await findButton('重试原请求').trigger('click')
    await flushPromises()
    expect(mocks.save!.mock.calls[1]![2]).toBe(first)
    expect(sessionStorage.length).toBe(0)
  })
  it('does not overwrite a competing revision after a 409', async () => {
    mocks.save!.mockRejectedValue(new ApiError(409, 'revision_conflict'))
    await setup()
    await chooseUncertain()
    await findButton('确认保存').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('系统不会覆盖他人修订')
    expect(findButton('预览并保存复核').attributes('disabled')).toBeDefined()
    expect(mocks.save).toHaveBeenCalledOnce()
  })
  it('refuses a changed source during preflight before sending any POST', async () => {
    await setup()
    await chooseUncertain()
    mocks.review!.mockResolvedValue({
      ...view(),
      source: { ...source, canonical_sha256: 'changed' },
    })
    await findButton('确认保存').trigger('click')
    await flushPromises()
    expect(mocks.save).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('刷新后请重新选择证据')
  })
  it('reports unavailable source without rendering fake empty content', async () => {
    mocks.page!.mockRejectedValue(new ApiError(503, 'unavailable'))
    await setup()
    expect(wrapper.find('[data-source-pointer]').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('暂时无法显示')
  })
})
it.each(['application/pdf', 'image/png', 'image/jpeg'])(
  'releases the authenticated %s Blob preview on unmount',
  async (type) => {
    const create = vi.fn(() => 'blob:synthetic'),
      revoke = vi.fn()
    URL.createObjectURL = create
    URL.revokeObjectURL = revoke
    mocks.file!.mockResolvedValue(new Blob(['synthetic'], { type }))
    wrapper = mount(FilePreview, {
      props: { sid: 's', name: 'synthetic', type },
      global: { stubs: { ElButton: button, ElDialog: dialog } },
    })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(create).toHaveBeenCalledOnce()
    wrapper.unmount()
    expect(revoke).toHaveBeenCalledWith('blob:synthetic')
  },
)
