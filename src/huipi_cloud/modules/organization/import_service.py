"""Two-phase trusted roster import: upload preview -> authorized confirmation.

Contract for B-1.1b (implementation owned by the import workstream):

``preview_student_import`` parses and validates the CSV without touching students or
enrollments, persists a frozen preview batch and returns row-level structured issues.
``confirm_student_import`` re-validates against current database state inside one audited
transaction and applies only if the whole batch is still valid; it never guesses identity
from names, never deletes students, never overwrites history and is request-idempotent.
"""

from uuid import UUID

from huipi_cloud.modules.identity.protocol import Principal

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_PREVIEW_ENTRIES = 500


async def preview_student_import(
    session,
    principal: Principal,
    request_id: UUID,
    *,
    mode: str,
    academic_year_id: UUID,
    filename: str | None,
    content: bytes,
) -> dict:
    raise NotImplementedError("student import preview is owned by the import workstream")


async def confirm_student_import(
    session,
    principal: Principal,
    request_id: UUID,
    *,
    batch_id: UUID,
    expected_input_sha256: str | None,
) -> dict:
    raise NotImplementedError("student import confirmation is owned by the import workstream")


async def get_student_import(session, *, batch_id: UUID) -> dict:
    raise NotImplementedError("student import status is owned by the import workstream")


async def list_student_imports(session, *, limit: int, offset: int) -> dict:
    raise NotImplementedError("student import history is owned by the import workstream")


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_PREVIEW_ENTRIES",
    "MAX_ROWS",
    "confirm_student_import",
    "get_student_import",
    "list_student_imports",
    "preview_student_import",
]
