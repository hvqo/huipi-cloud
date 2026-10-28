"""Default legacy-only ORM boundary, including projections, aliases and aggregates.

There is intentionally no execution-option bypass. Fixed Core queries in the
trusted identity/security boundary inspect classification or create protected resources.
This is an application boundary, not RLS against a database administrator.
"""

from functools import lru_cache

from sqlalchemy import and_, event, exists, false, or_, select
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.sql import visitors

LEGACY = "legacy_homework"


@lru_cache(maxsize=1)
def rules():
    from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
    from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
    from huipi_cloud.modules.assignments.models import (
        AnswerKey,
        Assignment,
        Question,
        RubricCriterion,
    )
    from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
    from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
    from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
    from huipi_cloud.modules.identity.models import AdminOperation, AssignmentMembership
    from huipi_cloud.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument
    from huipi_cloud.modules.parsing.models import ParsedArtifact
    from huipi_cloud.modules.submissions.models import ParsingTask, Submission, SubmissionFile
    from huipi_cloud.modules.visual_evidence.models import VisualEvidenceArtifact

    a, s, q, d = (
        m.__table__.alias("_security_" + m.__tablename__)
        for m in (Assignment, Submission, Question, KnowledgeDocument)
    )

    def assignment(column):
        return exists(
            select(1)
            .select_from(a)
            .where(
                a.c.id == column,
                a.c.security_classification == LEGACY,
            )
            .correlate_except(a)
        )

    def submission(column):
        return exists(
            select(1)
            .select_from(s)
            .where(
                s.c.id == column,
                assignment(s.c.assignment_id),
            )
            .correlate_except(s)
        )

    def question(column):
        return exists(
            select(1)
            .select_from(q)
            .where(
                q.c.id == column,
                assignment(q.c.assignment_id),
            )
            .correlate_except(q)
        )

    from huipi_cloud.modules.exam_grading.models import (
        ExamGradingOperation,
        ExamGradingWorkspace,
        ExamScoringJob,
        ExamVisualRegion,
        ExamVisualSelection,
    )
    from huipi_cloud.modules.exam_results.models import ExamResultClassAccess

    result = [
        (ExamResultClassAccess, false()),
        (Assignment, Assignment.security_classification == LEGACY),
        (ExamGradingWorkspace, false()),
        (ExamScoringJob, false()),
        (ExamGradingOperation, false()),
        (ExamVisualSelection, false()),
        (ExamVisualRegion, false()),
    ]
    from huipi_cloud.modules.exam_parsing.models import ExamSubmissionLink

    models = (
        ExamSubmissionLink,
        Question,
        AnswerKey,
        RubricCriterion,
        Submission,
        SubmissionFile,
        ParsingTask,
        ParsedArtifact,
        CanonicalArtifact,
        AnswerAlignmentArtifact,
        AnswerReviewDecision,
        GradeInputArtifact,
        GradeProposalArtifact,
        GradeReviewRevision,
        VisualEvidenceArtifact,
        KnowledgeDocument,
        AssignmentMembership,
    )
    for model in models:
        predicates = []
        # Check actual parents, never only the denormalized assignment_id.
        for field, predicate in (
            ("assignment_id", assignment),
            ("submission_id", submission),
            ("question_id", question),
        ):
            if hasattr(model, field):
                predicates.append(predicate(getattr(model, field)))
        result.append((model, and_(*predicates)))
    result.append(
        (
            KnowledgeChunk,
            exists(
                select(1)
                .select_from(d)
                .where(
                    d.c.id == KnowledgeChunk.document_version_id,
                    assignment(d.c.assignment_id),
                )
                .correlate_except(d)
            ),
        )
    )
    result.append(
        (
            AdminOperation,
            or_(
                AdminOperation.assignment_id.is_(None),
                assignment(AdminOperation.assignment_id),
            ),
        )
    )
    return tuple(result)


@event.listens_for(Session, "do_orm_execute")
def legacy_visibility(state):
    if state.is_select or state.is_update or state.is_delete:
        # Only target mappers actually present in the caller's statement. Applying
        # unrelated parent criteria inside each other's EXISTS can recurse forever.
        targets = {mapper.class_ for mapper in state.all_mappers}
        for element in visitors.iterate(state.statement):
            mapper = element._annotations.get("parentmapper")
            if mapper is not None:
                targets.add(mapper.class_)
        state.statement = state.statement.options(
            *(
                with_loader_criteria(
                    model, predicate, include_aliases=True, propagate_to_loaders=False
                )
                for model, predicate in rules()
                if model in targets
            )
        )
