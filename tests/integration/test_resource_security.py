"""B-1.1a: real PG/private S3, signed OIDC and actual legacy HTTP/service paths."""

import asyncio
import hashlib
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, insert, or_, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import aliased
from test_grade_review import _make_candidate, _review_payload
from test_teacher_authorization import auth_api as auth_api
from test_teacher_authorization import create

from huipi_cloud.infrastructure.database.base import Base
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.assignments.schemas import AssignmentCreate
from huipi_cloud.modules.copilot.protocol import CopilotError
from huipi_cloud.modules.copilot.tools import BusinessTools, validate_arguments
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.grade_review_service import record_grade_review
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import ImportRequest
from huipi_cloud.modules.knowledge.service import import_material, material_history
from huipi_cloud.modules.resource_security import service
from huipi_cloud.modules.resource_security.models import (
    PermissionDefinition,
    SecurityAudit,
    SecurityGrant,
    SecurityScope,
)
from huipi_cloud.modules.submissions.models import Submission


def principal(ctx, name="admin"):
    u = ctx.users[name]
    return Principal(
        user_id=u.id,
        issuer=u.issuer,
        subject=u.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        is_admin=u.is_admin,
    )


@pytest.fixture
async def sec(auth_api):
    ctx = auth_api
    # Deployment bootstrap belongs to an independent test DB, not public HTTP.
    async with ctx.factory() as s, s.begin():
        await s.execute(text("TRUNCATE security_audit_events"))
    async with ctx.factory() as s:
        await service.deployment_bootstrap(s, ctx.users["admin"].id, uuid4(), "synthetic-approval")
    try:
        yield ctx
    finally:
        async with ctx.factory() as s, s.begin():
            ids = [u.id for u in ctx.users.values()]
            await s.execute(delete(SecurityGrant).where(SecurityGrant.user_id.in_(ids)))
            # Test-operator cleanup may remove its own protected memberships via Core.
            await s.execute(
                delete(AssignmentMembership.__table__).where(
                    AssignmentMembership.__table__.c.user_id.in_(ids)
                )
            )


async def protected(ctx, classification="exam_protected"):
    # Trusted DB seed is allowed to construct protected/unknown fixtures. Not an exam feature.
    aid = uuid4()
    async with ctx.factory() as s, s.begin():
        await s.execute(
            insert(Assignment.__table__).values(
                id=aid,
                title="SYNTHETIC-PRIVATE",
                subject="数学",
                grade_level="合成班",
                security_classification=classification,
                status="draft",
            )
        )
        s.add(AssignmentMembership(assignment_id=aid, user_id=ctx.users["a"].id, can_write=True))
    return aid


@pytest.mark.anyio
async def test_legacy_rules_lists_aliases_and_unknown_class_fail_closed(sec):
    ctx = sec
    legacy = UUID(await create(ctx))
    pid = await protected(ctx)
    unknown = await protected(ctx, "unclassified")
    for who in ("a", "admin"):
        rows = (
            await ctx.client.get(
                "/api/v1/assignments", headers=ctx.headers(who), params={"limit": 1, "offset": 0}
            )
        ).json()
        assert [r["id"] for r in rows] == [str(legacy)]
        for resource in (pid, unknown, uuid4()):
            r = await ctx.client.get(f"/api/v1/assignments/{resource}", headers=ctx.headers(who))
            assert r.status_code == 404 and r.json() == {
                "detail": "resource_not_found_or_forbidden"
            }
            assert b"SYNTHETIC-PRIVATE" not in r.content
    async with ctx.factory() as s:
        a = aliased(Assignment)
        assert list((await s.scalars(select(a.id))).all()) == [legacy]
        assert await s.scalar(select(func.count()).select_from(Assignment)) == 1
        subquery = select(Assignment.id).subquery()
        assert list(await s.scalars(select(subquery.c.id))) == [legacy]
        cte = select(Assignment.id).cte()
        assert list(await s.scalars(select(cte.c.id))) == [legacy]
        assert list(await s.scalars(select(Assignment.id).union(select(Assignment.id)))) == [legacy]
        assert await s.get(Assignment, pid) is None
    # A client-supplied classification/role is ignored, never an authority input.
    r = await ctx.client.post(
        "/api/v1/assignments",
        headers=ctx.headers("a", role="admin"),
        json={
            "title": "合成普通作业",
            "subject": "数学",
            "grade_level": "合成",
            "security_classification": "exam_protected",
            "scope": str(pid),
            "is_admin": True,
        },
    )
    assert r.status_code == 201
    async with ctx.factory() as s:
        row = await s.get(Assignment, UUID(r.json()["id"]))
        assert row.security_classification == "legacy_homework"


