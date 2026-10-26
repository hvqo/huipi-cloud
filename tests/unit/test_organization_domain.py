"""B-1.1b domain contracts: derived grade labels, strict schemas and fixed permission catalog.

These tests are deliberately database-free: they pin the pure derivation rule, the frozen
validation contract and the fact that new organization actions are installation-scoped only
and never reachable through the legacy ``User.is_admin`` shortcut.
"""

import inspect
from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.identity import dependencies as identity_dependencies
from huipi_cloud.modules.organization import operations as org_operations
from huipi_cloud.modules.organization import service as org_service
from huipi_cloud.modules.organization.schemas import (
    AcademicTermCreate,
    AcademicYearCreate,
    AdministrativeClassCreate,
    GradeCohortCreate,
    MutationRequest,
    StudentImportConfirmRequest,
    StudentTransferRequest,
    SubjectCreate,
)
from huipi_cloud.modules.resource_security import service as security_service

NEW_ACTIONS = frozenset(
    {
        "organization.read",
        "organization.manage",
        "student.import.preview",
        "student.import.confirm",
    }
)

CREATE_SCHEMAS = [
    (
        AcademicYearCreate,
        dict(
            request_id=uuid4(),
            code="2026-2027",
            display_name="2026-2027学年",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 7, 10),
        ),
    ),
    (
        AcademicTermCreate,
        dict(
            request_id=uuid4(),
            academic_year_id=uuid4(),
            term_no=1,
            code="T1",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 1, 20),
        ),
    ),
    (
        GradeCohortCreate,
        dict(request_id=uuid4(), code="2026", display_name="2026级", intake_year=2026),
    ),
    (SubjectCreate, dict(request_id=uuid4(), code="MATH", display_name="数学")),
    (
        AdministrativeClassCreate,
        dict(request_id=uuid4(), grade_cohort_id=uuid4(), code="G1-01", display_name="高一1班"),
    ),
]


@pytest.mark.parametrize(
    "intake_year,start_year,expected",
    [
        # Admission year is grade 1, then 2 and 3 for the following school years.
        (2025, 2025, (1, "高一")),
        (2025, 2026, (2, "高二")),
        (2025, 2027, (3, "高三")),
        # Grade 4 (graduated) and grade 0 (not yet admitted) are outside high school.
        (2025, 2028, None),
        (2025, 2029, None),
        (2025, 2024, None),
        (2025, 2000, None),
        (2025, 2100, None),
        # Same rule with a different intake year.
        (2020, 2020, (1, "高一")),
        (2020, 2022, (3, "高三")),
        (2020, 2023, None),
    ],
)
def test_grade_label_derivation_boundaries(intake_year, start_year, expected):
    assert org_service.grade_label(intake_year, start_year) == expected


def test_grade_label_is_never_stored_on_the_student_model():
    # Grade is derived from (intake year, academic year); a student row keeps no grade.
    assert "grade" not in {column.key for column in org_service.Student.__table__.c}
    assert "grade_label" in inspect.getsource(org_service)
    assert org_service.GRADE_LABELS == {1: "高一", 2: "高二", 3: "高三"}


def test_academic_year_schema_rejects_non_increasing_dates():
    base = dict(
        request_id=uuid4(),
        code="2026-2027",
        display_name="2026-2027学年",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 7, 10),
    )
    model = AcademicYearCreate(**base)
    assert model.start_date < model.end_date
    for start, end in [
        (date(2026, 9, 1), date(2026, 9, 1)),
        (date(2026, 9, 2), date(2026, 9, 1)),
    ]:
        with pytest.raises(ValidationError):
            AcademicYearCreate(**{**base, "start_date": start, "end_date": end})


def test_academic_term_schema_rejects_non_increasing_dates_and_bad_term_number():
    base = dict(
        request_id=uuid4(),
        academic_year_id=uuid4(),
        term_no=1,
        code="T1",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 1, 20),
    )
    AcademicTermCreate(**base)
    with pytest.raises(ValidationError):
        AcademicTermCreate(**{**base, "end_date": date(2026, 9, 1)})
    for term_no in (0, -1, 5):
        with pytest.raises(ValidationError):
            AcademicTermCreate(**{**base, "term_no": term_no})
    for term_no in (1, 4):
        AcademicTermCreate(**{**base, "term_no": term_no})


@pytest.mark.parametrize("intake_year", [1999, 2101])
def test_grade_cohort_intake_year_range_is_bounded(intake_year):
    with pytest.raises(ValidationError):
        GradeCohortCreate(
            request_id=uuid4(), code="X", display_name="合成届别", intake_year=intake_year
        )


def test_grade_cohort_intake_year_boundaries_are_accepted():
    for intake_year in (2000, 2100):
        cohort = GradeCohortCreate(
            request_id=uuid4(), code="X", display_name="合成届别", intake_year=intake_year
        )
        assert cohort.intake_year == intake_year


def test_create_schemas_forbid_extra_fields_and_require_request_id():
    for schema, payload in CREATE_SCHEMAS:
        schema(**payload)
        with pytest.raises(ValidationError):
            schema(**payload, unexpected_authority=True)
        without_request_id = {k: v for k, v in payload.items() if k != "request_id"}
        with pytest.raises(ValidationError):
            schema(**without_request_id)


