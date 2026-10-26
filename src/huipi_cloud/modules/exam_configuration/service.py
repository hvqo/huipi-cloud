"""Fixed, scope-filtered projections and date-qualified roster previews.

Writes remain in the original exam/roster services and their audited transactions.
No raw answers, private object keys, or arbitrary SQL/table interfaces are returned.
"""

from decimal import Decimal

from sqlalchemy import false, func, or_, select

from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import (
    AcademicTerm,
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    Student,
    Subject,
)
from huipi_cloud.modules.papers import service as papers
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant
from huipi_cloud.modules.scans import service as scans

from . import schemas
from .schemas import ConfigurationEventView, ConfigurationPaperChoice, ConfigurationSessionView

Y, T, C, EN, ST, SU = (
    m.__table__
    for m in (AcademicYear, AcademicTerm, AdministrativeClass, ClassEnrollment, Student, Subject)
)
G = SecurityGrant.__table__


async def actor_grants(db, p):
    await identity.refresh_principal(db, p, lock=True)
    rows = (
        (
            await db.execute(
                select(G)
                .where(G.c.user_id == p.user_id, G.c.revoked_at.is_(None))
                .order_by(G.c.id)
                .limit(101)
                .with_for_update(read=True)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 100:
        raise AccessError("exam_configuration_grant_limit", 413)
    values = []
    for row in rows:
        scope = await security.scope(db, row["scope_id"])
        codes = await security.role_codes(db, row["role_version_id"], scope.kind)
        values.append((scope, codes))
    return values


def allowed(grants, actions, kind, identifier=None, event_id=None):
    for scope, codes in grants:
        if not set(actions) <= codes:
            continue
        if scope.kind == "installation":
            return True
        if kind == "exam_event" and scope.kind == kind and scope.exam_event_id == identifier:
            return True
        if kind == "exam_session" and (
            scope.kind == kind
            and scope.exam_session_id == identifier
            or scope.kind == "exam_event"
            and scope.exam_event_id == event_id
        ):
            return True
        if kind == "paper_version" and scope.kind == kind and scope.paper_version_id == identifier:
            return True
    return False


def scope_filter(grants, action, kind):
    table = exams.E if kind == "exam_event" else exams.S if kind == "exam_session" else papers.P
    filters = []
    for scope, codes in grants:
        if action not in codes:
            continue
        if scope.kind == "installation":
            return table.c.id.is_not(None)
        if scope.kind == kind:
            filters.append(table.c.id == getattr(scope, kind + "_id"))
        elif kind == "exam_session" and scope.kind == "exam_event":
            filters.append(table.c.exam_event_id == scope.exam_event_id)
    return or_(*filters) if filters else false()


async def http_authorize(db, p, kind, identifiers, actions):
    if kind == "list":
        return
    if kind == "installation":
        await papers.authorize_root(db, p, actions[0])
        return
    if kind == "roster":
        roster = await exams.row(db, rosters.R, identifiers["roster_id"])
        identifier = roster["exam_session_id"]
        kind = "exam_session"
    else:
        identifier = identifiers["event_id" if kind == "exam_event" else "session_id"]
    await exams.authorize(db, p, kind, identifier, set(actions))


async def options(db, p, offset=0):
    async with scans.transaction(db):
        grants = await actor_grants(db, p)
        org = allowed(grants, {"organization.read"}, "installation")
        values = {}
        truncated = False
        for name, table, filters in [
            ("years", Y, Y.c.status != "closed"),
            ("terms", T, T.c.id.is_not(None)),
            ("subjects", SU, SU.c.is_active),
            ("classes", C, C.c.status == "active"),
        ]:
            rows = (
                (
                    (
                        await db.execute(
                            select(
                                *[
                                    table.c[f]
                                    for f in getattr(
                                        schemas,
                                        {
                                            "years": "AcademicYearSummary",
                                            "terms": "AcademicTermSummary",
                                            "subjects": "SubjectSummary",
                                            "classes": "AdministrativeClassSummary",
                                        }[name],
                                    ).model_fields
                                ]
                            )
                            .where(filters)
                            .order_by(table.c.id)
                            .offset(offset)
                            .limit(101)
                        )
                    )
                    .mappings()
                    .all()
                )
                if org
                else []
            )
            values[name] = [exams.public(row) for row in rows[:100]]
            truncated |= len(rows) > 100
        return dict(
            can_create_event=allowed(grants, {"exam.event.create"}, "installation"),
            organization_available=org,
            truncated=truncated,
            **values,
        )


async def term_label(db, term_id):
    value = (
        await db.execute(
            select(Y.c.display_name, T.c.code)
            .join(T, T.c.academic_year_id == Y.c.id)
            .where(T.c.id == term_id)
        )
    ).one()
    return f"{value.display_name} · {value.code}"


async def event_value(db, row, grants):
    return ConfigurationEventView(
        **dict(row),
        term_label=await term_label(db, row["academic_term_id"]),
        actions=[a for a in ["exam.event.manage"] if allowed(grants, {a}, "exam_event", row["id"])],
    )


async def events(db, p, offset=0):
    async with scans.transaction(db):
        grants = await actor_grants(db, p)
        rows = (
            (
                await db.execute(
                    select(exams.E)
                    .where(scope_filter(grants, "exam.event.read", "exam_event"))
                    .order_by(exams.E.c.created_at.desc(), exams.E.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[await event_value(db, r, grants) for r in rows[:20]], has_more=len(rows) > 20
        )


async def event(db, p, identifier):
    async with scans.transaction(db):
        await exams.authorize(db, p, "exam_event", identifier, {"exam.event.read"})
        return await event_value(
            db, await exams.row(db, exams.E, identifier), await actor_grants(db, p)
        )


async def paper_choice(db, row):
    from huipi_cloud.modules.assignments.models import Question, RubricCriterion

    q, r = Question.__table__, RubricCriterion.__table__
    rows = (
        (
            await db.execute(
                select(
                    q.c.id,
                    q.c.max_score,
                    func.coalesce(func.sum(r.c.points), 0).label("rubric_points"),
                    func.count(r.c.id).label("criteria"),
                )
                .join(papers.M, papers.M.c.legacy_question_id == q.c.id)
                .outerjoin(r, r.c.question_id == q.c.id)
                .where(
                    papers.M.c.paper_version_id == row["id"],
                    q.c.assignment_id == row["canonical_assignment_id"],
                )
                .group_by(q.c.id)
                .limit(201)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 200:
        raise AccessError("paper_content_too_large", 413)
    actual = await papers.digests(db, row["canonical_assignment_id"])
    return ConfigurationPaperChoice(
        paper_version_id=row["id"],
        title=row["title"],
        version_no=row["version_no"],
        status=row["status"],
        question_count=len(rows),
        max_points=str(sum((r["max_score"] for r in rows), Decimal("0.00"))),
        rubric_complete=bool(rows)
        and all(r["criteria"] and r["rubric_points"] == r["max_score"] for r in rows),
        content_current=actual["content_sha256"] == row["content_sha256"]
        and await papers.mapping_digest(db, row["id"]) == row["mapping_digest"],
    )


async def available_papers(db, p, offset=0):
    async with scans.transaction(db):
        grants = await actor_grants(db, p)
        rows = (
            (
                await db.execute(
                    select(papers.P)
                    .where(
                        papers.P.c.status == "locked",
                        scope_filter(grants, "paper.use", "paper_version"),
                    )
                    .order_by(papers.P.c.created_at.desc(), papers.P.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return dict(items=[await paper_choice(db, r) for r in rows[:20]], has_more=len(rows) > 20)


async def session_value(db, row, grants, *, detail=False):
    parent = await exams.row(db, exams.E, row["exam_event_id"])
    subject = await exams.row(db, SU, row["subject_id"])
    actions = [
        a
        for a in [
            "exam.session.manage",
            "exam.roster.read",
            "exam.roster.manage",
            "exam.roster.freeze",
            "exam.roster.correct",
        ]
        if allowed(grants, {a}, "exam_session", row["id"], row["exam_event_id"])
    ]
    if allowed(
        grants,
        {"exam.session.manage", "exam.session.paper.bind"},
        "exam_session",
        row["id"],
        row["exam_event_id"],
    ):
        actions.append("exam.session.paper.bind")
    bindings = []
    if detail:
        for binding in (
            await db.execute(
                select(exams.B)
                .where(exams.B.c.exam_session_id == row["id"])
                .order_by(exams.B.c.created_at, exams.B.c.id)
                .limit(200)
            )
        ).mappings():
            paper = await exams.row(db, papers.P, binding["paper_version_id"])
            choice = await paper_choice(db, paper)
            bindings.append(
                dict(
                    **choice.model_dump(),
                    id=binding["id"],
                    variant_code=binding["variant_code"],
                    paper_status=paper["status"],
                )
                | {"status": binding["status"]}
            )
    return ConfigurationSessionView(
        **dict(row),
        event_title=parent["title"],
        subject_name=subject["display_name"],
        term_label=await term_label(db, parent["academic_term_id"]),
        actions=actions,
        papers=bindings,
    )


async def sessions(db, p, event_id=None, offset=0):
    async with scans.transaction(db):
        grants = await actor_grants(db, p)
        query = select(exams.S).where(scope_filter(grants, "exam.session.read", "exam_session"))
        if event_id:
            query = query.where(exams.S.c.exam_event_id == event_id)
        rows = (
            (
                await db.execute(
                    query.order_by(exams.S.c.created_at.desc(), exams.S.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[await session_value(db, r, grants) for r in rows[:20]], has_more=len(rows) > 20
        )


async def session(db, p, identifier):
    async with scans.transaction(db):
        await exams.authorize(db, p, "exam_session", identifier, {"exam.session.read"})
        return await session_value(
            db, await exams.row(db, exams.S, identifier), await actor_grants(db, p), detail=True
        )


async def candidates(db, p, sid, request):
    async with scans.transaction(db):
        await rosters.authorize(db, p, sid, "manage", "read")
        await papers.authorize_root(db, p, "organization.read")
        value = await rosters.locked_session(db, sid)
        exams.revision(value, request.expected_session_revision)
        business_date = value["starts_at"].astimezone(exams.SCHOOL_ZONE).date()
        classes = (
            (
                await db.execute(
                    select(C)
                    .where(C.c.id.in_(request.class_ids), C.c.status == "active")
                    .order_by(C.c.id)
                    .with_for_update(read=True)
                )
            )
            .mappings()
            .all()
        )
        if len(classes) != len(request.class_ids):
            raise AccessError("roster_class_unavailable", 409)
        query = (
            select(
                ST.c.id.label("student_id"),
                ST.c.name.label("student_name"),
                ST.c.school_student_code.label("student_code"),
                ST.c.is_active,
                C.c.id.label("class_id"),
                C.c.display_name.label("class_name"),
            )
            .join(EN, EN.c.student_id == ST.c.id)
            .join(C, C.c.id == EN.c.administrative_class_id)
            .where(
                C.c.id.in_(request.class_ids),
                EN.c.valid_from <= business_date,
                or_(EN.c.valid_to.is_(None), EN.c.valid_to > business_date),
            )
        )
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        if total > 2000:
            raise AccessError("roster_entry_limit", 413)
        rows = (
            (
                await db.execute(
                    query.order_by(C.c.code, ST.c.school_student_code, ST.c.id)
                    .offset(request.offset)
                    .limit(request.limit)
                )
            )
            .mappings()
            .all()
        )
        current = (
            (
                await db.execute(
                    select(rosters.E)
                    .join(rosters.R, rosters.R.c.id == rosters.E.c.roster_revision_id)
                    .where(rosters.R.c.exam_session_id == sid, rosters.R.c.is_current)
                )
            )
            .mappings()
            .all()
        )
        by_student = {e["student_id"]: e for e in current}
        students = []
        for r in rows:
            old = by_student.get(r["student_id"], {})
            students.append(
                dict(
                    student_id=r["student_id"],
                    student_name=r["student_name"],
                    student_code=r["student_code"],
                    class_id=r["class_id"],
                    class_name=r["class_name"],
                    exam_number=old.get("exam_number"),
                    session_paper_id=old.get("session_paper_id"),
                    issues=[] if r["is_active"] else ["student_inactive"],
                )
            )
        # Count the same date-qualified query, not today's class membership.
        source = query.subquery()
        grouped = (
            await db.execute(select(source.c.class_id, func.count()).group_by(source.c.class_id))
        ).all()
        class_counts = {str(c["id"]): 0 for c in classes}
        class_counts.update({str(i): n for i, n in grouped})
        return dict(
            business_date=business_date,
            session_revision=value["revision"],
            students=students,
            total=total,
            offset=request.offset,
            has_more=request.offset + len(rows) < total,
            class_counts=class_counts,
            warnings=["empty_class"] if any(n == 0 for n in class_counts.values()) else [],
        )
