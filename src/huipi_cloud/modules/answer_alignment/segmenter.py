"""Build non-overlapping answer regions from candidate boundaries."""

from collections import defaultdict
from uuid import UUID

from huipi_cloud.modules.answer_alignment.detector import (
    detect_question_candidates,
)
from huipi_cloud.modules.answer_alignment.matcher import (
    CandidateAssessment,
    assess_candidates,
    assignment_questions_digest,
    mark_review_required,
)
from huipi_cloud.modules.answer_alignment.protocol import (
    ALIGNER_VERSION,
    ALIGNMENT_SCHEMA_NAME,
    ALIGNMENT_SCHEMA_VERSION,
    AlignedAnswer,
    AnswerAlignmentDocument,
    QuestionEvidence,
    SourceRegion,
    UnassignedRegion,
)
from huipi_cloud.modules.answer_alignment.structure import SourceAtom
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.canonical_documents.protocol import CanonicalDocument


def align_canonical_document(
    canonical: CanonicalDocument,
    questions: list[Question],
    *,
    assignment_id: UUID,
    canonical_sha256: str,
    alignment_id: UUID,
) -> AnswerAlignmentDocument:
    """Create a deterministic result with explicit review for weak boundaries."""

    atoms, candidates = detect_question_candidates(canonical)
    assessments = assess_candidates(candidates, atoms, questions)
    assessments = _review_complex_splits(assessments, atoms)
    answer_data: dict[UUID, dict[str, object]] = {
        question.id: {
            "question": question,
            "evidence": [],
            "regions": [],
            "review": False,
        }
        for question in questions
    }
    unassigned: list[UnassignedRegion] = []
    evidence_by_candidate: dict[str, QuestionEvidence] = {}
    for assessment in assessments:
        atom = atoms[assessment.candidate.atom_index]
        evidence = assessment.to_evidence(atom)
        evidence_by_candidate[assessment.candidate.candidate_id] = evidence
        if assessment.question_id is not None:
            target = answer_data[assessment.question_id]
            target["evidence"].append(evidence)  # type: ignore[union-attr]
            if assessment.matching_status == "review_required":
                target["review"] = True

    if assessments:
        first = assessments[0].candidate
        leading_regions = _regions_between(atoms, (0, 0), (first.atom_index, first.text_start))
        if leading_regions:
            unassigned.append(
                UnassignedRegion(
                    reason_code="content_before_first_question_candidate",
                    source_regions=leading_regions,
                    text_projection=_text_projection(leading_regions),
                )
            )
    elif atoms:
        regions = _regions_between(atoms, (0, 0), (len(atoms), 0))
        if regions:
            unassigned.append(
                UnassignedRegion(
                    reason_code="no_question_number_candidate",
                    source_regions=regions,
                    text_projection=_text_projection(regions),
                )
            )

    for position, assessment in enumerate(assessments):
        candidate = assessment.candidate
        next_candidate = (
            assessments[position + 1].candidate if position + 1 < len(assessments) else None
        )
        start = (candidate.atom_index, candidate.text_end)
        end = (
            (next_candidate.atom_index, next_candidate.text_start)
            if next_candidate is not None
            else (len(atoms), 0)
        )
        regions = _regions_between(atoms, start, end)
        evidence = evidence_by_candidate[candidate.candidate_id]
        if assessment.question_id is None:
            unassigned.append(
                UnassignedRegion(
                    reason_code="question_number_not_in_assignment",
                    evidence=evidence,
                    source_regions=regions,
                    text_projection=_text_projection(regions),
                )
            )
            continue

        target = answer_data[assessment.question_id]
        target["regions"].extend(regions)  # type: ignore[union-attr]
        if not regions or not any(region.text and region.text.strip() for region in regions):
            if not any(region.asset_refs for region in regions):
                target["review"] = True

    answers: list[AlignedAnswer] = []
    for question in sorted(questions, key=lambda item: (item.question_number, str(item.id))):
        data = answer_data[question.id]
        evidence = data["evidence"]
        regions = data["regions"]
        if not evidence:
            match_status = "not_observed"
        elif data["review"]:
            match_status = "review_required"
        else:
            match_status = "aligned"
        answers.append(
            AlignedAnswer(
                question_id=question.id,
                question_number=question.question_number,
                question_type=question.question_type,
                matching_status=match_status,
                evidence=evidence,
                source_regions=regions,
                text_projection=_text_projection(regions),
                asset_refs=_unique_asset_refs(regions),
            )
        )

    status = "complete"
    if unassigned or any(answer.matching_status != "aligned" for answer in answers):
        status = "review_required"
    return AnswerAlignmentDocument(
        schema_name=ALIGNMENT_SCHEMA_NAME,
        schema_version=ALIGNMENT_SCHEMA_VERSION,
        alignment_id=alignment_id,
        submission_id=canonical.submission_id,
        assignment_id=assignment_id,
        canonical_document_id=canonical.document_id,
        canonical_sha256=canonical_sha256,
        aligner_version=ALIGNER_VERSION,
        assignment_questions_digest=assignment_questions_digest(questions),
        status=status,
        candidates=[
            assessment.to_evidence(atoms[assessment.candidate.atom_index])
            for assessment in assessments
        ],
        answers=answers,
        unassigned_regions=unassigned,
    )


