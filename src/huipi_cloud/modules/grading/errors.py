"""Stable failures for GradeInput creation and verification."""


class GradeInputError(Exception):
    """Base class for controlled GradeInput failures."""


class GradeInputNotFoundError(GradeInputError):
    """The submission, question, or indexed input does not exist."""


class GradeInputSourceChangedError(GradeInputError):
    """A source version changed between assessment and PostgreSQL registration."""

    def __init__(self, reason_code: str = "grade_input_source_changed") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class GradeInputIdempotencyConflictError(GradeInputError):
    """The request ID is already bound to a different semantic input."""


class GradeInputRegistrationRolledBackError(GradeInputError):
    """Registration failed before COMMIT and the transaction was rolled back."""


class GradeInputCommitOutcomeUnknownError(GradeInputError):
    """COMMIT was attempted but its result was not confirmed."""


class GradeInputArtifactCorruptError(GradeInputError):
    """The indexed S3 object failed size, checksum, or protocol validation."""