async def copy_protected_graph(factory, aid):
    """Test-only Core copy: all FK rows keep real parents; no production reclassification."""
    mappings = {}
    all_ids = {}
    excluded = {"users", "assignment_memberships", "admin_operations"}
    async with factory() as s, s.begin():
        for table in Base.metadata.sorted_tables:
            if table.name in excluded or table.name.startswith(("security_", "permission_")):
                continue
            conditions = []
            if table.name == "assignments":
                conditions = [table.c.id == aid]
            else:
                for col in table.c:
                    for fk in col.foreign_keys:
                        old = mappings.get(fk.column.table.name, {})
                        if old:
                            conditions.append(col.in_(old))
            if not conditions:
                continue
            rows = (await s.execute(select(table).where(or_(*conditions)))).mappings().all()
            mapping = mappings.setdefault(table.name, {})
            for row in rows:
                values = dict(row)
                pk = list(table.primary_key)[0].name
                new = uuid4()
                mapping[values[pk]] = new
                all_ids[values[pk]] = new
                values[pk] = new
                if table.name == "knowledge_documents":
                    all_ids.setdefault(values["document_id"], uuid4())
                for key, value in list(values.items()):
                    if isinstance(value, UUID):
                        values[key] = all_ids.get(value, value)
                    elif isinstance(value, str) and (key.endswith("_key") or key == "object_key"):
                        values[key] = value + "/security-test/" + new.hex
                if "request_id" in values:
                    values["request_id"] = uuid4()
                if table.name == "assignments":
                    values["security_classification"] = "exam_protected"
                await s.execute(insert(table).values(**values))
    return mappings


