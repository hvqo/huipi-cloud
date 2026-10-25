"""Class access narrows one existing Grant; no aggregate scores are persisted."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class ExamResultClassAccess(Base):
    __tablename__ = "exam_result_class_access"
    __table_args__ = (
        Index(
            "uq_result_class_access_active",
            "grant_id",
            "exam_session_id",
            "administrative_class_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    grant_id: Mapped[UUID] = mapped_column(ForeignKey("security_grants.id", ondelete="RESTRICT"))
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    administrative_class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    export_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
