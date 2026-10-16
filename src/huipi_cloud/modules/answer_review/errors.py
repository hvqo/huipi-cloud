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


class AnswerReviewRevisionConflictError(Exception):
    """The HTTP client edited an outdated revision; inspect again."""


class AnswerReviewUnavailableError(Exception):
    """A database operation or commit result is unavailable; retry the same request ID."""
