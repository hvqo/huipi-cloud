import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import {
  addDecimals,
  compareDecimals,
  formatDecimal,
  sumDecimals,
  validateScoreInput,
} from '../src/lib/decimal'
import { formatScore } from '../src/lib/labels'
import { mapGradeQuote } from '../src/lib/grade-quote'
import { buildCanonical } from '../src/services/mock/canonical'
import { questionDirectory } from '../src/lib/question-directory'
import { createMockServices } from '../src/services/mock'
import { demoState, resetDemoState, idempotent, persist } from '../src/services/mock/state'
import { setDemoRole } from '../src/app/demo-auth'
import { makeReview } from '../src/lib/review'
import { createRealServices } from '../src/services/real'
import { UnsupportedCapabilityError, type S } from '../src/services/types'

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  resetDemoState()
  setDemoRole('teacher')
  vi.useFakeTimers()
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('分值必须精确、显示不能吞掉整数末尾的零', () => {
  it.each(['10', '100', '1000', '10.00'])('%s 正确格式化', (value) =>
    expect(formatScore(value)).toBe(String(Number(value))),
  )
  it('使用整数百分位相加，不会放大10倍或引入浮点误差', () => {
    expect(addDecimals('0.1', '0.2')).toBe('0.30')
    expect(sumDecimals(['5', '5.5', '0.25'])).toBe('10.75')
    expect(compareDecimals('5', '5.00')).toBe(0)
    expect(formatDecimal('10')).toBe('10.00')
  })
  it.each(['-1', 'NaN', 'Infinity', '0.001', '1e3', ''])('拒绝非法分值 %s', (value) =>
    expect(validateScoreInput(value, '10')).not.toBe(''),
  )
  it('拒绝超过上限', () => expect(validateScoreInput('10.01', '10')).toContain('上限'))
})

describe('评分输入与Canonical字符坐标不能混用', () => {
  const pointer = '/pages/2/blocks/0/content'
  const text = '题干😀𠀀é学生答案'
  const location: S['GradeEvidenceLocation'] = {
    criterion_id: 'c',
    content_pointer: pointer,
    page_number: 3,
    segment_index: 7,
    text_start: 0,
    text_end: 7,
    canonical_text_start: 2,
    canonical_text_end: 9,
    canonical_page_path: '/api/v1/submissions/s/canonical-document/pages/3',
  }
  it('转换Unicode码点和已验证的局部偏移', () => {
    const result = mapGradeQuote(
      { content_pointer: pointer, text_start: 2, text_end: 5 },
      new Map([[pointer, text]]),
      [location],
    )
    expect(result).toMatchObject({ segment_index: 7, text_start: 0, text_end: 3, quote: '😀𠀀e' })
  })
  it('未知节点和超出评分证据范围时拒绝', () => {
    expect(() =>
      mapGradeQuote(
        { content_pointer: pointer, text_start: 0, text_end: 3 },
        new Map([[pointer, text]]),
        [location],
      ),
    ).toThrow('映射')
    expect(() =>
      mapGradeQuote(
        { content_pointer: pointer, text_start: 2, text_end: 50 },
        new Map([[pointer, text]]),
        [location],
      ),
    ).toThrow('范围')
  })
  it('跨页指针必须解析到真正的节点，不使用累计block索引', () => {
    const { canonical, answerPointers } = buildCanonical(
      's',
      Array.from({ length: 7 }, (_, i) => ({
        qid: `q${i}`,
        number: i + 1,
        stem: `题${i}`,
        answer: `答${i}`,
      })),
    )
    for (const entry of Object.values(answerPointers)) {
      const [, , p, , b] = entry.pointer.split('/')
      expect(canonical.pages[Number(p)]!.page.blocks[Number(b)]!.content.value).toBe(entry.text)
    }
    expect(new Set(canonical.pages.flatMap((p) => p.page.blocks.map((b) => b.block_id))).size).toBe(
      canonical.block_count,
    )
  })
})