@pytest.mark.anyio
async def test_all_legacy_sensitive_http_and_service_graphs_deny_before_s3(
    sec, postgres_engine, monkeypatch
):
    ctx = sec
    ctx.client.headers.update(ctx.headers("a"))
    result = await _make_candidate(ctx.client, ctx.storage, postgres_engine)
    factory, sid, q, _, _, _, proposal, *_ = result
    await record_grade_review(
        factory,
        ctx.storage,
        proposal_id=proposal.proposal_id,
        request_id=uuid4(),
        expected_revision=0,
        payload=_review_payload(),
    )
    raw = "合成教学参考资料，不能跨作业读取。".encode()
    request = ImportRequest(
        assignment_id=q.assignment_id,
        title="合成资料",
        source_type="text",
        permission="synthetic",
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )
    receipt = await import_material(factory, ctx.storage, request, raw)
    original = await ctx.client.get(f"/api/v1/submissions/{sid}/file")
    assert original.status_code == 200 and original.content.startswith(b"%PDF")
    mappings = await copy_protected_graph(factory, q.assignment_id)
    aid = mappings["assignments"][q.assignment_id]
    new_sid = mappings["submissions"][sid]
    qid = mappings["questions"][q.id]
    pid = mappings["grade_proposal_artifacts"][proposal.proposal_id]
    async with factory() as s:
        file_table = Base.metadata.tables["submission_files"]
        file_row = (
            (await s.execute(select(file_table).where(file_table.c.submission_id == new_sid)))
            .mappings()
            .one()
        )
    await ctx.storage.upload_fileobj(
        io.BytesIO(original.content), file_row["object_key"], file_row["content_type"]
    )
    # A legacy membership on a protected parent is deliberately ineffective.
    async with factory() as s, s.begin():
        s.add(AssignmentMembership(assignment_id=aid, user_id=ctx.users["a"].id, can_write=True))
    paths = [
        f"/assignments/{aid}",
        f"/assignments/{aid}/workbench",
        f"/assignments/{aid}/submissions",
        f"/submissions/{new_sid}",
        *[
            f"/submissions/{new_sid}/{x}"
            for x in [
                "file",
                "parsing-task",
                "parsed-document",
                "parsed-document/markdown",
                "canonical-document",
                "canonical-document/pages/1",
                "answer-alignment",
                "answer-reviews",
            ]
        ],
        f"/submissions/{new_sid}/answer-alignment/questions/{qid}",
        f"/submissions/{new_sid}/answer-reviews/questions/{qid}",
        f"/submissions/{new_sid}/questions/{qid}/grade-proposals/{pid}/review",
        f"/submissions/{new_sid}/questions/{qid}/grade-proposals/{pid}/reviews",
    ]

    async def forbidden_storage(*a, **kw):
        raise AssertionError("protected storage must not be touched")

    monkeypatch.setattr(ctx.storage, "download", forbidden_storage)
    for who in ("a", "admin"):
        for path in paths:
            response = await ctx.client.get("/api/v1" + path, headers=ctx.headers(who))
            assert response.status_code == 404, (path, response.text)
            assert response.json() == {"detail": "resource_not_found_or_forbidden"}
            assert b"bucket" not in response.content and b"object_key" not in response.content
        # Legacy lists do not even reveal the protected IDs.
        listing = await ctx.client.get("/api/v1/assignments", headers=ctx.headers(who))
        assert str(aid) not in listing.text
    tools = BusinessTools(factory, aid)
    with pytest.raises(CopilotError):
        await tools.execute(
            "get_assignment_summary", validate_arguments("get_assignment_summary", {})
        )
    retriever = LexicalRetriever(factory, ctx.storage)
    assert not (await retriever.search(aid, "合成教学", 5)).hits
    with pytest.raises(Exception) as error:
        await material_history(factory, aid, receipt.document_id)
    assert getattr(error.value, "code", "") == "knowledge_not_found_in_scope"
    # Direct old repositories/history also cannot load the protected graph.
    from huipi_cloud.modules.grading.grade_review_repository import list_reviews

    assert await list_reviews(factory, pid) == []
    from huipi_cloud.modules.answer_review.repository import decision_region_page

    async with factory() as s:
        decision_id = next(iter(mappings["answer_review_decisions"].values()))
        assert await decision_region_page(s, decision_id) == []
    for method, path, payload in [
        (
            "post",
            f"/assignments/{aid}/questions",
            {
                "question_number": 2,
                "question_type": "short_answer",
                "stem": "不能写入",
                "max_score": "2",
            },
        ),
        ("put", f"/questions/{qid}/answer-key", {"answer_content": "不能写入"}),
        (
            "put",
            f"/questions/{qid}/rubric",
            {"criteria": [{"description": "不能写入", "points": "2", "sort_order": 1}]},
        ),
        ("post", f"/assignments/{aid}/publish", None),
        ("put", f"/assignments/{aid}/members/{ctx.users['b'].id}", {"can_write": True}),
    ]:
        for who in ("a", "admin"):
            r = await ctx.client.request(
                method, "/api/v1" + path, headers=ctx.headers(who), json=payload
            )
            assert r.status_code in {403, 404}, (path, r.text)
            assert "不能写入" not in r.text
    r = await ctx.client.post(
        f"/api/v1/assignments/{aid}/submissions",
        headers=ctx.headers("admin"),
        data={"student_ref": "synthetic"},
        files={"file": ("test.pdf", original.content, "application/pdf")},
    )
    assert r.status_code == 404
    async with factory() as s:
        # The old database-operator CLI also cannot insert legacy memberships.
        import argparse

        from huipi_cloud.modules.identity import __main__ as old_cli

        monkeypatch.setattr(old_cli, "session_factory", factory)
        with pytest.raises(AccessError):
            await old_cli.run(
                argparse.Namespace(
                    command="grant", assignment_id=aid, user_id=ctx.users["b"].id, write=True
                )
            )
        assert (
            await s.scalar(
                select(func.count())
                .select_from(AssignmentMembership.__table__)
                .where(
                    AssignmentMembership.__table__.c.assignment_id == aid,
                    AssignmentMembership.__table__.c.user_id == ctx.users["b"].id,
                )
            )
            == 0
        )
    async with factory() as s:
        assert await s.scalar(select(func.count()).select_from(Submission)) == 1
        assert await s.scalar(select(func.count()).select_from(GradeReviewRevision)) == 1
        rawrows = (await s.execute(select(GradeReviewRevision.__table__))).mappings().all()
        assert len(rawrows) == 2  # protected history exists; it was not deleted to hide it.


