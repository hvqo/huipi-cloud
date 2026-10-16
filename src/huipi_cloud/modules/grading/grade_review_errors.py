"""Stable failures for manual review of grading proposals."""


class GradeReviewError(Exception):
    status = "review_invalid"

    def __init__(self, reason_code: str | None = None) -> None:
        self.reason_code = reason_code or self.status
        super().__init__(self.reason_code)


class GradeReviewNotFoundError(GradeReviewError):
    status = "review_target_not_found"


class GradeReviewSourceStaleError(GradeReviewError):
    status = "source_stale"


class GradeReviewInvalidDecisionError(GradeReviewError):
    status = "review_decision_invalid"


class GradeReviewConflictError(GradeReviewError):
    status = "review_revision_conflict"


class GradeReviewIdempotencyConflictError(GradeReviewError):
    status = "request_id_conflict"


class GradeReviewCommitOutcomeUnknownError(GradeReviewError):
    status = "commit_outcome_unknown"


class GradeReviewRegistrationRolledBackError(GradeReviewError):
    status = "review_registration_failed"


class GradeReviewArtifactCorruptError(GradeReviewError):
    status = "review_history_corrupt"


class GradeReviewUnavailableError(GradeReviewError):
    status = "grade_review_unavailable"
