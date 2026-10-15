"""Server-issued in-process Principal. Token roles are deliberately excluded."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: UUID
    issuer: str
    subject: str
    expires_at: datetime
    is_admin: bool = False  # Loaded only from the local User record.


class AccessError(Exception):
    def __init__(self, code: str, status_code: int = 403):
        self.code = code
        self.status_code = status_code
        super().__init__(code)
