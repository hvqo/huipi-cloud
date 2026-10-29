"""Teacher-scoped homework delivery and versioned uploads."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.modules.assignments.models import AnswerKey, Assignment, Question, RubricCriterion
from huipi_cloud.modules.identity import admin_service
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import (
    AcademicTerm,
    AdministrativeClass,
    ClassEnrollment,
    Student,
    Subject,
)
from huipi_cloud.modules.submissions.models import ParsingTask, Submission, SubmissionFile
from huipi_cloud.modules.submissions.uploads import stage_upload

from . import access
from .models import (
    HomeworkAttempt,
    HomeworkClass,
    HomeworkOffering,
    HomeworkParticipant,
    TeachingAssignment,
)

T, OFFER, C, P, A = (
    m.__table__
    for m in (
        TeachingAssignment,
        HomeworkOffering,
        HomeworkClass,
        HomeworkParticipant,
        HomeworkAttempt,
    )
)


def today():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def create_question(db, principal, assignment_id, payload):
    try:
        async with db.begin():
            await identity.require_assignment(db, principal, assignment_id, write=True, lock=True)
            assignment = await db.scalar(
                select(Assignment).where(Assignment.id == assignment_id).with_for_update()
            )
            if assignment is None or assignment.status != "draft":
                raise AccessError("homework_assignment_not_draft", 409)
            question = Question(assignment_id=assignment_id, **payload.question.model_dump())
            db.add(question)
            await db.flush()
            db.add(AnswerKey(question_id=question.id, answer_content=payload.answer.answer_content))
            db.add_all(
                RubricCriterion(question_id=question.id, **item.model_dump())
                for item in payload.rubric.criteria
            )
            await db.flush()
            return {"question_id": str(question.id)}
    except IntegrityError:
        raise AccessError("homework_question_conflict", 409) from None


async def grant_teaching(db, principal, payload):
    async with admin_service.administrator_transaction(db, principal) as actor:
        teacher = await db.scalar(
            select(User)
            .where(User.id == payload.teacher_id, User.is_active, User.is_admin.is_(False))
            .with_for_update(read=True)
        )
        classroom = await db.scalar(
            select(AdministrativeClass)
            .where(
                AdministrativeClass.id == payload.class_id, AdministrativeClass.status == "active"
            )
            .with_for_update(read=True)
        )
        term = await db.scalar(
            select(AcademicTerm)
            .where(AcademicTerm.id == payload.term_id)
            .with_for_update(read=True)
        )
        subject = await db.scalar(
            select(Subject)
            .where(Subject.id == payload.subject_id, Subject.is_active)
            .with_for_update(read=True)
        )
        if not all((teacher, classroom, term, subject)):
            raise AccessError("homework_teaching_source_invalid", 404)
        existing = await db.scalar(
            select(T.c.id).where(
                T.c.teacher_id == teacher.id,
                T.c.class_id == classroom.id,
                T.c.term_id == term.id,
                T.c.subject_id == subject.id,
                T.c.revoked_at.is_(None),
            )
        )
        if existing:
            return {"id": str(existing), "created": False}
        identifier = uuid4()
        await db.execute(
            insert(T).values(
                id=identifier,
                teacher_id=teacher.id,
                class_id=classroom.id,
                term_id=term.id,
                subject_id=subject.id,
                granted_by=actor.user_id,
            )
        )
        return {"id": str(identifier), "created": True}


async def revoke_teaching(db, principal, identifier):
    async with admin_service.administrator_transaction(db, principal) as actor:
        result = await db.execute(
            update(T)
            .where(T.c.id == identifier, T.c.revoked_at.is_(None))
            .values(revoked_at=func.now(), revoked_by=actor.user_id)
            .returning(T.c.id)
        )
        if result.scalar_one_or_none() is None:
            raise AccessError(access.DENIED, 404)
        return {"revoked": True}


async def admin_teaching_options(db, principal):
    """Only an active database administrator can provision teaching relationships."""
    async with admin_service.administrator_transaction(db, principal):
        teachers = (
            await db.scalars(
                select(User)
                .where(User.is_active, User.is_admin.is_(False))
                .order_by(User.created_at, User.id)
                .limit(101)
            )
        ).all()
        classrooms = (
            await db.scalars(
                select(AdministrativeClass)
                .where(AdministrativeClass.status == "active")
                .order_by(AdministrativeClass.display_name, AdministrativeClass.id)
                .limit(101)
            )
        ).all()
        terms = (
            await db.scalars(
                select(AcademicTerm)
                .where(AcademicTerm.start_date <= today(), AcademicTerm.end_date >= today())
                .order_by(AcademicTerm.code, AcademicTerm.id)
                .limit(101)
            )
        ).all()
        subjects = (
            await db.scalars(
                select(Subject)
                .where(Subject.is_active)
                .order_by(Subject.display_name, Subject.id)
                .limit(101)
            )
        ).all()
        relationships = (
            (
                await db.execute(
                    select(T)
                    .where(T.c.revoked_at.is_(None))
                    .order_by(T.c.created_at, T.c.id)
                    .limit(101)
                )
            )
            .mappings()
            .all()
        )
        return {
            "teachers": [{"id": x.id} for x in teachers[:100]],
            "classes": [{"id": x.id, "name": x.display_name} for x in classrooms[:100]],
            "terms": [{"id": x.id, "name": x.code} for x in terms[:100]],
            "subjects": [{"id": x.id, "name": x.display_name} for x in subjects[:100]],
            "assignments": [dict(x) for x in relationships[:100]],
            "truncated": any(
                len(x) > 100 for x in (teachers, classrooms, terms, subjects, relationships)
            ),
        }


async def teaching_options(db, principal):
    async with db.begin():
        await identity.refresh_principal(db, principal, lock=True)
        rows = (
            (
                await db.execute(
                    select(
                        T.c.id,
                        T.c.class_id,
                        T.c.term_id,
                        T.c.subject_id,
                        AdministrativeClass.display_name.label("class_name"),
                        AcademicTerm.code.label("term_name"),
                        Subject.display_name.label("subject_name"),
                        AcademicTerm.start_date,
                        AcademicTerm.end_date,
                    )
                    .join(AdministrativeClass, AdministrativeClass.id == T.c.class_id)
                    .join(AcademicTerm, AcademicTerm.id == T.c.term_id)
                    .join(Subject, Subject.id == T.c.subject_id)
                    .where(
                        T.c.teacher_id == principal.user_id,
                        T.c.revoked_at.is_(None),
                        AdministrativeClass.status == "active",
                        Subject.is_active,
                        AcademicTerm.start_date <= today(),
                        AcademicTerm.end_date >= today(),
                    )
                    .order_by(
                        AcademicTerm.code, Subject.display_name, AdministrativeClass.display_name
                    )
                    .limit(101)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 100:
            raise AccessError("homework_teaching_limit", 413)
        return {"items": [dict(row) for row in rows]}


async def create_offering(db, principal, assignment_id, payload):
    try:
        async with db.begin():
            await identity.require_assignment(db, principal, assignment_id, write=True, lock=True)
            assignment = await db.scalar(
                select(Assignment).where(Assignment.id == assignment_id).with_for_update()
            )
            if assignment is None or assignment.status != "published":
                raise AccessError("homework_assignment_not_published", 409)
            classes = await access.teaching_classes(
                db, principal, payload.term_id, payload.subject_id, lock=True
            )
            if not set(payload.class_ids) <= classes:
                raise AccessError(access.DENIED, 404)
            term = await db.scalar(
                select(AcademicTerm)
                .where(AcademicTerm.id == payload.term_id)
                .with_for_update(read=True)
            )
            subject = await db.scalar(
                select(Subject)
                .where(Subject.id == payload.subject_id, Subject.is_active)
                .with_for_update(read=True)
            )
            if (
                not term
                or not subject
                or not (term.start_date <= today() <= term.end_date)
                or subject.display_name != assignment.subject
            ):
                raise AccessError("homework_term_or_subject_mismatch", 409)
            if payload.due_at <= datetime.now(UTC):
                raise AccessError("homework_deadline_invalid", 422)
            existing = (
                (await db.execute(select(OFFER).where(OFFER.c.assignment_id == assignment_id)))
                .mappings()
                .first()
            )
            if existing:
                old_classes = set(
                    (
                        await db.scalars(
                            select(C.c.class_id).where(C.c.offering_id == existing["id"])
                        )
                    ).all()
                )
                if (
                    existing["term_id"] == payload.term_id
                    and existing["subject_id"] == payload.subject_id
                    and existing["due_at"] == payload.due_at
                    and old_classes == set(payload.class_ids)
                ):
                    return {"offering_id": str(existing["id"]), "created": False}
                raise AccessError("homework_already_assigned", 409)
            classroom_rows = (
                await db.execute(
                    select(AdministrativeClass.id, AdministrativeClass.display_name)
                    .where(
                        AdministrativeClass.id.in_(payload.class_ids),
                        AdministrativeClass.status == "active",
                    )
                    .order_by(AdministrativeClass.id)
                    .with_for_update(read=True)
                )
            ).all()
            if len(classroom_rows) != len(payload.class_ids):
                raise AccessError("homework_class_unavailable", 409)
            rows = (
                await db.execute(
                    select(
                        ClassEnrollment.id,
                        ClassEnrollment.administrative_class_id,
                        Student.id,
                        Student.name,
                        Student.school_student_code,
                    )
                    .join(Student, Student.id == ClassEnrollment.student_id)
                    .where(
                        ClassEnrollment.administrative_class_id.in_(payload.class_ids),
                        ClassEnrollment.valid_from <= today(),
                        (ClassEnrollment.valid_to.is_(None) | (ClassEnrollment.valid_to > today())),
                        Student.is_active,
                    )
                    .order_by(ClassEnrollment.id)
                    .limit(2001)
                    .with_for_update(read=True, of=[ClassEnrollment, Student])
                )
            ).all()
            if not rows or len(rows) > 2000:
                raise AccessError("homework_roster_empty_or_too_large", 409)
            if len({row[2] for row in rows}) != len(rows):
                raise AccessError("homework_student_conflict", 409)
            oid = uuid4()
            await db.execute(
                insert(OFFER).values(
                    id=oid,
                    assignment_id=assignment_id,
                    term_id=term.id,
                    subject_id=subject.id,
                    due_at=payload.due_at,
                    created_by=principal.user_id,
                )
            )
            await db.execute(
                insert(C),
                [
                    dict(id=uuid4(), offering_id=oid, class_id=cid, class_name=name)
                    for cid, name in classroom_rows
                ],
            )
            await db.execute(
                insert(P),
                [
                    dict(
                        id=uuid4(),
                        offering_id=oid,
                        student_id=sid,
                        enrollment_id=eid,
                        class_id=cid,
                        student_name=name,
                        student_code=code,
                    )
                    for eid, cid, sid, name, code in rows
                ],
            )
            return {"offering_id": str(oid), "created": True, "student_count": len(rows)}
    except IntegrityError:
        raise AccessError("homework_conflict", 409) from None


async def read_offering(db, principal, assignment_id, class_id=None, offset=0, limit=50):
    from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision

    async with db.begin():
        offering, classes = await access.offering_access(db, principal, assignment_id)
        if class_id and class_id not in classes:
            raise AccessError(access.DENIED, 404)
        visible = {class_id} if class_id else classes
        rooms = (
            (
                await db.execute(
                    select(C.c.class_id, C.c.class_name)
                    .where(C.c.offering_id == offering["id"], C.c.class_id.in_(classes))
                    .order_by(C.c.class_name)
                )
            )
            .mappings()
            .all()
        )
        participants = (
            (
                await db.execute(
                    select(P)
                    .where(P.c.offering_id == offering["id"], P.c.class_id.in_(visible))
                    .order_by(P.c.student_name, P.c.student_code, P.c.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        counts = (
            (
                await db.execute(
                    select(
                        P.c.class_id,
                        func.count(P.c.id).label("expected"),
                        func.count(A.c.id).label("submitted"),
                        func.sum(case((A.c.submitted_at > offering["due_at"], 1), else_=0)).label(
                            "late"
                        ),
                    )
                    .select_from(P.outerjoin(A, (A.c.participant_id == P.c.id) & A.c.is_current))
                    .where(P.c.offering_id == offering["id"], P.c.class_id.in_(classes))
                    .group_by(P.c.class_id)
                )
            )
            .mappings()
            .all()
        )
        question_count = await db.scalar(
            select(func.count(Question.id)).where(Question.assignment_id == assignment_id)
        )
        latest_reviews = select(
            GradeReviewRevision.submission_id.label("submission_id"),
            GradeReviewRevision.question_id.label("question_id"),
            GradeReviewRevision.review_status.label("review_status"),
            GradeReviewRevision.reviewer_user_id.label("reviewer_user_id"),
            func.row_number()
            .over(
                partition_by=(GradeReviewRevision.submission_id, GradeReviewRevision.question_id),
                order_by=(
                    GradeReviewRevision.created_at.desc(),
                    GradeReviewRevision.review_revision.desc(),
                ),
            )
            .label("row_number"),
        ).subquery()
        reviewed_by_class = dict(
            (
                await db.execute(
                    select(P.c.class_id, func.count())
                    .select_from(
                        P.join(A, (A.c.participant_id == P.c.id) & A.c.is_current)
                        .join(latest_reviews, latest_reviews.c.submission_id == A.c.submission_id)
                        .join(Question, Question.id == latest_reviews.c.question_id)
                    )
                    .where(
                        P.c.offering_id == offering["id"],
                        P.c.class_id.in_(classes),
                        Question.assignment_id == assignment_id,
                        latest_reviews.c.row_number == 1,
                        latest_reviews.c.review_status.in_(["accepted", "revised"]),
                        latest_reviews.c.reviewer_user_id.is_not(None),
                    )
                    .group_by(P.c.class_id)
                )
            ).all()
        )
        result = []
        for row in participants[:limit]:
            attempt = (
                (
                    await db.execute(
                        select(A, ParsingTask.status.label("parsing_status"))
                        .outerjoin(ParsingTask, ParsingTask.submission_id == A.c.submission_id)
                        .where(A.c.participant_id == row["id"], A.c.is_current)
                    )
                )
                .mappings()
                .first()
            )
            status = (
                "not_submitted"
                if not attempt
                else ("late" if attempt["submitted_at"] > offering["due_at"] else "submitted")
            )
            reviewed = 0
            if attempt:
                history = (
                    await db.execute(
                        select(
                            GradeReviewRevision.question_id,
                            GradeReviewRevision.review_status,
                            GradeReviewRevision.reviewer_user_id,
                        )
                        .where(GradeReviewRevision.submission_id == attempt["submission_id"])
                        .order_by(
                            GradeReviewRevision.created_at.desc(),
                            GradeReviewRevision.review_revision.desc(),
                        )
                        .limit(1000)
                    )
                ).all()
                latest = {}
                for qid, review_status, reviewer_id in history:
                    latest.setdefault(qid, (review_status, reviewer_id))
                reviewed = sum(
                    status in {"accepted", "revised"} and reviewer is not None
                    for status, reviewer in latest.values()
                )
            result.append(
                dict(
                    participant_id=row["id"],
                    student_name=row["student_name"],
                    student_code=row["student_code"],
                    class_id=row["class_id"],
                    status=status,
                    current_submission_id=attempt["submission_id"] if attempt else None,
                    version=attempt["version"] if attempt else None,
                    parsing_status=attempt["parsing_status"] if attempt else None,
                    reviewed_count=reviewed,
                    question_count=question_count,
                )
            )
        return dict(
            offering_id=offering["id"],
            due_at=offering["due_at"],
            classes=[dict(r) for r in rooms],
            counts=[
                {
                    **dict(r),
                    "reviewed_records": reviewed_by_class.get(r["class_id"], 0),
                    "submitted_question_count": r["submitted"] * question_count,
                }
                for r in counts
            ],
            students=result,
            offset=offset,
            limit=limit,
            has_next=len(participants) > limit,
        )


async def attempts(db, principal, participant_id):
    async with db.begin():
        row, _ = await access.participant_access(db, principal, participant_id)
        records = (
            (
                await db.execute(
                    select(A.c.submission_id, A.c.version, A.c.is_current, A.c.submitted_at)
                    .where(A.c.participant_id == participant_id)
                    .order_by(A.c.version.desc())
                    .limit(101)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            student_name=row["student_name"],
            student_code=row["student_code"],
            items=[dict(r) for r in records[:100]],
            truncated=len(records) > 100,
        )


async def submission_info(db, principal, submission_id):
    async with db.begin():
        attempt = await access.submission_access(db, principal, submission_id)
        if attempt is None:
            raise AccessError(access.DENIED, 404)
        participant, _ = await access.participant_access(db, principal, attempt["participant_id"])
        return dict(
            participant_id=participant["id"],
            student_name=participant["student_name"],
            student_code=participant["student_code"],
            version=attempt["version"],
            is_current=attempt["is_current"],
        )


async def upload(db, principal, participant_id, request_id, file, storage):
    staged = await stage_upload(file, settings.max_upload_size_bytes)
    object_key = None
    committed = False
    try:
        async with db.begin():
            participant, _ = await access.participant_access(
                db, principal, participant_id, write=True, lock=True
            )
            previous = (
                (
                    await db.execute(
                        select(A, SubmissionFile.original_filename, SubmissionFile.content_type)
                        .join(SubmissionFile, SubmissionFile.submission_id == A.c.submission_id)
                        .where(A.c.actor_id == principal.user_id, A.c.request_id == request_id)
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if (
                    previous["participant_id"] != participant_id
                    or previous["file_sha256"] != staged.sha256
                    or previous["original_filename"] != staged.filename
                    or previous["content_type"] != staged.content_type
                ):
                    raise AccessError("homework_request_conflict", 409)
                return {
                    "submission_id": str(previous["submission_id"]),
                    "version": previous["version"],
                    "is_current": previous["is_current"],
                    "created": False,
                }
        sid, fid, attempt_id = uuid4(), uuid4(), uuid4()
        prefix = storage.object_key_prefix
        key = f"assignments/{participant['assignment_id']}/submissions/{sid}/{fid}"
        object_key = f"{prefix}/{key}" if prefix else key
        await storage.upload_fileobj(staged.fileobj, object_key, staged.content_type)
        await db.begin()
        participant, _ = await access.participant_access(
            db, principal, participant_id, write=True, lock=True
        )
        previous = (
            (
                await db.execute(
                    select(A, SubmissionFile.original_filename, SubmissionFile.content_type)
                    .join(SubmissionFile, SubmissionFile.submission_id == A.c.submission_id)
                    .where(A.c.actor_id == principal.user_id, A.c.request_id == request_id)
                )
            )
            .mappings()
            .first()
        )
        if previous:
            if (
                previous["participant_id"] != participant_id
                or previous["file_sha256"] != staged.sha256
                or previous["original_filename"] != staged.filename
                or previous["content_type"] != staged.content_type
            ):
                raise AccessError("homework_request_conflict", 409)
            await db.rollback()
            await storage.delete(object_key)
            object_key = None
            return {
                "submission_id": str(previous["submission_id"]),
                "version": previous["version"],
                "is_current": previous["is_current"],
                "created": False,
            }
        version = (
            await db.scalar(
                select(func.coalesce(func.max(A.c.version), 0)).where(
                    A.c.participant_id == participant_id
                )
            )
        ) + 1
        await db.execute(
            update(A)
            .where(A.c.participant_id == participant_id, A.c.is_current)
            .values(is_current=False)
        )
        submission = Submission(
            id=sid,
            assignment_id=participant["assignment_id"],
            student_ref=f"hw:{participant_id.hex}:{attempt_id.hex[:16]}",
            status="submitted",
        )
        submission.file = SubmissionFile(
            id=fid,
            bucket=storage.bucket,
            object_key=object_key,
            original_filename=staged.filename,
            content_type=staged.content_type,
            size_bytes=staged.size_bytes,
            sha256=staged.sha256,
        )
        submission.parsing_task = ParsingTask(
            status="pending", max_attempts=settings.parsing_max_attempts
        )
        db.add(submission)
        await db.flush()
        await db.execute(
            insert(A).values(
                id=attempt_id,
                participant_id=participant_id,
                submission_id=sid,
                version=version,
                actor_id=principal.user_id,
                request_id=request_id,
                file_sha256=staged.sha256,
            )
        )
        committed = True
        await db.commit()
        return {"submission_id": str(sid), "version": version, "is_current": True, "created": True}
    except BaseException as error:
        await db.rollback()
        if object_key and not committed and not isinstance(error, asyncio.CancelledError):
            try:
                await storage.delete(object_key)
            except Exception:
                pass
        if committed and isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            raise AccessError("homework_commit_unknown", 503) from None
        raise
    finally:
        await asyncio.to_thread(staged.fileobj.close)


async def current_source(db, principal, participant_id, question_id, *, write=False, lock=False):
    """Resolve the currently selected attempt under real class authority."""
    participant, _ = await access.participant_access(
        db, principal, participant_id, write=write, lock=lock
    )
    attempt = (
        (await db.execute(select(A).where(A.c.participant_id == participant_id, A.c.is_current)))
        .mappings()
        .first()
    )
    if not attempt:
        raise AccessError("homework_submission_missing", 409)
    current = await db.scalar(
        select(A.c.id).where(A.c.participant_id == participant_id, A.c.is_current)
    )
    question = await db.scalar(
        select(Question.id).where(
            Question.id == question_id, Question.assignment_id == participant["assignment_id"]
        )
    )
    if current != attempt["id"] or not question:
        raise AccessError("homework_source_changed", 409)
    return attempt


async def prepare_submission(db, principal, submission_id, storage):
    """Normalize and align one current teacher-authorized homework submission."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from huipi_cloud.modules.answer_alignment import service as alignment_service
    from huipi_cloud.modules.answer_alignment.errors import (
        AnswerAlignmentArtifactCorruptError,
        AnswerAlignmentLimitError,
        AnswerAlignmentNotFoundError,
        AnswerAlignmentNotReadyError,
    )
    from huipi_cloud.modules.canonical_documents import service as canonical_service
    from huipi_cloud.modules.canonical_documents.errors import (
        CanonicalArtifactCorruptError,
        CanonicalDocumentNotFoundError,
        CanonicalNormalizationError,
        CanonicalSourceNotReadyError,
    )

    try:
        async with db.begin():
            attempt = await access.submission_access(
                db, principal, submission_id, write=True, lock=True
            )
            if not attempt:
                raise AccessError(access.DENIED, 404)
            if (
                await db.scalar(
                    select(ParsingTask.status).where(ParsingTask.submission_id == submission_id)
                )
                != "succeeded"
            ):
                raise AccessError("homework_parsing_not_ready", 409)
            source_id = attempt["id"]
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        await canonical_service.normalize_submission(factory, storage, submission_id)
        await alignment_service.align_submission(factory, storage, submission_id)
        async with db.begin():
            current = await access.submission_access(
                db, principal, submission_id, write=True, lock=True
            )
            if not current or current["id"] != source_id:
                raise AccessError("homework_source_changed", 409)
        return {"status": "ready_for_review"}
    except (
        AnswerAlignmentArtifactCorruptError,
        AnswerAlignmentLimitError,
        AnswerAlignmentNotFoundError,
        AnswerAlignmentNotReadyError,
        CanonicalArtifactCorruptError,
        CanonicalDocumentNotFoundError,
        CanonicalNormalizationError,
        CanonicalSourceNotReadyError,
    ):
        raise AccessError("homework_source_preparation_failed", 409) from None


