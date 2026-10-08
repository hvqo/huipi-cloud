"""Submission use cases and database transaction boundaries."""

import asyncio
import logging
from uuid import UUID, uuid4

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.assignments.errors import (
    AssignmentConflictError,
    AssignmentNotFoundError,
)
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.submissions import repository
from huipi_cloud.modules.submissions.errors import SubmissionNotFoundError
from huipi_cloud.modules.submissions.models import ParsingTask, Submission, SubmissionFile
from huipi_cloud.modules.submissions.uploads import StagedUpload, stage_upload

logger = logging.getLogger(__name__)


async def create_submission(
    session: AsyncSession,
    storage: S3ObjectStorage,
    *,
    assignment_id: UUID,
    student_ref: str,
    upload: UploadFile,
    max_size_bytes: int,
    max_parsing_attempts: int = 3,
) -> Submission:
    assignment_status = await repository.get_assignment_status(session, assignment_id)
    if assignment_status is None:
        raise AssignmentNotFoundError("作业不存在")
    if assignment_status != "published":
        raise AssignmentConflictError("只有已发布的作业可以提交")
    await session.rollback()

    staged: StagedUpload = await stage_upload(upload, max_size_bytes)
    submission_id = uuid4()
    file_id = uuid4()
    key_path = f"assignments/{assignment_id}/submissions/{submission_id}/{file_id}"
    prefix = storage.object_key_prefix
    object_key = f"{prefix}/{key_path}" if prefix else key_path
    upload_attempted = False
    commit_started = False
    try:
        upload_attempted = True
        await storage.upload_fileobj(staged.fileobj, object_key, staged.content_type)

        await session.begin()
        assignment = await session.scalar(
            select(Assignment)
            .where(Assignment.id == assignment_id)
            .with_for_update(read=True)
        )
        if assignment is None:
            raise AssignmentNotFoundError("作业不存在")
        if assignment.status != "published":
            raise AssignmentConflictError("只有已发布的作业可以提交")

        submission = Submission(
            id=submission_id,
            assignment_id=assignment_id,
            student_ref=student_ref,
            status="submitted",
        )
        submission.file = SubmissionFile(
            id=file_id,
            bucket=storage.bucket,
            object_key=object_key,
            original_filename=staged.filename,
            content_type=staged.content_type,
            size_bytes=staged.size_bytes,
            sha256=staged.sha256,
        )
        submission.parsing_task = ParsingTask(
            status="pending",
            max_attempts=max_parsing_attempts,
        )
        session.add(submission)
        await session.flush()

        # COMMIT can succeed on PostgreSQL while its acknowledgement is lost.
        # Once attempted, keep the object to avoid committed metadata pointing
        # at a deleted object; an uncertain rollback may instead leave an orphan.
        commit_started = True
        await session.commit()
        return submission
    except BaseException:
        try:
            await session.rollback()
        except Exception as rollback_error:
            logger.warning(
                "Submission database rollback failed key=%s error_type=%s",
                object_key,
                type(rollback_error).__name__,
            )

        if upload_attempted:
            if commit_started:
                logger.warning(
                    "Submission commit outcome uncertain; retaining object key=%s",
                    object_key,
                )
            else:
                try:
                    await storage.delete(object_key)
                    logger.info("Submission object compensation completed key=%s", object_key)
                except Exception as error:
                    logger.warning(
                        "Submission object compensation failed key=%s error_type=%s",
                        object_key,
                        type(error).__name__,
                    )
        raise
    finally:
        await asyncio.to_thread(staged.fileobj.close)


async def get_submission(session: AsyncSession, submission_id: UUID) -> Submission:
    submission = await repository.get_submission(session, submission_id)
    if submission is None:
        raise SubmissionNotFoundError("提交记录不存在")
    return submission


async def list_submissions(
    session: AsyncSession,
    assignment_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[Submission]:
    if await repository.get_assignment_status(session, assignment_id) is None:
        raise AssignmentNotFoundError("作业不存在")
    return await repository.list_assignment_submissions(
        session,
        assignment_id,
        limit=limit,
        offset=offset,
    )
