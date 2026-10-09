"""Safe failures for canonical document normalization and reads."""


class CanonicalNormalizationError(Exception):
    """A deterministic source or protocol error with a safe public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CanonicalSourceNotReadyError(Exception):
    """Raised when the submission has no successful parsed source yet."""


class CanonicalDocumentNotFoundError(Exception):
    """Raised when the submission does not exist."""


class CanonicalDocumentNotReadyError(Exception):
    """Raised when a canonical result is not available yet."""


class CanonicalDocumentFailedError(Exception):
    """Raised when the most recent deterministic normalization failed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CanonicalPageNotFoundError(Exception):
    """Raised when a canonical page number is outside the document."""


class CanonicalArtifactCorruptError(Exception):
    """Raised when an indexed canonical object fails integrity verification."""
