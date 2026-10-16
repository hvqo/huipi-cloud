import type { Region, Review, ReviewBody } from '../api/client'
export type Decision = ReviewBody['decision']
export const decisions: Record<string, string> = {
  response_present: '存在作答',
  response_absent: '不存在作答',
  uncertain: '不确定',
  unreviewed: '待复核',
}
export const reasons: Record<string, string> = {
  student_work_visible: '可见学生作答',
  printed_prompt_only: '仅有印刷题干',
  blank_response_area: '作答区域为空',
  ambiguous_handwriting: '笔迹难以判断',
  ocr_incomplete: 'OCR 内容不完整',
  formula_only_response: '仅有公式作答',
  table_entry_visible: '可见表格填写',
  diagram_marks_visible: '可见图中标注',
  shared_figure_attribution_unclear: '共享图形归属不明',
  response_not_linked_to_question: '作答与本题关系不明',
  source_quality_insufficient: '来源质量不足',
  multiple_response_regions: '多个作答区域',
  cross_page_response: '跨页作答',
}
export const allowedReasons: Record<Decision, string[]> = {
  response_present: [
    'student_work_visible',
    'formula_only_response',
    'table_entry_visible',
    'diagram_marks_visible',
    'multiple_response_regions',
    'cross_page_response',
  ],
  response_absent: ['printed_prompt_only', 'blank_response_area'],
  uncertain: [
    'ambiguous_handwriting',
    'ocr_incomplete',
    'shared_figure_attribution_unclear',
    'response_not_linked_to_question',
    'source_quality_insufficient',
  ],
}
export function makeReview(
  view: Review,
  decision: Decision,
  reason: string,
  regions: Region[],
  requestId = crypto.randomUUID(),
): ReviewBody {
  if (!allowedReasons[decision].includes(reason)) throw new Error('请选择与审核决定一致的原因。')
  if (
    !regions.length &&
    (decision !== 'uncertain' ||
      !['response_not_linked_to_question', 'source_quality_insufficient'].includes(reason))
  )
    throw new Error('请选择真实证据范围，未选中文字不表示没有作答。')
  return {
    request_id: requestId,
    expected_revision: view.expected_revision,
    expected_source: { ...view.source },
    decision,
    reason_codes: [reason] as ReviewBody['reason_codes'],
    response_regions: decision === 'response_present' ? regions : [],
    excluded_prompt_regions: decision === 'response_absent' ? regions : [],
    uncertain_regions: decision === 'uncertain' ? regions : [],
  }
}
export function sourceMatches(a: Review['source'], b: Review['source']) {
  return Object.keys(a).every((key) => a[key as keyof typeof a] === b[key as keyof typeof b])
}
export class PendingReview {
  body: string | null = null
  constructor(private key: string) {
    const saved = sessionStorage.getItem(key)
    if (saved) {
      try {
        const value = JSON.parse(saved)
        if (typeof value.request_id === 'string') this.body = saved
      } catch {
        sessionStorage.removeItem(key)
      }
    }
  }
  freeze(payload: ReviewBody) {
    if (this.body) throw new Error('请先确认上一笔保存结果。')
    this.body = JSON.stringify(payload)
    sessionStorage.setItem(this.key, this.body)
    return this.body
  }
  clear() {
    this.body = null
    sessionStorage.removeItem(this.key)
  }
}
