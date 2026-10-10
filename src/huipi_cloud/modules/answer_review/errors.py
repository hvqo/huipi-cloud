"""Safe errors raised by the local human-review workflow."""


class AnswerReviewSubmissionNotFoundError(Exception):
    """The requested submission does not exist."""


class AnswerReviewQuestionNotFoundError(Exception):
    """The Question does not belong to the requested Submission's Assignment."""


class AnswerReviewSourceNotReadyError(Exception):
    """The current Canonical or Answer Alignment source is not available."""


class AnswerReviewSourceChangedError(Exception):
    """The source changed while a reviewer was preparing a decision."""


class AnswerReviewRegionInvalidError(Exception):
    """A selected source path or character range does not exist."""


class AnswerReviewIdempotencyConflictError(Exception):
    """An idempotency request ID was reused with a different payload."""
