"""Validation and response contracts for the single-school organization slice."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ImportMode = Literal["student_only", "enrollment_only", "student_with_enrollment"]
AcademicYearStatus = Literal["planned", "active", "closed"]
ClassStatus = Literal["active", "inactive"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MutationRequest(StrictModel):
    request_id: UUID


class AcademicYearCreate(MutationRequest):
    code: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=100)
    start_date: date
    end_date: date
    status: AcademicYearStatus = "planned"

    @model_validator(mode="after")
    def validate_range(self) -> "AcademicYearCreate":
        if self.end_date <= self.start_date:
            raise ValueError("academic_year_date_order")
        return self


class AcademicTermCreate(MutationRequest):
    academic_year_id: UUID
    term_no: int = Field(ge=1, le=4)
    code: str = Field(min_length=1, max_length=32)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_range(self) -> "AcademicTermCreate":
        if self.end_date <= self.start_date:
            raise ValueError("academic_term_date_order")
        return self


class GradeCohortCreate(MutationRequest):
    code: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=100)
    intake_year: int = Field(ge=2000, le=2100)


class SubjectCreate(MutationRequest):
    code: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=100)


class AdministrativeClassCreate(MutationRequest):
    grade_cohort_id: UUID
    code: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=100)


class AcademicYearSummary(StrictModel):
    id: UUID
    code: str
    display_name: str
    start_date: date
    end_date: date
    status: AcademicYearStatus


class AcademicTermSummary(StrictModel):
    id: UUID
    academic_year_id: UUID
    term_no: int
    code: str
    start_date: date
    end_date: date


class GradeCohortSummary(StrictModel):
    id: UUID
    code: str
    display_name: str
    intake_year: int


class SubjectSummary(StrictModel):
    id: UUID
    code: str
    display_name: str
    is_active: bool


class AdministrativeClassSummary(StrictModel):
    id: UUID
    grade_cohort_id: UUID
    code: str
    display_name: str
    status: ClassStatus


class StudentSummary(StrictModel):
    id: UUID
    school_student_code: str
    name: str
    is_active: bool


class ClassEnrollmentSummary(StrictModel):
    id: UUID
    student_id: UUID
    administrative_class_id: UUID
    administrative_class_code: str | None = None
    valid_from: date
    valid_to: date | None
    source: Literal["manual", "student_import"]


class Page(StrictModel):
    limit: int
    offset: int
    has_next: bool
    next_offset: int | None


class AcademicYearPage(Page):
    records: list[AcademicYearSummary]


class AcademicTermPage(Page):
    records: list[AcademicTermSummary]


class GradeCohortPage(Page):
    records: list[GradeCohortSummary]


class SubjectPage(Page):
    records: list[SubjectSummary]


class AdministrativeClassPage(Page):
    records: list[AdministrativeClassSummary]


class StudentPage(Page):
    records: list[StudentSummary]


class ClassEnrollmentPage(Page):
    records: list[ClassEnrollmentSummary]


class MutationResult(StrictModel):
    id: UUID
    action: str
    replayed: bool = False


class StudentTransferRequest(MutationRequest):
    student_id: UUID
    to_administrative_class_id: UUID
    effective_from: date


class StudentTransferResult(StrictModel):
    student_id: UUID
    from_administrative_class_id: UUID | None
    to_administrative_class_id: UUID
    effective_from: date
    replayed: bool = False


class ImportErrorEntry(StrictModel):
    row_number: int
    field: str | None
    code: str
    message: str


class StudentImportPreviewResult(StrictModel):
    batch_id: UUID
    mode: ImportMode
    status: Literal["previewed", "confirmed"]
    input_sha256: str
    file_size_bytes: int
    row_count: int
    academic_year_id: UUID
    counts: dict[str, int]
    can_confirm: bool
    errors: list[ImportErrorEntry]
    warnings: list[ImportErrorEntry]
    truncated: bool
    created_at: datetime


class StudentImportConfirmRequest(StrictModel):
    request_id: UUID
    expected_input_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class StudentImportConfirmResult(StrictModel):
    batch_id: UUID
    status: Literal["confirmed"]
    created_students: int
    created_enrollments: int
    unchanged_rows: int
    rejected_rows: int
    confirmed_at: datetime
    replayed: bool = False


class StudentImportBatchSummary(StrictModel):
    batch_id: UUID
    mode: ImportMode
    status: Literal["previewed", "confirmed"]
    input_sha256: str
    row_count: int
    academic_year_id: UUID
    counts: dict[str, int]
    created_at: datetime
    confirmed_at: datetime | None


class StudentImportBatchPage(Page):
    records: list[StudentImportBatchSummary]
