"""Pydantic response schemas for student submissions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SubmissionFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


class ParsingTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: Literal["pending"]
    created_at: datetime
    updated_at: datetime


class SubmissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assignment_id: UUID
    student_ref: str
    status: Literal["submitted"]
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime
    file: SubmissionFileRead
    parsing_task: ParsingTaskRead
