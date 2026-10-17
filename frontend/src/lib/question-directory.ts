import type { AssignmentsService, S } from '../services/types'
/** A review may target question 21+. Never treat the first workbench page as the whole assignment. */
export async function questionDirectory(
  assignments: AssignmentsService,
  aid: string,
  page: S['AssignmentWorkbench'],
): Promise<S['AssignmentWorkbench']> {
  if (!page.has_next) return page
  // Existing protected detail endpoint returns the full question collection. The API client
  // applies its JSON byte limit; a read failure is surfaced, not silently truncated.
  const detail = await assignments.detail(aid)
  return {
    ...page,
    questions: detail.questions.map((q) => ({
      id: q.id,
      question_number: q.question_number,
      question_type: q.question_type,
      stem: q.stem,
      max_score: q.max_score,
      rubric_count: q.rubric_criteria.length,
      rubric_truncated: q.rubric_criteria.length > 5,
      rubric_criteria: q.rubric_criteria.slice(0, 5),
    })),
    offset: 0,
    limit: detail.questions.length,
    has_next: false,
    next_offset: null,
  }
}
