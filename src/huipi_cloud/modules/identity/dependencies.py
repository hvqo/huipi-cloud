"""Every business HTTP route requires a fixed policy, a verified user and current grants."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier, VerifiedIdentity
from huipi_cloud.infrastructure.database import session as database
from huipi_cloud.modules.identity import service
from huipi_cloud.modules.identity.protocol import AccessError, Principal

bearer = HTTPBearer(auto_error=False)
verifier = TokenVerifier(settings)

# Exact route names + allowed methods: a new route has no implicit permission.
ADMIN_POLICIES = {
    "list_teachers": "/admin/teachers",
    "register_teacher": "/admin/teachers",
    "set_teacher_active": "/admin/teachers/{user_id}",
    "list_teacher_memberships": "/admin/teachers/{user_id}/memberships",
    "list_admin_audit_events": "/admin/audit-events",
}

# Organization routes use the fixed installation-scoped permission catalog; they never
# fall back to legacy User.is_admin or AssignmentMembership authority.
ORGANIZATION_POLICIES = {
    "list_academic_years": "organization.read",
    "list_academic_terms": "organization.read",
    "list_grade_cohorts": "organization.read",
    "list_subjects": "organization.read",
    "list_administrative_classes": "organization.read",
    "list_students": "organization.read",
    "list_student_enrollments": "organization.read",
    "list_student_imports": "organization.read",
    "get_student_import": "organization.read",
    "preview_student_import": "student.import.preview",
    "confirm_student_import": "student.import.confirm",
}

EXAM_MARK_POLICIES = {
    "initialize_exam_grading": (
        "POST",
        "/exam-parsing-jobs/{job_id}/grading-workspace",
        "exam.answer.review",
    ),
    "list_exam_grading_questions": (
        "GET",
        "/exam-parsing-jobs/{job_id}/grading-questions",
        "exam.mark.read",
    ),
    "inspect_exam_grading": ("GET", "/exam-grading-workspaces/{workspace_id}", "exam.mark.read"),
    "record_exam_answer_review": (
        "POST",
        "/exam-grading-workspaces/{workspace_id}/answer-review",
        "exam.answer.review",
    ),
    "request_exam_scoring": (
        "POST",
        "/exam-grading-workspaces/{workspace_id}/scoring",
        "exam.score.request",
    ),
    "record_exam_score_review": (
        "POST",
        "/exam-grading-workspaces/{workspace_id}/score-review",
        "exam.score.review",
    ),
}

EXAM_PARSE_POLICIES = {
    "start_exam_parsing": ("POST", "/answer-scripts/{script_id}/parsing", "exam.parse.start"),
    "current_exam_parsing": ("GET", "/answer-scripts/{script_id}/parsing", "exam.parse.read"),
    "inspect_exam_parsing": ("GET", "/exam-parsing-jobs/{job_id}", "exam.parse.read"),
    "read_exam_parsing_page": (
        "GET",
        "/exam-parsing-jobs/{job_id}/pages/{page_number}",
        "exam.parse.read",
    ),
}

SCRIPT_POLICIES = {
    "list_script_pages": (
        "GET",
        "/exam-sessions/{exam_session_id}/script-pages",
        "exam.script.read",
    ),
    "list_script_identities": (
        "GET",
        "/exam-sessions/{exam_session_id}/identity-candidates",
        "exam.script.read",
    ),
    "create_answer_script": (
        "POST",
        "/exam-sessions/{exam_session_id}/answer-scripts",
        "exam.script.edit",
    ),
    "list_answer_scripts": (
        "GET",
        "/exam-sessions/{exam_session_id}/answer-scripts",
        "exam.script.read",
    ),
    "inspect_answer_script": ("GET", "/answer-scripts/{script_id}", "exam.script.read"),
    "list_answer_script_versions": (
        "GET",
        "/answer-scripts/{script_id}/versions",
        "exam.script.read",
    ),
    "change_answer_script": ("POST", "/answer-scripts/{script_id}/versions", "exam.script.edit"),
    "get_answer_script_manifest": (
        "GET",
        "/answer-scripts/{script_id}/manifest",
        "exam.identity.verify",
    ),
}

SCAN_POLICIES = {
    "list_scan_sessions": ("GET", "/scan-sessions", None),
    "create_scan_batch": (
        "POST",
        "/exam-sessions/{exam_session_id}/scan-batches",
        "scan.batch.create",
    ),
    "list_scan_batches": (
        "GET",
        "/exam-sessions/{exam_session_id}/scan-batches",
        "scan.batch.read",
    ),
    "inspect_scan_batch": ("GET", "/scan-batches/{batch_id}", "scan.batch.read"),
    "list_scan_files": ("GET", "/scan-batches/{batch_id}/files", "scan.batch.read"),
    "upload_scan_file": ("POST", "/scan-batches/{batch_id}/files", "scan.file.upload"),
    "inspect_scan_file": ("GET", "/scan-files/{file_id}", "scan.batch.read"),
    "list_scan_pages": ("GET", "/scan-files/{file_id}/pages", "scan.file.read"),
    "download_scan_original": ("GET", "/scan-files/{file_id}/original", "scan.file.read"),
    "preview_scan_page": ("GET", "/scan-pages/{page_id}/preview", "scan.file.read"),
}

POLICIES = {
    **{name: {policy[0]} for name, policy in EXAM_MARK_POLICIES.items()},
    **{name: {policy[0]} for name, policy in EXAM_PARSE_POLICIES.items()},
    **{name: {policy[0]} for name, policy in SCRIPT_POLICIES.items()},
    **{name: {policy[0]} for name, policy in SCAN_POLICIES.items()},
    "get_current_user": {"GET"},
    "list_academic_years": {"GET"},
    "list_academic_terms": {"GET"},
    "list_grade_cohorts": {"GET"},
    "list_subjects": {"GET"},
    "list_administrative_classes": {"GET"},
    "list_students": {"GET"},
    "list_student_enrollments": {"GET"},
    "list_student_imports": {"GET"},
    "get_student_import": {"GET"},
    "preview_student_import": {"POST"},
    "confirm_student_import": {"POST"},
    "list_teachers": {"GET"},
    "register_teacher": {"POST"},
    "set_teacher_active": {"PATCH"},
    "list_teacher_memberships": {"GET"},
    "list_admin_audit_events": {"GET"},
    "get_assignment_workbench": {"GET"},
    "inspect_grade_review": {"GET"},
    "list_grade_reviews": {"GET"},
    "record_teacher_grade_review": {"POST"},
    "list_answer_reviews": {"GET"},
    "inspect_answer_review": {"GET"},
    "record_answer_review": {"POST"},
    "create_assignment": {"POST"},
    "list_assignments": {"GET"},
    "get_assignment": {"GET"},
    "add_question": {"POST"},
    "replace_answer_key": {"PUT"},
    "replace_rubric": {"PUT"},
    "publish_assignment": {"POST"},
    "create_submission": {"POST"},
    "list_submissions": {"GET"},
    "get_submission": {"GET"},
    "get_submission_file": {"GET"},
    "get_parsing_task_status": {"GET"},
    "get_parsed_document": {"GET"},
    "get_parsed_markdown": {"GET"},
    "get_canonical_document_status": {"GET"},
    "get_canonical_document_page": {"GET"},
    "get_answer_alignment_summary": {"GET"},
    "get_question_answer_alignment": {"GET"},
    "grant_assignment_membership": {"PUT"},
    "revoke_assignment_membership": {"DELETE"},
}

GRADE_REVIEW_PATH = (
    "/submissions/{submission_id}/questions/{question_id}/grade-proposals/{proposal_id}"
)

POLICY_PATHS = {
    **{name: policy[1] for name, policy in EXAM_MARK_POLICIES.items()},
    **{name: policy[1] for name, policy in EXAM_PARSE_POLICIES.items()},
    **{name: policy[1] for name, policy in SCRIPT_POLICIES.items()},
    **{name: policy[1] for name, policy in SCAN_POLICIES.items()},
    "get_current_user": "/me",
    "list_academic_years": "/organization/academic-years",
    "list_academic_terms": "/organization/academic-terms",
    "list_grade_cohorts": "/organization/grade-cohorts",
    "list_subjects": "/organization/subjects",
    "list_administrative_classes": "/organization/classes",
    "list_students": "/organization/students",
    "list_student_enrollments": "/organization/students/{student_id}/enrollments",
    "list_student_imports": "/organization/student-imports",
    "get_student_import": "/organization/student-imports/{batch_id}",
    "preview_student_import": "/organization/student-imports",
    "confirm_student_import": "/organization/student-imports/{batch_id}/confirm",
    **ADMIN_POLICIES,
    "get_assignment_workbench": "/assignments/{assignment_id}/workbench",
    "inspect_grade_review": GRADE_REVIEW_PATH + "/review",
    "list_grade_reviews": GRADE_REVIEW_PATH + "/reviews",
    "record_teacher_grade_review": GRADE_REVIEW_PATH + "/reviews",
    "list_answer_reviews": "/submissions/{submission_id}/answer-reviews",
    "inspect_answer_review": "/submissions/{submission_id}/answer-reviews/questions/{question_id}",
    "record_answer_review": "/submissions/{submission_id}/answer-reviews/questions/{question_id}",
    "create_assignment": "/assignments",
    "list_assignments": "/assignments",
    "get_assignment": "/assignments/{assignment_id}",
    "add_question": "/assignments/{assignment_id}/questions",
    "replace_answer_key": "/questions/{question_id}/answer-key",
    "replace_rubric": "/questions/{question_id}/rubric",
    "publish_assignment": "/assignments/{assignment_id}/publish",
    "create_submission": "/assignments/{assignment_id}/submissions",
    "list_submissions": "/assignments/{assignment_id}/submissions",
    "get_submission": "/submissions/{submission_id}",
    "get_submission_file": "/submissions/{submission_id}/file",
    "get_parsing_task_status": "/submissions/{submission_id}/parsing-task",
    "get_parsed_document": "/submissions/{submission_id}/parsed-document",
    "get_parsed_markdown": "/submissions/{submission_id}/parsed-document/markdown",
    "get_canonical_document_status": "/submissions/{submission_id}/canonical-document",
    "get_canonical_document_page": (
        "/submissions/{submission_id}/canonical-document/pages/{page_number}"
    ),
    "get_answer_alignment_summary": "/submissions/{submission_id}/answer-alignment",
    "get_question_answer_alignment": (
        "/submissions/{submission_id}/answer-alignment/questions/{question_id}"
    ),
    "grant_assignment_membership": "/assignments/{assignment_id}/members/{user_id}",
    "revoke_assignment_membership": "/assignments/{assignment_id}/members/{user_id}",
}


async def get_auth_session():
    # Separate from business sessions: authentication reads cannot autobegin a write UoW.
    factory = database.session_factory
    if factory is None:
        raise AccessError("authorization_unavailable", 503)
    async with factory() as session:
        yield session


AuthSession = Annotated[AsyncSession, Depends(get_auth_session)]


async def get_verified_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> VerifiedIdentity:
    if credentials is None:
        raise AccessError("authentication_required", 401)
    return await verifier.verify(credentials.credentials)


async def get_current_principal(
    identity: Annotated[VerifiedIdentity, Depends(get_verified_identity)],
    session: AuthSession,
) -> Principal:
    try:
        async with asyncio.timeout(5), session.begin():
            await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            return await service.resolve_principal(session, identity)
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("authorization_unavailable", 503) from None


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


async def authorize_request(
    request: Request,
    principal: CurrentPrincipal,
    session: AuthSession,
) -> Principal:
    name = request.scope["route"].name
    if request.method not in POLICIES.get(name, set()) or request.scope[
        "route"
    ].path != POLICY_PATHS.get(name, ""):
        raise AccessError("route_not_authorized")
    try:
        async with asyncio.timeout(5), session.begin():
            await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            current = await service.refresh_principal(session, principal)
            if name in {
                *ADMIN_POLICIES,
                "grant_assignment_membership",
                "revoke_assignment_membership",
            }:
                if not current.is_admin:
                    raise AccessError("administrator_required")
                return current
            if name in {"get_current_user", "create_assignment", "list_assignments"}:
                return current
            if name in {
                "inspect_grade_review",
                "list_grade_reviews",
                "record_teacher_grade_review",
            }:
                from huipi_cloud.modules.grading.grade_review_repository import authorize_proposal

                try:
                    ids = {
                        key: UUID(str(request.path_params[key]))
                        for key in ("submission_id", "question_id", "proposal_id")
                    }
                except ValueError:
                    raise AccessError("invalid_resource_id", 422) from None
                await authorize_proposal(
                    session, principal=current, write=request.method != "GET", **ids
                )
                return current
            if name in EXAM_MARK_POLICIES:
                from huipi_cloud.modules.exam_grading import service as exam_mark_service

                try:
                    await exam_mark_service.http_authorize(
                        session, current, request.path_params, EXAM_MARK_POLICIES[name][2]
                    )
                except ValueError:
                    raise AccessError("invalid_resource_id", 422) from None
                return current
            if name in EXAM_PARSE_POLICIES:
                from huipi_cloud.modules.exam_parsing import service as exam_parse_service

                try:
                    await exam_parse_service.http_authorize(
                        session, current, request.path_params, EXAM_PARSE_POLICIES[name][2]
                    )
                except ValueError:
                    raise AccessError("invalid_resource_id", 422) from None
                return current
            if name in SCRIPT_POLICIES:
                from huipi_cloud.modules.answer_scripts import service as script_service

                try:
                    await script_service.http_authorize(
                        session, current, request.path_params, SCRIPT_POLICIES[name][2]
                    )
                except ValueError:
                    raise AccessError("invalid_resource_id", 422) from None
                return current
            if name in SCAN_POLICIES:
                from huipi_cloud.modules.scans import service as scan_service

                action = SCAN_POLICIES[name][2]
                if action is not None:
                    try:
                        await scan_service.http_authorize(
                            session, current, request.path_params, action
                        )
                    except ValueError:
                        raise AccessError("invalid_resource_id", 422) from None
                # Session discovery is filtered by current scoped grants in the service SQL.
                return current
            if name in ORGANIZATION_POLICIES:
                from huipi_cloud.modules.organization import service as organization_service

                await organization_service.authorize(session, current, ORGANIZATION_POLICIES[name])
                return current
            scopes = []
            for kind in ("assignment_id", "question_id", "submission_id"):
                value = request.path_params.get(kind)
                if value is None:
                    continue
                try:
                    resource_id = UUID(str(value))
                except ValueError:
                    raise AccessError("invalid_resource_id", 422) from None
                scopes.append(
                    resource_id
                    if kind == "assignment_id"
                    else await service.resource_assignment(session, kind, resource_id)
                )
            # Even an admin cannot pair a Question with a different Submission's Assignment.
            if not scopes or len(set(scopes)) != 1:
                raise AccessError("resource_not_found_or_forbidden", 404)
            await service.require_assignment(
                session,
                current,
                scopes[0],
                write=request.method != "GET",
            )
            return current
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("authorization_unavailable", 503) from None


AuthorizedPrincipal = Annotated[Principal, Depends(authorize_request)]