describe('演示环境是一致的业务状态，不是静态按钮', () => {
  it('匿名访问与普通教师管理员接口均拒绝', async () => {
    const svc = createMockServices()
    setDemoRole(null)
    await expect(svc.assignments.list({})).rejects.toMatchObject({ status: 401 })
    setDemoRole('teacher')
    await expect(svc.admin.teachers({})).rejects.toMatchObject({ status: 403 })
  })
  it('发布完整性、题号唯一与已发布作业不可改写', async () => {
    const svc = createMockServices()
    const a = await svc.assignments.create({
      title: '合成作业',
      subject: '数学',
      grade_level: '高一',
    })
    await expect(svc.assignments.publish(a.id)).rejects.toMatchObject({ status: 409 })
    const q = await svc.assignments.addQuestion(a.id, {
      question_number: 1,
      question_type: 'short_answer',
      stem: '1+1',
      max_score: '10',
    })
    await expect(
      svc.assignments.addQuestion(a.id, {
        question_number: 1,
        question_type: 'short_answer',
        stem: '重复',
        max_score: '10',
      }),
    ).rejects.toMatchObject({ status: 409 })
    await svc.assignments.replaceAnswerKey(q.id, '2')
    await svc.assignments.replaceRubric(q.id, {
      criteria: [{ description: '正确', points: '10', sort_order: 0 }],
    })
    expect((await svc.assignments.publishCheck(a.id)).ready).toBe(true)
    await svc.assignments.publish(a.id)
    await expect(svc.assignments.replaceAnswerKey(q.id, '3')).rejects.toMatchObject({ status: 409 })
  })
  it('作答复核幂等重放、跨题证据拒绝及不可变历史', async () => {
    const svc = createMockServices()
    const qid = demoState().assignments[0]!.question_ids[0]!
    const view = await svc.reviews.inspect('sub-1', qid)
    const e = view.evidence.find((e) => e.text_end! > e.text_start!)!
    const region = {
      content_pointer: e.content_pointer,
      text_start: e.text_start,
      text_end: e.text_end,
    }
    const body = JSON.stringify(
      makeReview(view, 'response_present', 'student_work_visible', [region]),
    )
    const result = await svc.reviews.save('sub-1', qid, body)
    await expect(svc.reviews.save('sub-1', qid, body)).resolves.toEqual(result)
    expect((await svc.reviews.history('sub-1', qid)).records).toHaveLength(
      view.expected_revision + 1,
    )
    const fresh = await svc.reviews.inspect('sub-1', qid)
    const invalid = makeReview(fresh, 'response_present', 'student_work_visible', [
      { ...region, content_pointer: '/pages/99/blocks/0/content' },
    ])
    await expect(svc.reviews.save('sub-1', qid, JSON.stringify(invalid))).rejects.toMatchObject({
      status: 422,
    })
    await expect(
      svc.reviews.save(
        'sub-1',
        qid,
        JSON.stringify({ ...JSON.parse(body), request_id: crypto.randomUUID() }),
      ),
    ).rejects.toMatchObject({ status: 409 })
  })
  it('评分接受、拒绝与重新评分保存完整前后关系而不发布成绩', async () => {
    const svc = createMockServices()
    const p = demoState().proposals.find(
      (p) => p.grading_status === 'completed_candidate' && p.source_status === 'current',
    )!
    const first = await svc.grading.save(
      p.submission_id,
      p.question_id,
      p.id,
      JSON.stringify({
        request_id: crypto.randomUUID(),
        expected_revision: p.reviews.length,
        review_status: 'accepted',
      }),
    )
    const second = await svc.grading.save(
      p.submission_id,
      p.question_id,
      p.id,
      JSON.stringify({
        request_id: crypto.randomUUID(),
        expected_revision: p.reviews.length,
        review_status: 'rejected',
        review_reason_codes: ['score_disagreement'],
      }),
    )
    expect(first.grade_published).toBe(false)
    expect(second.record.supersedes_review_id).toBe(first.record.review_id)
    expect(first.record.review_status).toBe('accepted')
    expect(second.record.proposed_total_score).toBe(p.proposed_total_score)
  })
  it('修改正分但无引文时拒绝，不留下半成品', async () => {
    const svc = createMockServices()
    const p = demoState().proposals.find(
      (p) =>
        p.grading_status === 'completed_candidate' &&
        p.source_status === 'current' &&
        p.criteria.some((c) => compareDecimals(c.proposed_points, '1') > 0),
    )!
    const before = p.reviews.length
    const rows = p.criteria.map((c, i) => ({
      criterion_id: c.criterion_id,
      reviewed_points: i ? '0' : '1',
      rationale: '合成修改',
      evidence_references: null,
    }))
    await expect(
      svc.grading.save(
        p.submission_id,
        p.question_id,
        p.id,
        JSON.stringify({
          request_id: crypto.randomUUID(),
          expected_revision: before,
          review_status: 'revised',
          review_reason_codes: ['score_disagreement'],
          criterion_reviews: rows,
        }),
      ),
    ).rejects.toMatchObject({ status: 422 })
    expect(p.reviews).toHaveLength(before)
  })
  it('管理操作授权撤销立即生效，重放不会重复审计', async () => {
    setDemoRole('admin')
    const svc = createMockServices()
    const rid = crypto.randomUUID()
    const result = await svc.admin.register({
      request_id: rid,
      issuer: 'https://idp.huipi-demo.cn',
      subject: 'synthetic-new-user',
    })
    await expect(
      svc.admin.register({
        request_id: rid,
        issuer: 'https://idp.huipi-demo.cn',
        subject: 'synthetic-new-user',
      }),
    ).resolves.toEqual(result)
    expect(demoState().audit.filter((e) => e.request_id === rid)).toHaveLength(1)
    await svc.admin.revoke('asg-1', 'u-teacher-li', crypto.randomUUID())
    setDemoRole('teacher')
    await expect(svc.submissions.detail('sub-1')).rejects.toMatchObject({ status: 404 })
    await expect(
      svc.reviews.inspect('sub-1', demoState().assignments[0]!.question_ids[0]!),
    ).rejects.toMatchObject({ status: 404 })
  })
  it('不同目标复用request_id被拒绝，缓存不能被调用者修改', () => {
    const value = idempotent('req', { target: 1 }, () => ({ nested: { value: 1 } }))
    value.nested.value = 9
    expect(idempotent('req', { target: 1 }, () => ({ nested: { value: 2 } }))).toEqual({
      nested: { value: 1 },
    })
    expect(() => idempotent('req', { target: 2 }, () => null)).toThrow('不同内容')
    persist()
    expect(idempotent('req', { target: 1 }, () => ({ nested: { value: 2 } }))).toEqual({
      nested: { value: 1 },
    })
  })
  it('取消助教不会留下半条消息或伪造回答', async () => {
    const svc = createMockServices()
    const c = await svc.assistant.newConversation()
    const abort = new AbortController()
    const pending = svc.assistant.send(c.id, '切线方程', abort.signal)
    abort.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(await svc.assistant.messages(c.id)).toHaveLength(0)
  })
  it('无检索证据时明确拒绝编造，删除资料后不再命中', async () => {
    const svc = createMockServices()
    const hits = await svc.knowledge.retrieve('切线方程')
    expect(hits.length).toBeGreaterThan(0)
    for (const id of new Set(hits.map((h) => h.doc_id))) await svc.knowledge.remove(id)
    expect(await svc.knowledge.retrieve('木星大气探测器xyz')).toHaveLength(0)
    const c = await svc.assistant.newConversation()
    const pending = svc.assistant.send(c.id, '木星大气探测器xyz')
    await vi.advanceTimersByTimeAsync(4000)
    const result = await pending
    expect(result.citations).toHaveLength(0)
    expect(result.content).toMatch(/没有|未找到|缺少/)
  })
})