@pytest.mark.anyio
async def test_atomic_protected_factory_idempotency_no_membership_and_class_immutable(sec):
    ctx = sec
    p = principal(ctx)
    rid = uuid4()
    payload = AssignmentCreate(title="合成安全载体", subject="数学", grade_level="合成")
    async with ctx.factory() as s:
        first = await service.create_protected_assignment(s, p, rid, payload)
    async with ctx.factory() as s:
        assert await service.create_protected_assignment(s, p, rid, payload) == first
    async with ctx.factory() as s:
        with pytest.raises(AccessError) as err:
            await service.create_protected_assignment(
                s, p, rid, payload.model_copy(update={"title": "改动"})
            )
        assert err.value.code == "security_request_conflict"
    aid = UUID(first["assignment_id"])
    async with ctx.factory() as s:
        assert (
            await s.execute(
                select(Assignment.__table__.c.security_classification).where(
                    Assignment.__table__.c.id == aid
                )
            )
        ).scalar_one() == "exam_protected"
        assert await s.scalar(select(func.count()).select_from(AssignmentMembership)) == 0
        assert (
            await s.scalar(
                select(func.count())
                .select_from(SecurityAudit)
                .where(SecurityAudit.request_id == rid)
            )
            == 1
        )
    for value in ("legacy_homework", "unclassified"):
        async with ctx.factory() as s:
            with pytest.raises(DBAPIError):
                await s.execute(
                    update(Assignment.__table__)
                    .where(Assignment.__table__.c.id == aid)
                    .values(security_classification=value)
                )
            await s.rollback()
    async with ctx.factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(delete(Assignment.__table__).where(Assignment.__table__.c.id == aid))
        await s.rollback()


@pytest.mark.anyio
async def test_legacy_admin_and_forged_principal_do_not_obtain_security_permissions(auth_api):
    ctx = auth_api
    for who in ("admin", "a"):
        async with ctx.factory() as s:
            with pytest.raises(AccessError) as error:
                await service.create_protected_assignment(
                    s,
                    principal(ctx, who).model_copy(update={"is_admin": True}),
                    uuid4(),
                    AssignmentCreate(title="不允许的保护资源", subject="数学", grade_level="合成"),
                )
            assert error.value.code == "resource_not_found_or_forbidden"
        async with ctx.factory() as s:
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(SecurityAudit)
                    .where(SecurityAudit.actor_user_id == ctx.users[who].id)
                )
                == 0
            )


