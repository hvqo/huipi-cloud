"""Current grant-filtered web projections; no privileged content session."""

from decimal import Decimal

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from huipi_cloud.modules.exam_configuration import service as configuration
from huipi_cloud.modules.identity.models import AssignmentMembership
from huipi_cloud.modules.scans import service as scans

from . import service as papers


async def authorize(db, p, action, pid=None):
    if action is None:
        return
    if action == "paper.create":
        await papers.authorize_root(db, p, action)
    else:
        await papers.authorize_paper(db, p, pid, action)


async def sources(db, p, offset=0):
    async with scans.transaction(db):
        await papers.authorize_root(db, p, "paper.create")
        m, a = AssignmentMembership.__table__, papers.A
        rows = (
            (
                await db.execute(
                    select(a.c.id, a.c.title, a.c.subject, a.c.grade_level)
                    .join(m, m.c.assignment_id == a.c.id)
                    .where(
                        a.c.security_classification == "legacy_homework",
                        a.c.status == "published",
                        m.c.user_id == p.user_id,
                        m.c.revoked_at.is_(None),
                    )
                    .order_by(a.c.created_at.desc(), a.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return dict(items=[dict(r) for r in rows[:20]], has_more=len(rows) > 20)


async def metadata(db, row, grants):
    from huipi_cloud.modules.identity.protocol import AccessError

    await papers.ensure_budget(db, row["canonical_assignment_id"])
    # SQL function owns canonical digest representation; no Python re-serialization substitute.
    from sqlalchemy import func

    doc = await db.scalar(select(cast(func.paper_document(row["canonical_assignment_id"]), JSONB)))
    questions = doc["questions"]
    mappings = await papers.mappings(db, row["id"])
    actual = await papers.digests(db, row["canonical_assignment_id"])
    if len(questions) > 200:
        raise AccessError("paper_content_too_large", 413)
    checks = dict(
        questions_complete=bool(questions) and all(q["stem"].strip() for q in questions),
        answers_complete=bool(questions)
        and all(q["answer_key"] and q["answer_key"]["answer_content"].strip() for q in questions),
        rubric_complete=bool(questions)
        and all(
            q["rubric"]
            and sum((Decimal(r["points"]) for r in q["rubric"]), Decimal(0))
            == Decimal(q["max_score"])
            for q in questions
        ),
        mapping_complete=bool(questions)
        and len(mappings) == len(questions)
        and [m["sort_order"] for m in mappings] == list(range(1, len(questions) + 1)),
        content_current=all(actual[k] == row[k] for k in actual)
        and await papers.mapping_digest(db, row["id"]) == row["mapping_digest"],
    )
    return dict(
        id=row["id"],
        family_id=row["paper_family_id"],
        prior_version_id=row["prior_version_id"],
        version_no=row["version_no"],
        revision=row["revision"],
        title=row["title"],
        status=row["status"],
        question_count=len(questions),
        max_points=str(sum((Decimal(q["max_score"]) for q in questions), Decimal("0.00"))),
        expected_content_sha256=actual["content_sha256"],
        expected_mapping_digest=await papers.mapping_digest(db, row["id"]),
        checks=checks,
        usable_for_exam=row["status"] == "locked" and all(checks.values()),
        actions=[
            a
            for a in ["paper.read", "paper.edit", "paper.lock"]
            if configuration.allowed(grants, {a}, "paper_version", row["id"])
        ],
    )


async def listing(db, p, offset=0, family_id=None):
    async with scans.transaction(db):
        grants = await configuration.actor_grants(db, p)
        query = select(papers.P).where(
            configuration.scope_filter(grants, "paper.read", "paper_version")
        )
        if family_id:
            # Authorize parent before disclosing which versions exist; each row is also filtered.
            parent = await papers.row(db, family_id)
            await papers.authorize_paper(db, p, parent["id"], "paper.read")
            query = query.where(papers.P.c.paper_family_id == parent["paper_family_id"])
        rows = (
            (
                await db.execute(
                    query.order_by(papers.P.c.created_at.desc(), papers.P.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[await metadata(db, r, grants) for r in rows[:20]],
            has_more=len(rows) > 20,
            can_create=configuration.allowed(grants, {"paper.create"}, "installation"),
        )


async def detail(db, p, pid):
    async with scans.transaction(db):
        await papers.authorize_paper(db, p, pid, "paper.read")
        row = await papers.row(db, pid, lock=True)
        summary = await metadata(db, row, await configuration.actor_grants(db, p))
        # Original canonical document and mapping; expose only stable question keys.
        from sqlalchemy import func

        doc = await db.scalar(
            select(cast(func.paper_document(row["canonical_assignment_id"]), JSONB))
        )
        numbers = dict(
            (
                await db.execute(
                    select(papers.Q.c.id, papers.Q.c.question_number).where(
                        papers.Q.c.assignment_id == row["canonical_assignment_id"]
                    )
                )
            ).all()
        )
        by_number = {q["question_number"]: q for q in doc["questions"]}
        result = []
        for m in await papers.mappings(db, pid):
            q = by_number[numbers[m["legacy_question_id"]]]
            result.append(
                dict(
                    question_key=m["question_key"],
                    printed_label=m["printed_label"],
                    sort_order=m["sort_order"],
                    question={
                        k: q[k] for k in ["question_number", "question_type", "stem", "max_score"]
                    },
                    answer_content=q["answer_key"]["answer_content"],
                    rubric=dict(criteria=q["rubric"]),
                )
            )
        return dict(paper=summary, questions=result)
