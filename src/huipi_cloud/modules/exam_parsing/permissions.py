from uuid import UUID

PARSE_ACTIONS = {
    code: {"installation", "exam_event", "exam_session"}
    for code in ("exam.parse.start", "exam.parse.read")
}
PARSE_OPERATOR_ROLE = UUID("11111111-b14b-4000-8000-000000000001")
PARSE_DEPLOYER_ROLE = UUID("11111111-b14b-4000-8000-000000000002")
PARSE_READER_ROLE = UUID("11111111-b14b-4000-8000-000000000003")
