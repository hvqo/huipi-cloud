"""Authorized Core operations over protected carriers; no generic ORM bypass option."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Text, cast, delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.assignments.models import AnswerKey, Assignment, Question, RubricCriterion
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.papers.models import (
    PaperCreatorAuthority,
    PaperFamily,
    PaperOperation,
    PaperQuestionMapping,
    PaperVersion,
)
from huipi_cloud.modules.papers.permissions import PAPER_AUTHOR_ROLE, PAPER_OPERATOR_ROLE
from huipi_cloud.modules.papers.schemas import QuestionEdit, Reorder, SnapshotCreate, VersionWrite
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant, SecurityScope

A, Q, K, R = (m.__table__ for m in (Assignment, Question, AnswerKey, RubricCriterion))
F, P, M, OPS = (
    m.__table__
    for m in (
        PaperFamily,
        PaperVersion,
        PaperQuestionMapping,
        PaperOperation,
    )
)
MAX_QUESTIONS = 200
MAX_CONTENT_BYTES = 2 * 1024 * 1024
DENIED = "resource_not_found_or_forbidden"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Operation:
    replay: dict | None
    operation_id: UUID
    result: dict | None = None


@asynccontextmanager
async def operation(session, principal, request_id, action, payload):
    fingerprint = digest({"action": action, "payload": payload})
    committing = False
    try:
        await session.begin()
        await session.execute(select(func.set_config("statement_timeout", "8000", True)))
        await identity.refresh_principal(session, principal, lock=True)
        key = int.from_bytes(
            hashlib.sha256(f"paper:{principal.user_id}:{request_id}".encode()).digest()[:8],
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(key)))
        old = (
            (
                await session.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == principal.user_id,
                        OPS.c.request_id == request_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if old and old["payload_sha256"] != fingerprint:
            raise AccessError("paper_request_conflict", 409)
        op = Operation(dict(old["result"]) if old else None, old["id"] if old else uuid4())
        yield op
        if op.replay is None:
            if op.result is None:
                raise RuntimeError("paper operation missing result")
            await session.execute(
                insert(OPS).values(
                    id=op.operation_id,
                    actor_user_id=principal.user_id,
                    request_id=request_id,
                    action=action,
                    payload_sha256=fingerprint,
                    result=op.result,
                )
            )
        committing = True
        await session.commit()
    except BaseException as error:
        try:
            await session.rollback()
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        if isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            code = getattr(getattr(error, "orig", None), "sqlstate", None)
            if not committing and code in {"23505", "23514", "23503", "40001", "40P01"}:
                raise AccessError("paper_write_conflict", 409) from None
            raise AccessError(
                "paper_commit_outcome_unknown" if committing else "paper_operation_unavailable",
                503,
            ) from None
        raise


async def authorize_root(session, principal, action):
    root = await security.installation_scope(session)
    await security.require_permission(session, principal, action, root.id)


async def authorize_paper(session, principal, pid, action):
    target = await session.scalar(
        select(SecurityScope.id).where(
            SecurityScope.kind == "paper_version",
            SecurityScope.paper_version_id == pid,
        )
    )
    if target is None:
        raise AccessError(DENIED, 404)
    await security.require_permission(session, principal, action, target)


async def row(session, pid, *, lock=False):
    query = select(P).where(P.c.id == pid)
    result = (
        (await session.execute(query.with_for_update() if lock else query)).mappings().one_or_none()
    )
    if result is None:
        raise AccessError(DENIED, 404)
    return result


async def ensure_budget(session, aid):
    size = await session.scalar(select(func.octet_length(cast(func.paper_document(aid), Text))))
    if size is None or size > MAX_CONTENT_BYTES:
        raise AccessError("paper_content_too_large", 413)


async def digests(session, aid):
    return await session.scalar(select(cast(func.paper_digests(aid), JSONB)))


async def mapping_digest(session, pid):
    return await session.scalar(select(func.paper_mapping_digest(pid)))


async def mappings(session, pid):
    return [
        dict(r)
        for r in (
            await session.execute(
                select(M).where(M.c.paper_version_id == pid).order_by(M.c.sort_order)
            )
        ).mappings()
    ]


async def summary(session, paper):
    d = await digests(session, paper["canonical_assignment_id"])
    return dict(
        paper_version_id=str(paper["id"]),
        paper_family_id=str(paper["paper_family_id"]),
        version_no=paper["version_no"],
        revision=paper["revision"],
        title=paper["title"],
        status=paper["status"],
        canonical_assignment_id=str(paper["canonical_assignment_id"]),
        source_assignment_id=str(paper["source_assignment_id"]),
        prior_version_id=str(paper["prior_version_id"]) if paper["prior_version_id"] else None,
        source_content_sha256=paper["source_content_sha256"],
        **d,
        mapping_digest=await mapping_digest(session, paper["id"]),
        locked_at=paper["locked_at"].isoformat() if paper["locked_at"] else None,
    )


async def validate_write(session, request, *, draft=True):
    paper = await row(session, request.paper_version_id, lock=True)
    if draft and paper["status"] != "draft":
        raise AccessError("paper_locked", 409)
    if paper["revision"] != request.expected_revision:
        raise AccessError("paper_revision_conflict", 409)
    current = await digests(session, paper["canonical_assignment_id"])
    if (
        current["content_sha256"] != request.expected_content_sha256
        or await mapping_digest(session, paper["id"]) != request.expected_mapping_digest
    ):
        raise AccessError("paper_content_changed", 409)
    return paper


async def source_rows(session, aid):
    """Lock actual rows, including children: published is not an immutability guarantee."""
    assignment = (
        (await session.execute(select(A).where(A.c.id == aid).with_for_update()))
        .mappings()
        .one_or_none()
    )
    if assignment is None:
        raise AccessError(DENIED, 404)
    questions = list(
        (
            await session.execute(
                select(Q)
                .where(Q.c.assignment_id == aid)
                .order_by(Q.c.id)
                .limit(MAX_QUESTIONS + 1)
                .with_for_update()
            )
        ).mappings()
    )
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise AccessError("paper_source_question_limit", 422)
    ids = [q["id"] for q in questions]
    answers = list(
        (
            await session.execute(
                select(K).where(K.c.question_id.in_(ids)).order_by(K.c.id).with_for_update()
            )
        ).mappings()
    )
    rubrics = list(
        (
            await session.execute(
                select(R).where(R.c.question_id.in_(ids)).order_by(R.c.id).with_for_update()
            )
        ).mappings()
    )
    for q in questions:
        answer = next((k for k in answers if k["question_id"] == q["id"]), None)
        criteria = [r for r in rubrics if r["question_id"] == q["id"]]
        if (
            answer is None
            or not criteria
            or len(criteria) > 100
            or sum(r["points"] for r in criteria) != q["max_score"]
        ):
            raise AccessError("paper_source_incomplete", 409)
    await ensure_budget(session, aid)
    doc = await session.scalar(select(cast(func.paper_document(aid), JSONB)))
    if len(json.dumps(doc, ensure_ascii=False).encode()) > MAX_CONTENT_BYTES:
        raise AccessError("paper_content_too_large", 413)
    return assignment, sorted(questions, key=lambda q: q["question_number"]), answers, rubrics


async def clone(
    session, principal, source, family_id, title, version_no, prior=None, *, operation_id
):
    assignment, questions, answers, rubrics = source
    source_sha = await digests(session, assignment["id"])
    carrier, pid = uuid4(), uuid4()
    await session.execute(
        insert(A).values(
            id=carrier,
            **{k: assignment[k] for k in ("title", "subject", "grade_level", "description")},
            status="draft",
            security_classification="exam_protected",
        )
    )
    prior_maps = await mappings(session, prior["id"]) if prior else []
    prepared = []
    for q in questions:
        qid = uuid4()
        await session.execute(
            insert(Q).values(
                id=qid,
                assignment_id=carrier,
                **{k: q[k] for k in ("question_number", "question_type", "stem", "max_score")},
            )
        )
        answer = next(k for k in answers if k["question_id"] == q["id"])
        await session.execute(
            insert(K).values(
                id=uuid4(),
                question_id=qid,
                answer_content=answer["answer_content"],
                source=answer["source"],
            )
        )
        criteria = sorted(
            [r for r in rubrics if r["question_id"] == q["id"]], key=lambda r: r["sort_order"]
        )
        await session.execute(
            insert(R),
            [
                dict(
                    id=uuid4(),
                    question_id=qid,
                    **{k: r[k] for k in ("description", "points", "sort_order")},
                )
                for r in criteria
            ],
        )
        previous = next(
            (m for m in prior_maps if m["legacy_question_id"] == q["id"]),
            None,
        )
        if prior is not None and previous is None:
            raise AccessError("paper_mapping_set_mismatch", 409)
        prepared.append(
            dict(
                question_key=previous["question_key"] if previous else uuid4(),
                legacy_question_id=qid,
                printed_label=previous["printed_label"] if previous else str(q["question_number"]),
                sort_order=previous["sort_order"] if previous else len(prepared) + 1,
                source_question_id=q["id"],
                source_rubric_sha256=digest([dict(r) for r in criteria]),
                prior_mapping_id=previous["id"] if previous else None,
            )
        )
    prepared.sort(key=lambda m: m["sort_order"])
    md = await session.scalar(
        select(func.paper_json_digest(cast(literal(json.dumps(prepared, default=str)), JSONB)))
    )
    await session.execute(
        insert(P).values(
            id=pid,
            paper_family_id=family_id,
            version_no=version_no,
            revision=1,
            title=title,
            status="draft",
            canonical_assignment_id=carrier,
            source_assignment_id=assignment["id"],
            source_content_sha256=source_sha["content_sha256"],
            **source_sha,
            mapping_digest=md,
            created_by_user_id=principal.user_id,
            prior_version_id=prior["id"] if prior else None,
        )
    )
    await session.execute(
        insert(M),
        [
            dict(id=uuid4(), paper_version_id=pid, canonical_assignment_id=carrier, **m)
            for m in prepared
        ],
    )
    scope_id, grant_id = uuid4(), uuid4()
    # Serialize the existing active-Grant budget for concurrent creations by one actor.
    budget_key = int.from_bytes(
        hashlib.sha256(f"paper-creator-budget:{principal.user_id}".encode()).digest()[:8],
        signed=True,
    )
    await session.execute(select(func.pg_advisory_xact_lock(budget_key)))
    count = await session.scalar(
        select(func.count())
        .select_from(SecurityGrant)
        .where(SecurityGrant.user_id == principal.user_id, SecurityGrant.revoked_at.is_(None))
    )
    if count >= 100:
        raise AccessError("paper_creator_grant_limit", 409)
    await session.execute(
        insert(SecurityScope).values(id=scope_id, kind="paper_version", paper_version_id=pid)
    )
    codes = await security.role_codes(session, PAPER_AUTHOR_ROLE, "paper_version")
    if codes != {"paper.read", "paper.edit", "paper.lock"}:
        raise AccessError("paper_author_policy_invalid", 503)
    # Domain endowment, NOT general self-delegation. IDs are exclusively minted above.
    # DB deferred checks bind this Grant to this transaction's new version and audit.
    await session.execute(
        insert(SecurityGrant).values(
            id=grant_id,
            user_id=principal.user_id,
            role_version_id=PAPER_AUTHOR_ROLE,
            scope_id=scope_id,
        )
    )
    await session.execute(
        insert(PaperCreatorAuthority).values(
            paper_version_id=pid,
            grant_id=grant_id,
            operation_id=operation_id,
            actor_user_id=principal.user_id,
        )
    )
    paper = await row(session, pid)
    return await summary(session, paper)


async def create_snapshot(session, principal, request: SnapshotCreate):
    return await _create_snapshot(session, principal, request, member_source=False)


async def require_member_source(session, principal, aid):
    """Web import requires explicit membership, including for legacy administrators."""
    from huipi_cloud.modules.identity.models import AssignmentMembership

    await identity.require_assignment(session, principal, aid, lock=True)
    member = await session.scalar(
        select(AssignmentMembership.id)
        .where(
            AssignmentMembership.assignment_id == aid,
            *identity.membership_filter(principal.user_id),
        )
        .with_for_update(read=True)
    )
    if member is None:
        raise AccessError(DENIED, 404)
    source = (
        (await session.execute(select(A).where(A.c.id == aid).with_for_update())).mappings().one()
    )
    if source["security_classification"] != "legacy_homework":
        raise AccessError(DENIED, 404)
    if source["status"] != "published":
        raise AccessError("paper_source_not_published", 409)


async def create_member_snapshot(session, principal, request: SnapshotCreate):
    """Fixed HTTP import entry; caller cannot relax this policy through a request."""
    return await _create_snapshot(session, principal, request, member_source=True)


async def _create_snapshot(session, principal, request, *, member_source):
    async with operation(
        session, principal, request.request_id, "snapshot", request.model_dump(mode="json")
    ) as op:
        await authorize_root(session, principal, "paper.create")
        await identity.require_assignment(
            session, principal, request.source_assignment_id, lock=True
        )
        if member_source:
            await require_member_source(session, principal, request.source_assignment_id)
        if op.replay is not None:
            await authorize_paper(
                session, principal, UUID(op.replay["paper_version_id"]), "paper.read"
            )
            return op.replay
        family_key = digest({"source": str(request.source_assignment_id), "title": request.title})
        await session.execute(
            pg_insert(F)
            .values(
                id=uuid4(), creation_key_sha256=family_key, created_by_user_id=principal.user_id
            )
            .on_conflict_do_nothing()
        )
        family = (
            (
                await session.execute(
                    select(F).where(F.c.creation_key_sha256 == family_key).with_for_update()
                )
            )
            .mappings()
            .one()
        )
        source = await source_rows(session, request.source_assignment_id)
        sha = (await digests(session, request.source_assignment_id))["content_sha256"]
        previous = (
            (
                await session.execute(
                    select(P)
                    .where(P.c.paper_family_id == family["id"])
                    .order_by(P.c.version_no.desc())
                    .limit(1)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if previous:
            await authorize_paper(session, principal, previous["id"], "paper.read")
            if (
                previous["source_content_sha256"] == sha
                and previous["source_assignment_id"] == request.source_assignment_id
                and (await digests(session, previous["canonical_assignment_id"]))["content_sha256"]
                == sha
            ):
                op.result = await summary(session, previous)
                return op.result
            if previous["status"] == "draft":
                raise AccessError("paper_draft_exists", 409)
            raise AccessError("paper_new_version_requires_fork", 409)
        op.result = await clone(
            session,
            principal,
            source,
            family["id"],
            request.title,
            previous["version_no"] + 1 if previous else 1,
            previous,
            operation_id=op.operation_id,
        )
        return op.result


async def inspect_paper(session, principal, pid):
    try:
        async with session.begin():
            await authorize_paper(session, principal, pid, "paper.read")
            paper = await row(session, pid, lock=True)
            result = await summary(session, paper)
            result["mappings"] = [
                json.loads(json.dumps(m, default=str)) for m in await mappings(session, pid)
            ]
            await ensure_budget(session, paper["canonical_assignment_id"])
            doc = await session.scalar(
                select(cast(func.paper_document(paper["canonical_assignment_id"]), JSONB))
            )
            if len(json.dumps(doc, ensure_ascii=False).encode()) > MAX_CONTENT_BYTES:
                raise AccessError("paper_content_too_large", 413)
            result["content"] = doc
            return result
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("paper_operation_unavailable", 503) from None


async def edit_question(session, principal, request: QuestionEdit):
    async with operation(
        session, principal, request.request_id, "edit_question", request.model_dump(mode="json")
    ) as op:
        await authorize_paper(session, principal, request.paper_version_id, "paper.edit")
        if op.replay is not None:
            return op.replay
        paper = await validate_write(session, request)
        mapping = next(
            (
                m
                for m in await mappings(session, paper["id"])
                if m["question_key"] == request.question_key
            ),
            None,
        )
        if mapping is None:
            raise AccessError(DENIED, 404)
        qid = mapping["legacy_question_id"]
        await session.execute(
            update(Q).where(Q.c.id == qid).values(**request.question.model_dump())
        )
        await session.execute(
            update(K)
            .where(K.c.question_id == qid)
            .values(answer_content=request.answer_content, source="teacher")
        )
        await session.execute(delete(R).where(R.c.question_id == qid))
        await session.execute(
            insert(R),
            [dict(id=uuid4(), question_id=qid, **r.model_dump()) for r in request.rubric.criteria],
        )
        op.result = await bump(session, paper)
        return op.result


async def bump(session, paper, **changes):
    await ensure_budget(session, paper["canonical_assignment_id"])
    d = await digests(session, paper["canonical_assignment_id"])
    await session.execute(
        update(P)
        .where(P.c.id == paper["id"])
        .values(
            revision=paper["revision"] + 1,
            **d,
            mapping_digest=await mapping_digest(session, paper["id"]),
            **changes,
        )
    )
    return await summary(session, await row(session, paper["id"]))


async def reorder(session, principal, request: Reorder):
    async with operation(
        session, principal, request.request_id, "reorder", request.model_dump(mode="json")
    ) as op:
        await authorize_paper(session, principal, request.paper_version_id, "paper.edit")
        if op.replay is not None:
            return op.replay
        paper = await validate_write(session, request)
        old = await mappings(session, paper["id"])
        by_key = {m["question_key"]: m for m in old}
        if set(by_key) != {m.question_key for m in request.mappings}:
            raise AccessError("paper_mapping_set_mismatch", 409)
        await session.execute(delete(M).where(M.c.paper_version_id == paper["id"]))
        values = []
        for position, item in enumerate(request.mappings, 1):
            values.append(
                dict(
                    by_key[item.question_key], printed_label=item.printed_label, sort_order=position
                )
            )
        await session.execute(insert(M), values)
        op.result = await bump(session, paper)
        return op.result


async def lock_paper(session, principal, request: VersionWrite):
    async with operation(
        session, principal, request.request_id, "lock", request.model_dump(mode="json")
    ) as op:
        await authorize_paper(session, principal, request.paper_version_id, "paper.lock")
        if op.replay is not None:
            return op.replay
        paper = await validate_write(session, request)
        # All direct content writers take a SHARE lock on this PaperVersion in DB triggers.
        # Our UPDATE lock fences them; digest SELECTs need no child locks (avoids lock inversion).
        doc = await session.scalar(
            select(cast(func.paper_document(paper["canonical_assignment_id"]), JSONB))
        )
        validate_document(doc)
        await session.execute(
            update(A).where(A.c.id == paper["canonical_assignment_id"]).values(status="published")
        )
        op.result = await bump(session, paper, status="locked", locked_at=func.clock_timestamp())
        return op.result


def validate_document(doc):
    if not doc["questions"] or len(doc["questions"]) > MAX_QUESTIONS:
        raise AccessError("paper_incomplete", 409)
    from decimal import Decimal

    for q in doc["questions"]:
        if (
            not q["answer_key"]
            or not q["rubric"]
            or sum(Decimal(r["points"]) for r in q["rubric"]) != Decimal(q["max_score"])
        ):
            raise AccessError("paper_incomplete", 409)


async def retire_paper(session, principal, request: VersionWrite):
    async with operation(
        session, principal, request.request_id, "retire", request.model_dump(mode="json")
    ) as op:
        await authorize_paper(session, principal, request.paper_version_id, "paper.edit")
        if op.replay is not None:
            return op.replay
        paper = await validate_write(session, request, draft=False)
        if paper["status"] not in {"draft", "locked"}:
            raise AccessError("paper_state_conflict", 409)
        await session.execute(
            update(P)
            .where(P.c.id == paper["id"])
            .values(status="retired", revision=paper["revision"] + 1)
        )
        op.result = await summary(session, await row(session, paper["id"]))
        return op.result


async def fork_paper(session, principal, request: VersionWrite):
    async with operation(
        session, principal, request.request_id, "fork", request.model_dump(mode="json")
    ) as op:
        await authorize_paper(session, principal, request.paper_version_id, "paper.edit")
        await authorize_root(session, principal, "paper.create")
        if op.replay is not None:
            await authorize_paper(
                session, principal, UUID(op.replay["paper_version_id"]), "paper.read"
            )
            return op.replay
        # Lock family before PaperVersion, matching snapshot creation order.
        original = await row(session, request.paper_version_id)
        await session.execute(
            select(F.c.id).where(F.c.id == original["paper_family_id"]).with_for_update()
        )
        paper = await validate_write(session, request, draft=False)
        if paper["locked_at"] is None:
            raise AccessError("paper_fork_requires_locked", 409)
        latest = await session.scalar(
            select(func.max(P.c.version_no)).where(P.c.paper_family_id == paper["paper_family_id"])
        )
        if latest != paper["version_no"]:
            raise AccessError("paper_successor_exists", 409)
        source = await source_rows(session, paper["canonical_assignment_id"])
        op.result = await clone(
            session,
            principal,
            source,
            paper["paper_family_id"],
            paper["title"],
            paper["version_no"] + 1,
            paper,
            operation_id=op.operation_id,
        )
        return op.result


async def enable_operator(session, actor_user_id, target_user_id, request_id, approval_ref):
    """Explicit DB-credential deployment approval; not an HTTP or bearer self-upgrade."""
    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("paper_deployment_approval_required", 422)
    user = await session.get(User, actor_user_id)
    if user is None:
        raise AccessError(DENIED, 404)
    principal = Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await session.rollback()
    async with operation(
        session,
        principal,
        request_id,
        "enable_operator",
        dict(target_user_id=str(target_user_id), approval_ref=approval_ref),
    ) as op:
        await authorize_root(session, principal, "security.grants.manage")
        if op.replay is not None:
            return op.replay
        target = await session.scalar(
            select(User)
            .where(User.id == target_user_id, User.is_active.is_(True))
            .with_for_update(read=True)
        )
        if target is None:
            raise AccessError(DENIED, 404)
        grant = SecurityGrant(
            user_id=target_user_id,
            role_version_id=PAPER_OPERATOR_ROLE,
            scope_id=security.INSTALLATION_SCOPE,
        )
        session.add(grant)
        await session.flush()
        op.result = dict(
            grant_id=str(grant.id),
            target_user_id=str(target_user_id),
            role_version_id=str(PAPER_OPERATOR_ROLE),
        )
        return op.result