@pytest.mark.anyio
async def test_grant_revoke_disable_and_scope_identity_checks(sec):
    ctx = sec
    operator = principal(ctx)
    teacher = principal(ctx, "a")
    request_id = uuid4()
    async with ctx.factory() as s:
        receipt = await service.grant_role(
            s,
            operator,
            request_id,
            user_id=teacher.user_id,
            role_version_id=service.CREATOR_ROLE,
            scope_id=service.INSTALLATION_SCOPE,
        )
    async with ctx.factory() as s:
        assert (
            await service.grant_role(
                s,
                operator,
                request_id,
                user_id=teacher.user_id,
                role_version_id=service.CREATOR_ROLE,
                scope_id=service.INSTALLATION_SCOPE,
            )
            == receipt
        )
    async with ctx.factory() as s:
        await service.create_protected_assignment(
            s,
            teacher,
            uuid4(),
            AssignmentCreate(title="明确授权后创建", subject="数学", grade_level="合成"),
        )
    async with ctx.factory() as s:
        with pytest.raises(AccessError) as err:
            await service.grant_role(
                s,
                teacher,
                uuid4(),
                user_id=teacher.user_id,
                role_version_id=service.OPERATOR_ROLE,
                scope_id=service.INSTALLATION_SCOPE,
            )
        assert err.value.code == "resource_not_found_or_forbidden"
    aid = await protected(ctx)
    async with ctx.factory() as s, s.begin():
        scoped = SecurityScope(kind="assignment", assignment_id=aid)
        s.add(scoped)
        await s.flush()
        scoped_id = scoped.id
    async with ctx.factory() as s:
        await service.grant_role(
            s,
            operator,
            uuid4(),
            user_id=teacher.user_id,
            role_version_id=service.AUDITOR_ROLE,
            scope_id=scoped_id,
        )
    async with ctx.factory() as s:
        result = await service.inspect_classification(s, teacher, aid, scoped_id)
        assert result == {"assignment_id": str(aid), "classification": "exam_protected"}
    async with ctx.factory() as s:
        with pytest.raises(AccessError) as err:
            await service.inspect_classification(s, teacher, uuid4(), scoped_id)
        assert err.value.status_code == 404
    async with ctx.factory() as s:
        await service.revoke_grant(s, operator, uuid4(), grant_id=UUID(receipt["grant_id"]))
    async with ctx.factory() as s:
        with pytest.raises(AccessError):
            await service.create_protected_assignment(
                s,
                teacher,
                uuid4(),
                AssignmentCreate(title="撤权后禁止", subject="数学", grade_level="合成"),
            )
    # Existing grants do not survive a disabled User or mismatched trusted identity.
    async with ctx.factory() as s, s.begin():
        await s.execute(update(User).where(User.id == teacher.user_id).values(is_active=False))
    for forged in (teacher, operator.model_copy(update={"user_id": teacher.user_id})):
        async with ctx.factory() as s:
            with pytest.raises(AccessError):
                await service.inspect_classification(s, forged, aid, scoped_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "action", ["exam.participation.read", "unknown.permission", "SELECT * FROM users"]
)
async def test_future_unknown_permissions_and_scopes_are_not_executable(sec, action):
    ctx = sec
    async with ctx.factory() as s, s.begin():
        with pytest.raises(AccessError):
            await service.require_permission(s, principal(ctx), action, service.INSTALLATION_SCOPE)
    async with ctx.factory() as s, s.begin():
        with pytest.raises(AccessError):
            await service.require_permission(
                s, principal(ctx), "security.protected.create", uuid4()
            )
    async with ctx.factory() as s:
        with pytest.raises(DBAPIError):
            s.add(SecurityScope(kind="exam_session", installation_id=1))
            await s.flush()
        await s.rollback()


@pytest.mark.anyio
async def test_single_grant_must_supply_delegation_and_permissions_without_cross_grant_union(sec):
    from huipi_cloud.modules.resource_security.models import (
        SecurityRolePermission,
        SecurityRoleVersion,
    )

    ctx = sec
    role_id = uuid4()
    async with ctx.factory() as s, s.begin():
        s.add(
            SecurityRoleVersion(
                id=role_id, role_code="synthetic_delegate_" + role_id.hex[:12], revision=1
            )
        )
        await s.flush()
        s.add(
            SecurityRolePermission(
                role_version_id=role_id, permission_code="security.grants.manage"
            )
        )
        await s.flush()
        await s.execute(
            update(SecurityRoleVersion)
            .where(SecurityRoleVersion.id == role_id)
            .values(sealed_at=datetime.now(UTC))
        )
        s.add_all(
            [
                SecurityGrant(
                    user_id=ctx.users["a"].id,
                    role_version_id=rid,
                    scope_id=service.INSTALLATION_SCOPE,
                )
                for rid in (role_id, service.CREATOR_ROLE)
            ]
        )
    async with ctx.factory() as s:
        with pytest.raises(AccessError):
            await service.grant_role(
                s,
                principal(ctx, "a"),
                uuid4(),
                user_id=ctx.users["b"].id,
                role_version_id=service.CREATOR_ROLE,
                scope_id=service.INSTALLATION_SCOPE,
            )
    async with ctx.factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(SecurityGrant)
                .where(SecurityGrant.user_id == ctx.users["b"].id)
            )
            == 0
        )


