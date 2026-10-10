"""Safe failure categories for visual evidence analysis."""


class VisualEvidenceError(Exception):
    """Base class carrying a public-safe stable failure code."""

    code = "visual_evidence_failed"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class VisualEvidenceNotFoundError(VisualEvidenceError):
    code = "submission_or_question_not_found"


class VisualEvidenceNotReadyError(VisualEvidenceError):
    code = "verified_alignment_not_ready"


class VisualEvidenceSourceChangedError(VisualEvidenceError):
    code = "source_version_changed"


class VisualEvidenceConflictError(VisualEvidenceError):
    code = "request_id_payload_conflict"


class VisualEvidenceConfigurationError(VisualEvidenceError):
    code = "visual_model_not_configured"


class VisualEvidenceProviderError(VisualEvidenceError):
    code = "visual_model_unavailable"


class VisualEvidenceProtocolError(VisualEvidenceError):
    code = "visual_model_response_invalid"


class VisualEvidenceInputError(VisualEvidenceError):
    code = "visual_input_invalid"


class VisualEvidenceLimitError(VisualEvidenceError):
    code = "visual_input_limit_exceeded"


class VisualEvidenceStorageError(VisualEvidenceError):
    code = "visual_evidence_storage_unavailable"
