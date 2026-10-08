"""Assignment-domain errors translated to HTTP responses at the API boundary."""


class AssignmentNotFoundError(Exception):
    """Raised when an assignment or question identifier does not exist."""


class AssignmentConflictError(Exception):
    """Raised when an operation conflicts with the current assignment state."""


class AssignmentValidationError(Exception):
    """Raised when cross-field domain validation fails."""
