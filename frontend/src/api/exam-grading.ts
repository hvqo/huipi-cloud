import { request, id, type Schema } from './client'
export interface MarkQuestionLink {
  question_id: string
  question_number: number
  stem: string
  max_score: string
  workspace_id: string | null
}
export const examGrading = {
  questions: (job: string) =>
    request<{ questions: MarkQuestionLink[] }>(
      `/api/v1/exam-parsing-jobs/${id(job)}/grading-questions`,
    ),
  initialize: (job: string, request_id: string) =>
    request<{ workspace_ids: string[] }>(`/api/v1/exam-parsing-jobs/${id(job)}/grading-workspace`, {
      body: JSON.stringify({ request_id }),
      timeoutMs: 100000,
    }),
  details: (workspace: string) =>
    request<Schema['ExamMarkDetails']>(`/api/v1/exam-grading-workspaces/${id(workspace)}`, {
      timeoutMs: 100000,
    }),
  mutate: (
    workspace: string,
    action: 'answer-review' | 'scoring' | 'score-review',
    body: Schema['AnswerDecision'] | Schema['ScoreRequest'] | Schema['ScoreReview'],
  ) =>
    request<Record<string, unknown>>(`/api/v1/exam-grading-workspaces/${id(workspace)}/${action}`, {
      body: JSON.stringify(body),
      timeoutMs: 100000,
    }),
}
