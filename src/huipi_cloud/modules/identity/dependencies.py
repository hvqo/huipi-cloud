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
POLICIES = {
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

POLICY_PATHS = {
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
            if name in {"grant_assignment_membership", "revoke_assignment_membership"}:
                if not current.is_admin:
                    raise AccessError("administrator_required")
                return current
            if name in {"create_assignment", "list_assignments"}:
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
