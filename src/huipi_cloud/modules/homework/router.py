"""Authenticated endpoints for teacher-authorized homework."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, UploadFile

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import service
from .schemas import HomeworkQuestionCreate, HomeworkScoreRequest, OfferingCreate, TeachingCreate

router = APIRouter(tags=["homework"], route_class=ScanRoute)


@router.get("/homework/teaching-assignments")
async def list_my_teaching_assignments(db: DbSession, principal: AuthorizedPrincipal):
    return await service.teaching_options(db, principal)


@router.post("/homework/admin/teaching-assignments")
async def create_teaching_assignment(
    body: TeachingCreate, db: DbSession, principal: AuthorizedPrincipal
):
    return await service.grant_teaching(db, principal, body)


@router.get("/homework/admin/teaching-assignments")
async def list_teaching_assignments(db: DbSession, principal: AuthorizedPrincipal):
    return await service.admin_teaching_options(db, principal)


@router.delete("/homework/admin/teaching-assignments/{teaching_id}")
async def revoke_teaching_assignment(
    teaching_id: UUID, db: DbSession, principal: AuthorizedPrincipal
):
    return await service.revoke_teaching(db, principal, teaching_id)


@router.post("/assignments/{assignment_id}/homework")
async def assign_homework(
    assignment_id: UUID, body: OfferingCreate, db: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_offering(db, principal, assignment_id, body)


@router.post("/assignments/{assignment_id}/homework-questions")
async def create_homework_question(
    assignment_id: UUID,
    body: HomeworkQuestionCreate,
    db: DbSession,
    principal: AuthorizedPrincipal,
):
    return await service.create_question(db, principal, assignment_id, body)


@router.get("/assignments/{assignment_id}/homework")
async def read_homework(
    assignment_id: UUID,
    db: DbSession,
    principal: AuthorizedPrincipal,
    class_id: UUID | None = None,
    offset: Annotated[int, Query(ge=0, le=2000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await service.read_offering(db, principal, assignment_id, class_id, offset, limit)


@router.get("/homework/participants/{participant_id}/attempts")
async def list_homework_attempts(
    participant_id: UUID, db: DbSession, principal: AuthorizedPrincipal
):
    return await service.attempts(db, principal, participant_id)


@router.get("/homework/submissions/{submission_id}")
async def read_homework_submission(
    submission_id: UUID,
    db: DbSession,
    principal: AuthorizedPrincipal,
):
    return await service.submission_info(db, principal, submission_id)


@router.post("/homework/submissions/{submission_id}/prepare")
async def prepare_homework_submission(
    submission_id: UUID,
    db: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.prepare_submission(db, principal, submission_id, storage)


@router.post("/homework/participants/{participant_id}/attempts")
async def upload_homework_attempt(
    participant_id: UUID,
    db: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
    request_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
):
    return await service.upload(db, principal, participant_id, request_id, file, storage)


@router.post("/homework/participants/{participant_id}/questions/{question_id}/candidate")
async def request_homework_candidate(
    participant_id: UUID,
    question_id: UUID,
    body: HomeworkScoreRequest,
    db: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.request_candidate(
        db, principal, participant_id, question_id, body, storage
    )


@router.get("/homework/participants/{participant_id}/questions/{question_id}/candidate")
async def read_homework_candidate(
    participant_id: UUID,
    question_id: UUID,
    db: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.read_candidate(db, principal, participant_id, question_id, storage)