def _review_complex_splits(
    assessments: list[CandidateAssessment],
    atoms: list[SourceAtom],
) -> list[CandidateAssessment]:
    by_block: dict[UUID, list[int]] = defaultdict(list)
    for position, assessment in enumerate(assessments):
        atom = atoms[assessment.candidate.atom_index]
        by_block[atom.block.block_id].append(position)

    result = list(assessments)
    asset_blocks = {
        atom.block.block_id for atom in atoms if atom.node.asset_refs
    }
    for block_id, positions in by_block.items():
        if block_id in asset_blocks:
            # An asset attached to a nested parent or a mixed text/asset node
            # has no character-level position. Preserve it, but do not claim
            # that the candidate owns the image without a precise source link.
            for position in positions:
                result[position] = mark_review_required(
                    result[position], "asset_attribution_requires_review"
                )
        if len(positions) < 2:
            continue

        for first_position, second_position in zip(positions, positions[1:]):
            first = assessments[first_position].candidate
            second = assessments[second_position].candidate
            if first.atom_index == second.atom_index:
                continue
            between = atoms[first.atom_index + 1 : second.atom_index]
            complex_boundary = any(
                atom.inside_nontext_structure
                or atom.node.normalized_type not in {"text", "layout"}
                or atom.node.asset_refs
                for atom in between
            )
            if complex_boundary:
                result[first_position] = mark_review_required(
                    result[first_position], "complex_block_split_requires_review"
                )
                result[second_position] = mark_review_required(
                    result[second_position], "complex_block_split_requires_review"
                )

    # A candidate can be syntactically clear but still be an unreliable end
    # boundary (for example, an unknown question number or a possible list
    # item). Keep the following source content unassigned and mark the answer
    # before that boundary for review instead of calling the truncated slice
    # complete.
    for position in range(len(result) - 1):
        current = result[position]
        following = result[position + 1]
        if current.question_id is not None and following.matching_status != "aligned":
            result[position] = mark_review_required(
                current, "answer_end_boundary_is_untrusted"
            )
    return result


def _regions_between(
    atoms: list[SourceAtom],
    start: tuple[int, int],
    end: tuple[int, int],
) -> list[SourceRegion]:
    start_atom, start_offset = start
    end_atom, end_offset = end
    regions: list[SourceRegion] = []
    for atom_index in range(start_atom, min(end_atom + 1, len(atoms))):
        atom = atoms[atom_index]
        lower = start_offset if atom_index == start_atom else 0
        upper = (
            end_offset
            if atom_index == end_atom
            else (len(atom.text) if atom.text is not None else 0)
        )
        if atom.text is None:
            if atom.node.asset_refs:
                regions.append(_make_region(atom, None, None, None))
            continue
        lower = min(max(lower, 0), len(atom.text))
        upper = min(max(upper, lower), len(atom.text))
        if upper == lower and (not atom.node.asset_refs or atom.text):
            continue
        fragment = atom.text[lower:upper]
        if not fragment.strip() and not atom.node.asset_refs:
            continue
        regions.append(_make_region(atom, lower, upper, fragment))
    return regions


def _make_region(
    atom: SourceAtom,
    text_start: int | None,
    text_end: int | None,
    text: str | None,
) -> SourceRegion:
    return SourceRegion(
        page_index=atom.block.page_index,
        source_block_id=atom.block.block_id,
        reading_order=atom.block.reading_order,
        bbox=atom.node.bbox or atom.block.bbox,
        source_type=atom.node.source_type,
        normalized_type=atom.node.normalized_type,
        content_pointer=atom.content_pointer,
        text_start=text_start,
        text_end=text_end,
        text=text,
        asset_refs=atom.node.asset_refs,
    )


def _text_projection(regions: list[SourceRegion]) -> str:
    return "\n".join(
        region.text.strip()
        for region in regions
        if region.text is not None
        and region.text.strip()
        and region.source_type not in {"header", "footer", "page_number"}
    )


def _unique_asset_refs(regions: list[SourceRegion]):
    references = {}
    for region in regions:
        for reference in region.asset_refs:
            key = tuple(sorted(reference.model_dump(mode="json").items()))
            references[key] = reference
    return list(references.values())
