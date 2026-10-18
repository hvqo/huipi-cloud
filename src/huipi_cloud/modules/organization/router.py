"""Organization, student identity and two-phase roster import HTTP contracts."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization import import_service, service
from huipi_cloud.modules.organization.schemas import (
    AcademicTermPage,
    AcademicYearPage,
    AdministrativeClassPage,
    ClassEnrollmentPage,
    GradeCohortPage,
    ImportMode,
    StudentImportBatchPage,
    StudentImportConfirmRequest,
    StudentImportConfirmResult,
    StudentImportPreviewResult,
    StudentPage,
    SubjectPage,
)


class OrganizationRoute(APIRoute):
    """Stable 503 on database/OS faults; never leak SQL or student payloads."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                async with asyncio.timeout(30):
                    response = await handler(request)
                    response.headers["Cache-Control"] = "private, no-store"
                    return response
            except (SQLAlchemyError, OSError, TimeoutError):
                raise AccessError("organization_operation_unavailable", 503) from None

        return safe_handler


router = APIRouter(
    prefix="/organization",
    tags=["organization"],
    route_class=OrganizationRoute,
    responses={
        401: {"description": "缺少或无效身份"},
        403: {"description": "缺少固定组织动作权限"},
        404: {"description": "资源不存在或不在授权范围"},
        503: {"description": "组织或授权服务不可用"},
    },
)
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=1000000)]


@router.get("/academic-years", response_model=AcademicYearPage)
async def list_academic_years(
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await service.list_academic_years(session, limit=limit, offset=offset)


@router.get("/academic-terms", response_model=AcademicTermPage)
async def list_academic_terms(
    session: DbSession,
    principal: AuthorizedPrincipal,
    academic_year_id: UUID,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await service.list_academic_terms(
        session, academic_year_id=academic_year_id, limit=limit, offset=offset
    )


@router.get("/grade-cohorts", response_model=GradeCohortPage)
async def list_grade_cohorts(
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await service.list_grade_cohorts(session, limit=limit, offset=offset)


@router.get("/subjects", response_model=SubjectPage)
async def list_subjects(
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 100,
    offset: Offset = 0,
):
    return await service.list_subjects(session, limit=limit, offset=offset)


@router.get("/classes", response_model=AdministrativeClassPage)
async def list_administrative_classes(
    session: DbSession,
    principal: AuthorizedPrincipal,
    cohort_id: UUID | None = None,
    status: str | None = Query(default=None, pattern="^(active|inactive)$"),
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await service.list_administrative_classes(
        session,
        grade_cohort_id=cohort_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/students", response_model=StudentPage)
async def list_students(
    session: DbSession,
    principal: AuthorizedPrincipal,
    class_id: UUID | None = None,
    query: str | None = Query(default=None, max_length=64),
    active_only: bool = True,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await service.list_students(
        session,
        class_id=class_id,
        query=query,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )


@router.get("/students/{student_id}/enrollments", response_model=ClassEnrollmentPage)
async def list_student_enrollments(
    student_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 100,
    offset: Offset = 0,
):
    return await service.list_student_enrollments(
        session, student_id=student_id, limit=limit, offset=offset
    )


@router.post("/student-imports", response_model=StudentImportPreviewResult, status_code=201)
async def preview_student_import(
    session: DbSession,
    principal: AuthorizedPrincipal,
    request_id: Annotated[UUID, Form()],
    mode: Annotated[ImportMode, Form()],
    academic_year_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
):
    content = await file.read(import_service.MAX_FILE_BYTES + 1)
    return await import_service.preview_student_import(
        session,
        principal,
        request_id,
        mode=mode,
        academic_year_id=academic_year_id,
        filename=file.filename,
        content=content,
    )


@router.get("/student-imports", response_model=StudentImportBatchPage)
async def list_student_imports(
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await import_service.list_student_imports(session, limit=limit, offset=offset)


@router.get("/student-imports/{batch_id}", response_model=StudentImportPreviewResult)
async def get_student_import(
    batch_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
):
    return await import_service.get_student_import(session, batch_id=batch_id)


@router.post(
    "/student-imports/{batch_id}/confirm",
    response_model=StudentImportConfirmResult,
)
async def confirm_student_import(
    batch_id: UUID,
    payload: StudentImportConfirmRequest,
    session: DbSession,
    principal: AuthorizedPrincipal,
):
    return await import_service.confirm_student_import(
        session,
        principal,
        payload.request_id,
        batch_id=batch_id,
        expected_input_sha256=payload.expected_input_sha256,
    )
