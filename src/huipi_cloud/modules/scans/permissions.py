"""Only existing, database-backed Event/Session/installation scopes are executable."""

from uuid import UUID

SCAN_ACTIONS = {
    code: {"installation", "exam_event", "exam_session"}
    for code in (
        "scan.batch.create",
        "scan.batch.read",
        "scan.file.upload",
        "scan.file.read",
    )
}
SCAN_OPERATOR_ROLE = UUID("11111111-b13a-4000-8000-000000000001")
SCAN_READER_ROLE = UUID("11111111-b13a-4000-8000-000000000002")
SCAN_DEPLOYER_ROLE = UUID("11111111-b13a-4000-8000-000000000003")
