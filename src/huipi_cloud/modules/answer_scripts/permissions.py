from uuid import UUID

SCRIPT_ACTIONS = {
    code: {"installation", "exam_event", "exam_session"}
    for code in (
        "exam.script.read",
        "exam.script.edit",
        "exam.script.confirm",
        "exam.identity.verify",
    )
}
SCRIPT_OPERATOR_ROLE = UUID("11111111-b14a-4000-8000-000000000001")
SCRIPT_DEPLOYER_ROLE = UUID("11111111-b14a-4000-8000-000000000002")

SCRIPT_READER_ROLE = UUID("11111111-b14a-4000-8000-000000000003")
SCRIPT_EDITOR_ROLE = UUID("11111111-b14a-4000-8000-000000000004")
SCRIPT_VERIFIER_ROLE = UUID("11111111-b14a-4000-8000-000000000005")