def grade_input_error_code(status):
    return {
        "unsupported_modality": "homework_visual_scoring_not_supported",
        "review_required": "homework_answer_not_ready",
        "source_stale": "homework_source_changed",
        "source_unavailable": "homework_source_unavailable",
        "invalid_rubric": "homework_rubric_invalid",
        "size_limit_exceeded": "homework_input_too_large",
    }.get(status, "homework_grade_input_unavailable")


async def request_candidate(db, principal, participant_id, question_id, payload, storage):
    """Reuse GradeInput and GradeProposal; no provider fallback and no auto-review."""
    from uuid import NAMESPACE_URL, uuid5

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from huipi_cloud.modules.grading.errors import (
        GradeInputCommitOutcomeUnknownError,
        GradeInputError,
        GradeInputRegistrationRolledBackError,
    )
    from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
    from huipi_cloud.modules.grading.proposal_service import grade_input_artifact
    from huipi_cloud.modules.grading.service import prepare_grade_input

    try:
        async with db.begin():
            source = await current_source(db, principal, participant_id, question_id, write=True)
            source_id, submission_id = source["id"], source["submission_id"]
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        prepared = await prepare_grade_input(
            factory,
            storage,
            submission_id=submission_id,
            question_id=question_id,
            request_id=uuid5(NAMESPACE_URL, f"homework-input:{payload.request_id}"),
        )
        if prepared.artifact_id is None:
            raise AccessError(grade_input_error_code(prepared.inspection.status), 409)
        result = await grade_input_artifact(
            factory,
            storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=payload.request_id,
        )
        async with db.begin():
            current = await current_source(
                db, principal, participant_id, question_id, write=True, lock=True
            )
            if current["id"] != source_id:
                raise AccessError("homework_source_changed", 409)
            return result.model_dump(mode="json")
    except GradeProposalError as error:
        raise AccessError(
            error.reason_code,
            409
            if error.status
            not in {"provider_unavailable", "provider_timeout", "commit_outcome_unknown"}
            else 503,
        ) from None
    except (GradeInputCommitOutcomeUnknownError, GradeInputRegistrationRolledBackError):
        raise AccessError("homework_grade_input_commit_unavailable", 503) from None
    except GradeInputError:
        raise AccessError("homework_grade_input_unavailable", 409) from None


