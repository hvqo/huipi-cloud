MARK_ACTIONS = {
    code: frozenset({"installation", "exam_event", "exam_session"})
    for code in ("exam.mark.read", "exam.answer.review", "exam.score.request", "exam.score.review")
}
MARK_OPERATOR_ROLE = "11111111-b14c-4000-8000-000000000001"
MARK_DEPLOYER_ROLE = "11111111-b14c-4000-8000-000000000002"
