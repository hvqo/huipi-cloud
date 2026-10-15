"""Trusted Principal → fixed Agent scope, for in-process callers; no HTTP chat route."""

import asyncio

from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.modules.copilot.protocol import CopilotError
from huipi_cloud.modules.copilot.tools import BusinessTools
from huipi_cloud.modules.identity import service
from huipi_cloud.modules.identity.protocol import AccessError


class AuthorizedBusinessTools(BusinessTools):
    @classmethod
    async def from_bearer(cls, token, config, session_factory, assignment_id, **kwargs):
        identity = await TokenVerifier(config).verify(token)
        if session_factory is None:
            raise AccessError("authorization_unavailable", 503)
        try:
            async with asyncio.timeout(5), session_factory() as session, session.begin():
                principal = await service.resolve_principal(session, identity)
                await service.require_assignment(session, principal, assignment_id)
        except (SQLAlchemyError, OSError, TimeoutError):
            raise AccessError("authorization_unavailable", 503) from None
        tools = cls(session_factory, assignment_id, **kwargs)
        tools.principal = principal
        return tools

    async def _authorize(self):
        try:
            async with asyncio.timeout(5), self.session_factory() as session, session.begin():
                await service.require_assignment(session, self.principal, self.assignment_id)
        except AccessError as error:
            raise CopilotError(error.code) from None
        except (SQLAlchemyError, OSError, TimeoutError):
            raise CopilotError("authorization_unavailable") from None

    async def execute(self, name, args):
        await self._authorize()
        result = await super().execute(name, args)
        await self._authorize()  # Do not release retrieved evidence after a mid-tool revocation.
        return result

    async def validate_citations(self, hits):
        await self._authorize()
        await super().validate_citations(hits)
        await self._authorize()