@pytest.mark.anyio
async def test_role_seal_cannot_be_mutated_or_receive_new_permissions(sec):
    from huipi_cloud.modules.resource_security.models import (
        SecurityRolePermission,
        SecurityRoleVersion,
    )

    ctx = sec
    statements = [
        update(SecurityRoleVersion)
        .where(SecurityRoleVersion.id == service.CREATOR_ROLE)
        .values(role_code="forged"),
        insert(SecurityRolePermission).values(
            role_version_id=service.CREATOR_ROLE, permission_code="security.grants.manage"
        ),
    ]
    for statement in statements:
        async with ctx.factory() as s:
            with pytest.raises(DBAPIError):
                await s.execute(statement)
            await s.rollback()
    # Even a persisted enabled row cannot activate a future action absent from code.
    async with ctx.factory() as s, s.begin():
        await s.execute(
            insert(PermissionDefinition).values(
                code="exam.participation.read", enabled=True, scope_kinds=["installation"]
            )
        )
    try:
        async with ctx.factory() as s:
            with pytest.raises(AccessError):
                await service.require_permission(
                    s, principal(ctx), "exam.participation.read", service.INSTALLATION_SCOPE
                )
    finally:
        async with ctx.factory() as s, s.begin():
            await s.execute(
                delete(PermissionDefinition).where(
                    PermissionDefinition.code == "exam.participation.read"
                )
            )


@pytest.mark.anyio
async def test_concurrent_same_request_creates_one_protected_assignment(sec):
    ctx = sec
    rid = uuid4()
    barrier = asyncio.Barrier(2)

    async def invoke():
        async with ctx.factory() as s:
            await barrier.wait()
            return await service.create_protected_assignment(
                s,
                principal(ctx),
                rid,
                AssignmentCreate(title="并发安全合成", subject="数学", grade_level="合成"),
            )

    results = await asyncio.gather(invoke(), invoke())
    assert results[0] == results[1]
    async with ctx.factory() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(SecurityAudit)
                .where(SecurityAudit.request_id == rid)
            )
            == 1
        )
        table = Assignment.__table__
        assert (
            await s.scalar(
                select(func.count())
                .select_from(table)
                .where(table.c.id == UUID(results[0]["assignment_id"]))
            )
            == 1
        )


@pytest.mark.anyio
async def test_grant_revocation_serializes_after_inflight_authority_and_rejects_next(sec):
    ctx = sec
    async with ctx.factory() as s:
        receipt = await service.grant_role(
            s,
            principal(ctx),
            uuid4(),
            user_id=ctx.users["a"].id,
            role_version_id=service.CREATOR_ROLE,
            scope_id=service.INSTALLATION_SCOPE,
        )
    held = asyncio.Event()
    release = asyncio.Event()
    revoking = asyncio.Event()

    async def execution():
        async with ctx.factory() as s, s.begin():
            await service.require_permission(
                s, principal(ctx, "a"), "security.protected.create", service.INSTALLATION_SCOPE
            )
            held.set()
            await release.wait()

    async def revocation():
        await held.wait()
        revoking.set()
        async with ctx.factory() as s:
            return await service.revoke_grant(
                s, principal(ctx), uuid4(), grant_id=UUID(receipt["grant_id"])
            )

    task = asyncio.create_task(execution())
    rev = asyncio.create_task(revocation())
    try:
        await asyncio.wait_for(revoking.wait(), 5)
        # Synchronize on a real database lock wait, not timing/sleep luck.
        async with ctx.factory() as s:
            async with asyncio.timeout(4):
                while True:
                    await s.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await s.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                            "WHERE datname=current_database() AND wait_event_type='Lock' "
                            "AND query LIKE '%security_grants%')"
                        )
                    )
                    if waiting:
                        break
                    await asyncio.sleep(
                        0
                    )  # yields; the assertion waits on observed PostgreSQL state.
            assert waiting and not rev.done()
    finally:
        release.set()
        await asyncio.gather(task, rev)
    async with ctx.factory() as s, s.begin():
        with pytest.raises(AccessError):
            await service.require_permission(
                s, principal(ctx, "a"), "security.protected.create", service.INSTALLATION_SCOPE
            )


