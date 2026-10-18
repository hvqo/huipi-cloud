"""Controlled organization CLI; there is no public HTTP write path in B-1.1b.

Every command requires a real Bearer token on stdin (never argv) and a request id. The
service re-reads the enabled User and installation-scoped Grant from the database, so a
revoked or deactivated operator is rejected immediately. Batch writes and audits share
one transaction; a lost COMMIT ack is reported as outcome-unknown and recovers on replay.
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.identity.service import resolve_principal
from huipi_cloud.modules.organization import service
from huipi_cloud.modules.organization.schemas import (
    AcademicTermCreate,
    AcademicYearCreate,
    AdministrativeClassCreate,
    GradeCohortCreate,
    StudentTransferRequest,
    SubjectCreate,
)

CREATE_COMMANDS = {
    "create-academic-year": (AcademicYearCreate, service.create_academic_year),
    "create-academic-term": (AcademicTermCreate, service.create_academic_term),
    "create-grade-cohort": (GradeCohortCreate, service.create_grade_cohort),
    "create-subject": (SubjectCreate, service.create_subject),
    "create-administrative-class": (
        AdministrativeClassCreate,
        service.create_administrative_class,
    ),
}


async def run(args):
    if session_factory is None:
        raise AccessError("organization_operation_unavailable", 503)
    if not args.token_stdin:
        raise AccessError("authentication_required", 401)
    token = sys.stdin.readline(16386).strip()
    identity = await TokenVerifier(settings).verify(token)
    async with session_factory() as session:
        principal = await resolve_principal(session, identity)
        await session.rollback()
        if args.command == "transfer-student":
            return await service.transfer_student(
                session,
                principal,
                StudentTransferRequest(
                    request_id=args.request_id,
                    student_id=args.student_id,
                    to_administrative_class_id=args.to_class_id,
                    effective_from=args.effective_from,
                ),
            )
        model, handler = CREATE_COMMANDS[args.command]
        with Path(args.payload).open("rb") as file:
            raw = file.read(8193)
        if len(raw) > 8192:
            raise AccessError("organization_payload_too_large", 413)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise AccessError("organization_payload_invalid", 422)
        data["request_id"] = str(args.request_id)
        return await handler(session, principal, model.model_validate(data))


def main():
    parser = argparse.ArgumentParser(description="受控组织维护CLI；不开放学生名册匿名或管理员捷径")
    sub = parser.add_subparsers(dest="command", required=True)
    names = [*CREATE_COMMANDS, "transfer-student"]
    for name in names:
        command = sub.add_parser(name)
        command.add_argument("--token-stdin", action="store_true")
        command.add_argument("--request-id", type=UUID, required=True)
        if name in CREATE_COMMANDS:
            command.add_argument("--payload", required=True)
        else:
            command.add_argument("--student-id", type=UUID, required=True)
            command.add_argument("--to-class-id", type=UUID, required=True)
            command.add_argument("--effective-from", type=date.fromisoformat, required=True)

    async def execute():
        try:
            print(json.dumps(await run(parser.parse_args()), ensure_ascii=False))
            return 0
        except AccessError as error:
            print(json.dumps({"status": "failed", "code": error.code}), file=sys.stderr)
            return 1
        except (SQLAlchemyError, OSError, TimeoutError, ValidationError, ValueError):
            print(
                '{"status":"failed","code":"organization_operation_unavailable"}',
                file=sys.stderr,
            )
            return 1
        finally:
            await dispose_database_engine()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
