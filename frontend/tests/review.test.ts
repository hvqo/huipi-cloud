import { beforeEach, expect, it } from 'vitest'
import { allowedReasons, makeReview, PendingReview, sourceMatches } from '../src/lib/review'
import type { Review } from '../src/api/client'
const source = {
  canonical_document_id: 'canonical',
  canonical_sha256: 'a'.repeat(64),
  alignment_artifact_id: 'alignment',
  alignment_sha256: 'b'.repeat(64),
  aligner_version: '1',
  assignment_questions_digest: 'c'.repeat(64),
}
const view = { source, expected_revision: 3 } as Review
const region = { content_pointer: '/pages/0/blocks/0/content', text_start: 3, text_end: 7 }
beforeEach(() => sessionStorage.clear())
it.each(['response_present', 'response_absent', 'uncertain'] as const)(
  'builds explicit %s body without actor claims',
  (decision) => {
    const body = makeReview(view, decision, allowedReasons[decision][0]!, [region])
    expect(body.expected_revision).toBe(3)
    expect(body.expected_source).toEqual(source)
    expect(Object.keys(body).sort()).toEqual([
      'decision',
      'excluded_prompt_regions',
      'expected_revision',
      'expected_source',
      'reason_codes',
      'request_id',
      'response_regions',
      'uncertain_regions',
    ])
    expect(
      body[
        decision === 'response_present'
          ? 'response_regions'
          : decision === 'response_absent'
            ? 'excluded_prompt_regions'
            : 'uncertain_regions'
      ],
    ).toEqual([region])
  },
)
it('never interprets an empty selection as student absence or present', () => {
  expect(() => makeReview(view, 'response_absent', 'printed_prompt_only', [])).toThrow()
  expect(() => makeReview(view, 'response_present', 'student_work_visible', [])).toThrow()
  expect(makeReview(view, 'uncertain', 'source_quality_insufficient', []).decision).toBe(
    'uncertain',
  )
  expect(() => makeReview(view, 'response_present', 'printed_prompt_only', [region])).toThrow()
})
it('keeps exactly the same request bytes across unknown result and page reload', () => {
  const pending = new PendingReview('huipi.review.actor.submission.question')
  const body = makeReview(view, 'response_present', 'student_work_visible', [region])
  const bytes = pending.freeze(body)
  body.response_regions![0]!.text_start = 99
  expect(pending.body).toBe(bytes)
  expect(new PendingReview('huipi.review.actor.submission.question').body).toBe(bytes)
  expect(() => pending.freeze(body)).toThrow('上一笔')
  pending.clear()
  expect(pending.body).toBe(null)
})
it('detects changed source versions without loosening the baseline', () => {
  expect(sourceMatches(source, { ...source })).toBe(true)
  expect(sourceMatches(source, { ...source, alignment_sha256: '0'.repeat(64) })).toBe(false)
})