@pytest.mark.anyio
async def test_database_audit_failure_rolls_back_and_unknown_commit_replays(
    sec, postgres_engine, monkeypatch
):
    ctx = sec
    payload = AssignmentCreate(title="合成事务", subject="数学", grade_level="合成")

    def fail_audit(conn, cursor, statement, params, context, executemany):
        if statement.startswith("INSERT INTO security_audit_events"):
            raise OperationalError("audit insert", {}, Exception("synthetic unavailable"))

    rid = uuid4()
    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_audit)
    try:
        async with ctx.factory() as s:
            with pytest.raises(AccessError) as err:
                await service.create_protected_assignment(s, principal(ctx), rid, payload)
            assert err.value.code == "authorization_unavailable"
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_audit)
    async with ctx.factory() as s:
        assert (
            await s.scalar(select(SecurityAudit.id).where(SecurityAudit.request_id == rid)) is None
        )
        table = Assignment.__table__
        assert await s.scalar(select(func.count()).select_from(table)) == 0
    commit = ctx.factory.class_.commit

    async def lost_ack(s):
        await commit(s)
        if s.info.pop("lost_ack", False):
            raise OSError("synthetic commit ack lost")

    monkeypatch.setattr(ctx.factory.class_, "commit", lost_ack)
    async with ctx.factory() as s:
        s.info["lost_ack"] = True
        with pytest.raises(AccessError) as err:
            await service.create_protected_assignment(s, principal(ctx), rid, payload)
        assert err.value.code == "security_commit_outcome_unknown"
    async with ctx.factory() as s:
        recovered = await service.create_protected_assignment(s, principal(ctx), rid, payload)
    async with ctx.factory() as s:
        assert (
            await s.scalar(
                select(Assignment.__table__.c.security_classification).where(
                    Assignment.__table__.c.id == UUID(recovered["assignment_id"])
                )
            )
        ) == "exam_protected"
        assert (
            await s.scalar(
                select(func.count())
                .select_from(SecurityAudit)
                .where(SecurityAudit.request_id == rid)
            )
            == 1
        )


@pytest.mark.anyio
async def test_security_audit_append_only_and_authorization_database_failure(sec, postgres_engine):
    ctx = sec
    async with ctx.factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(delete(SecurityAudit))
        await s.rollback()

    def fail_catalog(conn, cursor, statement, params, context, executemany):
        if "security_grants" in statement:
            raise OperationalError("authorization", {}, Exception("synthetic outage"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_catalog)
    try:
        async with ctx.factory() as s:
            with pytest.raises(AccessError) as err:
                await service.create_protected_assignment(
                    s,
                    principal(ctx),
                    uuid4(),
                    AssignmentCreate(title="不能保存", subject="数学", grade_level="合成"),
                )
            assert err.value.status_code == 503
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_catalog)


