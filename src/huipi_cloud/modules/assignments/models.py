"""SQLAlchemy models for teacher-managed assignments."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from huipi_cloud.infrastructure.database.base import Base, utc_now


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published')",
            name="ck_assignments_status",
        ),
        CheckConstraint("length(trim(title)) > 0", name="ck_assignments_title_not_blank"),
        CheckConstraint("length(trim(subject)) > 0", name="ck_assignments_subject_not_blank"),
        CheckConstraint(
            "length(trim(grade_level)) > 0",
            name="ck_assignments_grade_level_not_blank",
        ),
        Index("ix_assignments_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(100), nullable=False)
    grade_level: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    questions: Mapped[list["Question"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Question.question_number",
    )


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "question_number",
            name="uq_questions_assignment_number",
        ),
        CheckConstraint("question_number > 0", name="ck_questions_number_positive"),
        CheckConstraint(
            "question_type IN "
            "('single_choice', 'multiple_choice', 'fill_blank', 'short_answer', 'essay')",
            name="ck_questions_type",
        ),
        CheckConstraint("length(trim(stem)) > 0", name="ck_questions_stem_not_blank"),
        CheckConstraint("max_score > 0", name="ck_questions_max_score_positive"),
        Index("ix_questions_assignment_id", "assignment_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assignment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    question_type: Mapped[str] = mapped_column(String(30), nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    max_score: Mapped[Decimal] = mapped_column(Numeric(8, 2, asdecimal=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    assignment: Mapped[Assignment] = relationship(back_populates="questions")
    answer_key: Mapped["AnswerKey | None"] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    rubric_criteria: Mapped[list["RubricCriterion"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RubricCriterion.sort_order",
    )


class AnswerKey(Base):
    __tablename__ = "answer_keys"
    __table_args__ = (
        UniqueConstraint("question_id", name="uq_answer_keys_question_id"),
        CheckConstraint("length(trim(answer_content)) > 0", name="ck_answer_keys_content"),
        CheckConstraint(
            "source IN ('teacher', 'model_generated')",
            name="ck_answer_keys_source",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    answer_content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="teacher",
        server_default="teacher",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    question: Mapped[Question] = relationship(back_populates="answer_key")


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"
    __table_args__ = (
        UniqueConstraint(
            "question_id",
            "sort_order",
            name="uq_rubric_criteria_question_order",
        ),
        CheckConstraint("length(trim(description)) > 0", name="ck_rubric_criteria_description"),
        CheckConstraint("points > 0", name="ck_rubric_criteria_points_positive"),
        CheckConstraint("sort_order > 0", name="ck_rubric_criteria_order_positive"),
        Index("ix_rubric_criteria_question_id", "question_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    points: Mapped[Decimal] = mapped_column(Numeric(8, 2, asdecimal=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    question: Mapped[Question] = relationship(back_populates="rubric_criteria")
