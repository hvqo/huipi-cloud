"""Stable failures for candidate grading, provider calls, and proposal storage."""


class GradeProposalError(Exception):
    """Base class with a safe machine-readable failure state."""

    status = "invalid_model_response"

    def __init__(self, reason_code: str | None = None) -> None:
        self.reason_code = reason_code or self.status
        super().__init__(self.reason_code)


class GradeProposalNotFoundError(GradeProposalError):
    status = "input_stale"


class GradeProposalInputStaleError(GradeProposalError):
    status = "input_stale"


class GradeProposalInputTooLargeError(GradeProposalError):
    status = "input_too_large"


class GradeProposalIdempotencyConflictError(GradeProposalError):
    status = "request_id_conflict"


class GradeProposalCommitOutcomeUnknownError(GradeProposalError):
    status = "commit_outcome_unknown"


class GradeProposalRegistrationRolledBackError(GradeProposalError):
    status = "proposal_registration_failed"


class GradeProposalArtifactCorruptError(GradeProposalError):
    status = "proposal_integrity_failed"


class GradeProposalProviderError(GradeProposalError):
    status = "provider_unavailable"


class GradeProposalProviderTimeoutError(GradeProposalProviderError):
    status = "provider_timeout"


class GradeProposalProviderRateLimitedError(GradeProposalProviderError):
    status = "provider_rate_limited"


class GradeProposalInvalidModelResponseError(GradeProposalError):
    status = "invalid_model_response"


class GradeProposalInvalidRubricScoresError(GradeProposalError):
    status = "invalid_rubric_scores"


class GradeProposalInvalidEvidenceError(GradeProposalError):
    status = "invalid_evidence_reference"


class GradeProposalAbstainedError(GradeProposalError):
    status = "grading_abstained"