@pytest.mark.anyio
async def test_independent_migration_roundtrip_preserves_legacy_graph_and_refuses_unsafe_downgrade(
    auth_api,
    postgres_engine,
):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    ctx = auth_api
    ctx.client.headers.update(ctx.headers("a"))
    factory, sid, q, _, _, _, proposal, *_ = await _make_candidate(
        ctx.client, ctx.storage, postgres_engine
    )
    await record_grade_review(
        factory,
        ctx.storage,
        proposal_id=proposal.proposal_id,
        request_id=uuid4(),
        expected_revision=0,
        payload=_review_payload(),
    )
    # Tables added after the baseline revision do not exist at c72d9e14a510 and are not
    # part of the legacy graph this round-trip preserves.
    post_baseline_tables = {
        "scan_batches",
        "scan_files",
        "scan_pages",
        "scan_operations",
        "academic_years",
        "academic_terms",
        "grade_cohorts",
        "subjects",
        "administrative_classes",
        "students",
        "class_enrollments",
        "student_import_batches",
        "student_import_rows",
        "organization_operations",
        "paper_families",
        "paper_versions",
        "paper_question_mappings",
        "paper_operations",
        "exam_events",
        "exam_sessions",
        "session_papers",
        "exam_operations",
        "exam_participations",
        "roster_revisions",
        "roster_entry_snapshots",
        "roster_operations",
    }
    source = {}
    async with factory() as s:
        for table in Base.metadata.sorted_tables:
            if table.name.startswith(("security_", "permission_")):
                continue
            if table.name in post_baseline_tables:
                continue
            rows = (await s.execute(select(table))).mappings().all()
            source[table.name] = [
                {k: v for k, v in r.items() if k != "security_classification"} for r in rows
            ]

    def digest(rows):
        return hashlib.sha256(
            json.dumps(
                sorted(rows, key=lambda r: str(r.get("id", r.get("review_id")))),
                sort_keys=True,
                default=str,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    original = {name: digest(rows) for name, rows in source.items()}
    name = "huipi_b11a_roundtrip_" + uuid4().hex[:10] + "_test"
    url = postgres_engine.url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = create_async_engine(url)
    environment = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))

    async def alembic(*args, success=True):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
        )
        if success:
            assert result.returncode == 0, result.stderr[-1000:]
        return result

    try:
        async with admin.connect() as c:
            await c.execute(text('CREATE DATABASE "' + name + '"'))
        await alembic("upgrade", "c72d9e14a510")
        async with target.begin() as c:
            for table in Base.metadata.sorted_tables:
                if table.name not in source:
                    continue
                legacy = sa.table(
                    table.name,
                    *[
                        sa.column(col.name, col.type)
                        for col in table.c
                        if col.name != "security_classification"
                    ],
                )
                if source[table.name]:
                    await c.execute(insert(legacy), source[table.name])
        for action in [("upgrade", "head"), ("downgrade", "c72d9e14a510"), ("upgrade", "head")]:
            await alembic(*action)
            await target.dispose()  # DDL invalidates asyncpg prepared statements.
            if action[0] == "upgrade":
                await alembic("check")
            async with target.connect() as c:
                for table_name, expected in original.items():
                    # Fixed registry table names, not user-supplied SQL.
                    rows = (
                        (await c.execute(text('SELECT * FROM "' + table_name + '"')))
                        .mappings()
                        .all()
                    )
                    rows = [
                        {k: v for k, v in r.items() if k != "security_classification"} for r in rows
                    ]
                    assert digest(rows) == expected, table_name
        async with target.begin() as c:
            assert (
                await c.scalar(
                    text(
                        "SELECT bool_and(security_classification='legacy_homework') "
                        "FROM assignments"
                    )
                )
                is True
            )
            await c.execute(
                insert(Assignment.__table__).values(
                    id=uuid4(),
                    title="合成保护",
                    subject="数学",
                    grade_level="合成",
                    security_classification="exam_protected",
                )
            )
        async with target.connect() as c:
            version_before_rejection = await c.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        rejected = await alembic("downgrade", "c72d9e14a510", success=False)
        assert (
            rejected.returncode != 0 and "downgrade_requires_archived_new_data" in rejected.stderr
        )
        async with target.connect() as c:
            # The rejected downgrade is transactional: the schema stays at the current head.
            assert await c.scalar(text("SELECT version_num FROM alembic_version")) == (
                version_before_rejection
            )
    finally:
        await target.dispose()
        async with admin.connect() as c:
            await c.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
        await admin.dispose()


@pytest.mark.anyio
async def test_grant_identity_and_revocation_are_not_mutable(sec):
    ctx = sec
    async with ctx.factory() as s:
        receipt = await service.grant_role(
            s,
            principal(ctx),
            uuid4(),
            user_id=ctx.users["a"].id,
            role_version_id=service.CREATOR_ROLE,
            scope_id=service.INSTALLATION_SCOPE,
        )
    gid = UUID(receipt["grant_id"])
    async with ctx.factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(
                update(SecurityGrant)
                .where(SecurityGrant.id == gid)
                .values(user_id=ctx.users["b"].id)
            )
        await s.rollback()
    async with ctx.factory() as s:
        await service.revoke_grant(s, principal(ctx), uuid4(), grant_id=gid)
    async with ctx.factory() as s:
        first = await s.get(SecurityGrant, gid)
        timestamp = first.revoked_at
    async with ctx.factory() as s:
        await service.revoke_grant(s, principal(ctx), uuid4(), grant_id=gid)
    async with ctx.factory() as s:
        assert (await s.get(SecurityGrant, gid)).revoked_at == timestamp
        with pytest.raises(DBAPIError):
            await s.execute(
                update(SecurityGrant).where(SecurityGrant.id == gid).values(revoked_at=None)
            )
        await s.rollback()
