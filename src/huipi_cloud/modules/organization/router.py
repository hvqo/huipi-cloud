"""Organization, student identity and two-phase roster import HTTP contracts."""

import asyncio
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Response, UploadFile
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

from . import schemas as s
from . import workbench
from .csv_import import MODE_COLUMNS


class OrganizationRoute(APIRoute):
    """Stable 503 on database/OS faults; never leak SQL or student payloads."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                # Only the fixed CSV preview route accepts a larger multipart body.
                # Content-Type casing or a spoofed multipart header cannot bypass the
                # dictionary/confirmation request limit.
                if request.method == "POST" and self.name != "preview_student_import":
                    chunks, size = [], 0
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > 8192:
                                raise AccessError("organization_payload_too_large", 413)
                            chunks.append(chunk)
                    request._body = b"".join(chunks)
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
    as_of: date | None = None,
    academic_year_id: UUID | None = None,
    cohort_id: UUID | None = None,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await service.list_students(
        session,
        class_id=class_id,
        query=query,
        active_only=active_only,
        as_of=as_of,
        academic_year_id=academic_year_id,
        cohort_id=cohort_id,
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


@router.post("/academic-years", response_model=s.MutationResult, status_code=201)
async def school_create_year(
    body: s.AcademicYearCreate, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_academic_year(session, principal, body)


@router.post("/academic-terms", response_model=s.MutationResult, status_code=201)
async def school_create_term(
    body: s.AcademicTermCreate, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_academic_term(session, principal, body)


@router.post("/grade-cohorts", response_model=s.MutationResult, status_code=201)
async def school_create_cohort(
    body: s.GradeCohortCreate, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_grade_cohort(session, principal, body)


@router.post("/subjects", response_model=s.MutationResult, status_code=201)
async def school_create_subject(
    body: s.SubjectCreate, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_subject(session, principal, body)


@router.post("/classes", response_model=s.MutationResult, status_code=201)
async def school_create_class(
    body: s.AdministrativeClassCreate, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_administrative_class(session, principal, body)


@router.get("/class-summaries", response_model=s.SchoolClassPage)
async def school_classes(
    session: DbSession,
    principal: AuthorizedPrincipal,
    academic_year_id: UUID | None = None,
    cohort_id: UUID | None = None,
    as_of: date | None = None,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await workbench.classes(
        session,
        principal,
        academic_year_id=academic_year_id,
        cohort_id=cohort_id,
        as_of=as_of,
        limit=limit,
        offset=offset,
    )


@router.get("/student-import-template")
async def school_import_template(
    session: DbSession, principal: AuthorizedPrincipal, mode: ImportMode
):
    # Same parser registry; header only, so a template cannot accidentally import sample identities.
    async with session.begin():
        await service.authorize(session, principal, "student.import.preview")
        return Response(
            ("\ufeff" + ",".join(MODE_COLUMNS[mode]) + "\r\n").encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="students-{mode}.csv"',
                "X-Content-Type-Options": "nosniff",
            },
        )


@router.get("/student-imports/{batch_id}/rows", response_model=s.StudentImportRowPage)
async def school_import_rows(
    batch_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
):
    return await workbench.import_rows(session, principal, batch_id, limit=limit, offset=offset)
