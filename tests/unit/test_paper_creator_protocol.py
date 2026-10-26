"""Pure rejection contracts; real grant/receipt atomicity is proved on PostgreSQL."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.papers.permissions import PAPER_AUTHOR_ROLE, PAPER_CREATOR_ROLE
from huipi_cloud.modules.papers.schemas import SnapshotCreate, VersionWrite
from huipi_cloud.modules.resource_security.service import role_codes


@pytest.mark.parametrize(
    "role,kind",
    [
        (PAPER_CREATOR_ROLE, "paper_version"),
        (PAPER_CREATOR_ROLE, "exam_session"),
        (PAPER_AUTHOR_ROLE, "installation"),
        (PAPER_AUTHOR_ROLE, "exam_event"),
        (PAPER_AUTHOR_ROLE, "assignment"),
    ],
)
@pytest.mark.anyio
async def test_reserved_creation_roles_reject_wrong_scope_before_database_access(role, kind):
    with pytest.raises(AccessError) as error:
        await role_codes(None, role, kind)
    assert error.value.code == "security_role_scope_invalid"


@pytest.mark.parametrize("field", ["creator_user_id", "grant_id", "scope_id", "role_version_id"])
def test_client_cannot_choose_authority_target_or_role(field):
    with pytest.raises(ValidationError):
        SnapshotCreate(
            request_id=uuid4(), source_assignment_id=uuid4(), title="合成", **{field: uuid4()}
        )
    with pytest.raises(ValidationError):
        VersionWrite(
            request_id=uuid4(),
            paper_version_id=uuid4(),
            expected_revision=1,
            expected_content_sha256="a" * 64,
            expected_mapping_digest="b" * 64,
            **{field: uuid4()},
        )
