"""Submission-specific errors translated at the HTTP boundary."""


class SubmissionNotFoundError(Exception):
    """Raised when a submission does not exist."""


class InvalidUploadError(Exception):
    """Raised when an uploaded file name or file body is invalid."""


class UnsupportedUploadError(Exception):
    """Raised when extension, declared content type, or signature is unsupported."""


class UploadTooLargeError(Exception):
    """Raised when the actual file size exceeds the configured limit."""