def test_transfer_and_confirm_requests_are_strict_and_require_request_id():
    transfer = StudentTransferRequest(
        request_id=uuid4(),
        student_id=uuid4(),
        to_administrative_class_id=uuid4(),
        effective_from=date(2026, 10, 8),
    )
    assert transfer.effective_from == date(2026, 10, 8)
    with pytest.raises(ValidationError):
        StudentTransferRequest(
            student_id=uuid4(),
            to_administrative_class_id=uuid4(),
            effective_from=date(2026, 10, 8),
        )
    with pytest.raises(ValidationError):
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=uuid4(),
            to_administrative_class_id=uuid4(),
            effective_from=date(2026, 10, 8),
            is_admin=True,
        )
    request_id = uuid4()
    assert MutationRequest(request_id=request_id).request_id == request_id
    assert StudentImportConfirmRequest(request_id=request_id, expected_input_sha256="a" * 64)
    for invalid in ("a" * 63, "a" * 65):
        with pytest.raises(ValidationError):
            StudentImportConfirmRequest(request_id=request_id, expected_input_sha256=invalid)
    with pytest.raises(ValidationError):
        StudentImportConfirmRequest(request_id=request_id, unexpected=True)


def test_new_organization_actions_are_installation_scoped_only():
    catalog = security_service.CATALOG
    for action in NEW_ACTIONS:
        assert catalog[action] == frozenset({"installation"})
    # A single fixed installation scope: no school/class/assignment UUID scope for B-1.1b.
    assert org_operations.INSTALLATION_ACTIONS == NEW_ACTIONS
    assert NEW_ACTIONS.isdisjoint(
        {"security.grants.manage", "security.protected.create", "security.classification.inspect"}
    )


def test_grants_manage_catalog_entry_was_not_extended_by_organization_actions():
    # Delegation stays a separate permission; no organization code is folded into it.
    assert security_service.CATALOG["security.grants.manage"] == frozenset({"installation"})
    assert security_service.CATALOG["security.protected.create"] == frozenset({"installation"})
    assert security_service.CATALOG["security.classification.inspect"] == frozenset(
        {"installation", "assignment"}
    )
    # Exactly the B-1.1b additive change: four installation-scoped codes.
    assert set(security_service.CATALOG) == {
        "security.grants.manage",
        "security.protected.create",
        "security.classification.inspect",
        *NEW_ACTIONS,
        "scan.batch.create",
        "scan.batch.read",
        "scan.file.upload",
        "scan.file.read",
        "exam.script.read",
        "exam.script.edit",
        "exam.script.confirm",
        "exam.identity.verify",
        "exam.parse.start",
        "exam.parse.read",
        "exam.mark.read",
        "exam.answer.review",
        "exam.score.request",
        "exam.score.review",
        "exam.task.manage",
        "exam.task.work",
        "paper.create",
        "paper.read",
        "paper.edit",
        "paper.lock",
        "paper.use",
        "exam.roster.read",
        "exam.roster.manage",
        "exam.roster.freeze",
        "exam.roster.correct",
        "exam.event.create",
        "exam.event.read",
        "exam.event.manage",
        "exam.session.read",
        "exam.session.manage",
        "exam.session.paper.bind",
    }


def test_organization_http_policies_map_to_the_fixed_permission_catalog():
    policies = identity_dependencies.ORGANIZATION_POLICIES
    paths = identity_dependencies.POLICY_PATHS
    expected = {
        "school_create_year": "organization.manage",
        "school_create_term": "organization.manage",
        "school_create_cohort": "organization.manage",
        "school_create_subject": "organization.manage",
        "school_create_class": "organization.manage",
        "school_classes": "organization.read",
        "school_import_template": "student.import.preview",
        "school_import_rows": "organization.read",

        "list_academic_years": "organization.read",
        "list_academic_terms": "organization.read",
        "list_grade_cohorts": "organization.read",
        "list_subjects": "organization.read",
        "list_administrative_classes": "organization.read",
        "list_students": "organization.read",
        "list_student_enrollments": "organization.read",
        "list_student_imports": "organization.read",
        "get_student_import": "organization.read",
        "preview_student_import": "student.import.preview",
        "confirm_student_import": "student.import.confirm",
    }
    assert policies == expected
    assert set(policies) <= set(identity_dependencies.POLICIES)
    assert set(policies) <= set(paths)
    assert set(policies.values()) <= NEW_ACTIONS
    for name in policies:
        assert identity_dependencies.POLICIES[name] in ({"GET"}, {"POST"})
        assert paths[name].startswith("/organization/")


def test_legacy_admin_and_route_newcomers_cannot_obtain_organization_actions():
    # The legacy shortcut set in authorize_request never contains an organization policy.
    legacy_shortcuts = {
        *identity_dependencies.ADMIN_POLICIES,
        "get_current_user",
        "create_assignment",
        "list_assignments",
        "grant_assignment_membership",
        "revoke_assignment_membership",
        "inspect_grade_review",
        "list_grade_reviews",
        "record_teacher_grade_review",
    }
    assert set(identity_dependencies.ORGANIZATION_POLICIES).isdisjoint(legacy_shortcuts)
    # The organization authorization path itself never consults User.is_admin.
    source = inspect.getsource(org_operations.authorize)
    assert "is_admin" not in source
    assert "require_permission" in source
    # Every organization route is guarded by the exact fixed route-name/method policy map.
    for name in identity_dependencies.ORGANIZATION_POLICIES:
        assert identity_dependencies.POLICIES[name]