async def read_candidate(db, principal, participant_id, question_id, storage):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from huipi_cloud.modules.grading.grade_review_service import inspect_grade_proposal
    from huipi_cloud.modules.grading.models import GradeProposalArtifact
    from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
    from huipi_cloud.modules.grading.proposal_service import read_grade_proposal_artifact

    try:
        async with db.begin():
            source = await current_source(db, principal, participant_id, question_id)
            proposal_id = await db.scalar(
                select(GradeProposalArtifact.id)
                .where(
                    GradeProposalArtifact.submission_id == source["submission_id"],
                    GradeProposalArtifact.question_id == question_id,
                )
                .order_by(GradeProposalArtifact.created_at.desc(), GradeProposalArtifact.id.desc())
                .limit(1)
            )
            if not proposal_id:
                return {"candidate": None}
            factory = async_sessionmaker(db.bind, expire_on_commit=False)
            inspection = await inspect_grade_proposal(factory, storage, proposal_id=proposal_id)
            if inspection.source_status != "current":
                raise AccessError("homework_candidate_stale", 409)
            document, _ = await read_grade_proposal_artifact(factory, storage, proposal_id)
            current = await current_source(db, principal, participant_id, question_id, lock=True)
            if current["id"] != source["id"]:
                raise AccessError("homework_source_changed", 409)
            return {
                "candidate": document.model_dump(mode="json"),
                "review_revision": inspection.latest_review_revision,
                "review_status": inspection.latest_review_status,
            }
    except GradeProposalError:
        raise AccessError("homework_candidate_unavailable", 409) from None