it('独立图片证据可复核，不能移借到另一题，重复图片被拒绝', async () => {
  const svc = createMockServices(),
    qid = 'q-1-3'
  const view = await svc.reviews.inspect('sub-1', qid)
  const image = view.evidence.find((e) => e.normalized_type === 'image')!
  expect(image).toBeTruthy()
  const region = { content_pointer: image.content_pointer }
  const request = makeReview(view, 'response_present', 'diagram_marks_visible', [region])
  await expect(svc.reviews.save('sub-1', qid, JSON.stringify(request))).resolves.toMatchObject({
    record: { decision: 'response_present' },
  })
  const other = await svc.reviews.inspect('sub-1', 'q-1-2')
  await expect(
    svc.reviews.save(
      'sub-1',
      'q-1-2',
      JSON.stringify(makeReview(other, 'response_present', 'diagram_marks_visible', [region])),
    ),
  ).rejects.toMatchObject({ status: 422 })
  const fresh = await svc.reviews.inspect('sub-1', qid)
  await expect(
    svc.reviews.save(
      'sub-1',
      qid,
      JSON.stringify(
        makeReview(fresh, 'response_present', 'diagram_marks_visible', [region, region]),
      ),
    ),
  ).rejects.toMatchObject({ status: 422 })
})

it('作答复核版本改变后既有评分候选过期，不能继续接受旧分数', async () => {
  const svc = createMockServices(),
    sid = 'sub-1',
    qid = 'q-1-3'
  const proposal = demoState().proposals.find(
    (p) => p.submission_id === sid && p.question_id === qid,
  )!
  expect(proposal.source_status).toBe('current')
  const view = await svc.reviews.inspect(sid, qid)
  const e = view.evidence.find((e) => e.normalized_type === 'text')!
  await svc.reviews.save(
    sid,
    qid,
    JSON.stringify(
      makeReview(view, 'uncertain', 'source_quality_insufficient', [
        { content_pointer: e.content_pointer, text_start: e.text_start, text_end: e.text_end },
      ]),
    ),
  )
  expect((await svc.grading.inspect(sid, qid, proposal.id)).source_status).toBe('source_stale')
  await expect(
    svc.grading.save(
      sid,
      qid,
      proposal.id,
      JSON.stringify({
        request_id: crypto.randomUUID(),
        expected_revision: proposal.reviews.length,
        review_status: 'accepted',
      }),
    ),
  ).rejects.toMatchObject({ status: 409 })
})

