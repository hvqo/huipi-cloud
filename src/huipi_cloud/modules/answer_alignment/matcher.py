"""Question-number matching and auditable ambiguity rules."""

import hashlib
import json
from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from huipi_cloud.modules.answer_alignment.detector import DetectedCandidate
from huipi_cloud.modules.answer_alignment.protocol import QuestionEvidence
from huipi_cloud.modules.answer_alignment.structure import SourceAtom
from huipi_cloud.modules.assignments.models import Question


@dataclass(frozen=True)
class CandidateAssessment:
    candidate: DetectedCandidate
    question_id: UUID | None
    matching_status: str
    reason_codes: tuple[str, ...]

    def to_evidence(self, atom: SourceAtom) -> QuestionEvidence:
        return QuestionEvidence(
            candidate_id=self.candidate.candidate_id,
            question_number=self.candidate.question_number,
            question_id=self.question_id,
            marker_kind=self.candidate.marker_kind,
            marker_text=self.candidate.marker_text,
            page_index=atom.block.page_index,
            source_block_id=atom.block.block_id,
            reading_order=atom.block.reading_order,
            content_pointer=atom.content_pointer,
            text_start=self.candidate.text_start,
            text_end=self.candidate.text_end,
            matching_status=self.matching_status,
            reason_codes=list(self.reason_codes),
        )


def assignment_questions_digest(questions: Sequence[Question]) -> str:
    """Hash the exact Question identities and fields used by this aligner."""

    payload = [
        {
            "id": str(question.id),
            "question_number": question.question_number,
            "question_type": question.question_type,
        }
        for question in sorted(questions, key=lambda item: (item.question_number, str(item.id)))
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assess_candidates(
    candidates: Sequence[DetectedCandidate],
    atoms: Sequence[SourceAtom],
    questions: Sequence[Question],
) -> list[CandidateAssessment]:
    """Match only database question numbers and fail closed on weak evidence."""

    by_number = {question.question_number: question for question in questions}
    positions = {
        question.question_number: position
        for position, question in enumerate(
            sorted(questions, key=lambda item: (item.question_number, str(item.id)))
        )
    }
    counts: dict[int, int] = {}
    for candidate in candidates:
        if candidate.question_number in by_number:
            counts[candidate.question_number] = counts.get(candidate.question_number, 0) + 1

    known_candidates = [
        candidate for candidate in candidates if candidate.question_number in by_number
    ]
    generic_sequence_is_supported = len(known_candidates) >= 2
    assessments: list[CandidateAssessment] = []
    for candidate in candidates:
        question = by_number.get(candidate.question_number)
        if question is None:
            assessments.append(
                CandidateAssessment(
                    candidate,
                    None,
                    "unmatched",
                    ("question_number_not_in_assignment",),
                )
            )
            continue

        reasons: list[str] = []
        if counts[candidate.question_number] > 1:
            reasons.append("duplicate_question_number_candidate")
        if candidate.marker_kind == "wrapped_parentheses":
            reasons.append("parenthesized_marker_may_be_subquestion")
        if candidate.marker_kind != "explicit_chinese":
            # Numbered prose and numbered lists can have the same syntax. A
            # plausible sequence alone cannot prove that this is a Question.
            reasons.append("numbered_marker_may_be_list_item")
            if not generic_sequence_is_supported:
                reasons.append("insufficient_sequence_evidence")

        atom = atoms[candidate.atom_index]
        if atom.block.normalized_type == "list" or atom.inside_list:
            reasons.append("list_item_may_not_be_question_heading")
        assessments.append(
            CandidateAssessment(
                candidate,
                question.id,
                "review_required" if reasons else "aligned",
                tuple(dict.fromkeys(reasons)),
            )
        )

    # Candidate order must agree with the real Assignment question order.
    # Mark both sides of each inversion. Do not reorder content to force a match.
    previous_index: int | None = None
    previous_positions: list[int] = []
    for index, assessment in enumerate(assessments):
        question_number = assessment.candidate.question_number
        if question_number not in positions:
            continue
        current_index = positions[question_number]
        if previous_index is not None and current_index <= previous_index:
            for affected_index in (*previous_positions, index):
                old = assessments[affected_index]
                assessments[affected_index] = _with_reason(old, "question_sequence_out_of_order")
        if previous_index is None or current_index > previous_index:
            previous_index = current_index
            previous_positions = [index]
        else:
            previous_positions.append(index)

    return assessments


def mark_review_required(
    assessment: CandidateAssessment,
    reason_code: str,
) -> CandidateAssessment:
    return _with_reason(assessment, reason_code)


def _with_reason(assessment: CandidateAssessment, reason_code: str) -> CandidateAssessment:
    reasons = tuple(dict.fromkeys((*assessment.reason_codes, reason_code)))
    return CandidateAssessment(
        candidate=assessment.candidate,
        question_id=assessment.question_id,
        matching_status="review_required",
        reason_codes=reasons,
    )
