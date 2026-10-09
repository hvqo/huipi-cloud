"""Safe service errors for answer-alignment execution and reads."""


class AnswerAlignmentNotFoundError(Exception):
    """The requested Submission does not exist."""


class AnswerAlignmentNotReadyError(Exception):
    """A verified Canonical document is not available for this Submission."""


class AnswerAlignmentResultNotFoundError(Exception):
    """No result exists for the current Canonical and Question-set versions."""


class AnswerAlignmentArtifactCorruptError(Exception):
    """An indexed S3 result failed size, checksum, or protocol validation."""


class AnswerAlignmentLimitError(Exception):
    """A safe configured size or structural limit was exceeded."""


class AnswerAlignmentResponseTooLargeError(Exception):
    """One question response is larger than the configured API response limit."""