describe('真实模式不能失败后降级至合成数据', () => {
  it.each(['knowledge', 'assistant', 'insights'] as const)(
    '%s 缺失HTTP端点明确不可用',
    async (domain) => {
      const svc = createRealServices()
      const call = Promise.resolve().then<unknown>(() =>
        domain === 'knowledge'
          ? svc.knowledge.list()
          : domain === 'assistant'
            ? svc.assistant.conversations()
            : svc.insights.summary('7d'),
      )
      await expect(call).rejects.toBeInstanceOf(UnsupportedCapabilityError)
    },
  )
})

it('超过20题时读取完整受保护目录，后续题目仍可定位和导航', async () => {
  const svc = createMockServices()
  const a = await svc.assignments.create({
    title: '合成21题作业',
    subject: '数学',
    grade_level: '高一',
  })
  for (let i = 1; i <= 21; i++)
    await svc.assignments.addQuestion(a.id, {
      question_number: i,
      question_type: 'short_answer',
      stem: `题${i}`,
      max_score: '1',
    })
  const first = await svc.assignments.workbench(a.id, 0)
  expect(first.questions).toHaveLength(20)
  expect(first.has_next).toBe(true)
  const directory = await questionDirectory(svc.assignments, a.id, first)
  expect(directory.questions).toHaveLength(21)
  expect(directory.questions[20]?.question_number).toBe(21)
  expect(directory.has_next).toBe(false)
})
