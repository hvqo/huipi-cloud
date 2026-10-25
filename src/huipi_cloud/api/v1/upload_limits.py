"""Bound total multipart request bytes before Starlette spools uploaded files."""

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from huipi_cloud.modules.submissions.errors import UploadTooLargeError

MULTIPART_OVERHEAD_LIMIT = 64 * 1024


class UploadRequestLimitMiddleware:
    """Cap upload request bodies, including multipart headers and form fields."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_upload_size_bytes: int,
        api_v1_prefix: str,
        multipart_overhead_bytes: int = MULTIPART_OVERHEAD_LIMIT,
        student_import_max_bytes: int | None = None,
        scan_max_file_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.scan_request_limit = scan_max_file_bytes + multipart_overhead_bytes
        self.active_script_operations = 0
        self.active_scan_uploads = 0
        self.active_scan_downloads = 0
        self.app = app
        self.multipart_overhead_bytes = multipart_overhead_bytes
        self.max_request_size_bytes = max_upload_size_bytes + multipart_overhead_bytes
        self.upload_path_prefix = f"{api_v1_prefix.rstrip('/')}/assignments/"
        self.api_v1_prefix = api_v1_prefix.rstrip("/")
        self.student_import_request_limit = (
            None
            if student_import_max_bytes is None
            else student_import_max_bytes + multipart_overhead_bytes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        scan_upload = (
            scope.get("method") == "POST"
            and path.startswith(f"{self.api_v1_prefix}/scan-batches/")
            and path.endswith("/files")
        )
        scan_download = scope.get("method") == "GET" and (
            (path.startswith(f"{self.api_v1_prefix}/scan-files/") and path.endswith("/original"))
            or (path.startswith(f"{self.api_v1_prefix}/scan-pages/") and path.endswith("/preview"))
        )
        script_operation = (
            scope.get("method") == "POST"
            and (
                (
                    path.startswith(f"{self.api_v1_prefix}/exam-sessions/")
                    and path.endswith("/answer-scripts")
                )
                or (
                    path.startswith(f"{self.api_v1_prefix}/answer-scripts/")
                    and (path.endswith("/versions") or path.endswith("/parsing"))
                )
            )
        ) or (
            scope.get("method") == "GET"
            and path.startswith(f"{self.api_v1_prefix}/answer-scripts/")
            and path.endswith("/manifest")
        )
        script_operation = script_operation or (
            scope.get("method") == "GET"
            and path.startswith(f"{self.api_v1_prefix}/exam-parsing-jobs/")
            and "/pages/" in path
        )
        script_operation = script_operation or (
            scope.get("method") in {"GET", "POST"}
            and (
                path.startswith(f"{self.api_v1_prefix}/exam-grading-workspaces/")
                or (
                    path.startswith(f"{self.api_v1_prefix}/exam-parsing-jobs/")
                    and path.endswith("/grading-workspace")
                )
            )
        )
        if scan_upload or scan_download or script_operation:
            attribute = (
                "active_script_operations"
                if script_operation
                else "active_scan_uploads"
                if scan_upload
                else "active_scan_downloads"
            )
            maximum = 2 if scan_upload or script_operation else 4
            if getattr(self, attribute) >= maximum:
                await JSONResponse(
                    status_code=429,
                    content={"detail": "scan_busy"},
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            setattr(self, attribute, getattr(self, attribute) + 1)
            started = False

            async def safe_send(message):
                nonlocal started
                if message["type"] == "http.response.start":
                    started = True
                await send(message)

            try:
                async with asyncio.timeout(100 if script_operation else 150 if scan_upload else 60):
                    await self._limited(scope, receive, safe_send)
            except TimeoutError:
                if started:
                    raise
                await JSONResponse(
                    status_code=503,
                    content={"detail": "scan_request_timeout"},
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
            finally:
                setattr(self, attribute, getattr(self, attribute) - 1)
            return
        await self._limited(scope, receive, send)

    async def _limited(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._request_limit(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return

        content_length_header = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        if content_length_header is not None:
            try:
                content_length = int(content_length_header)
            except ValueError:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "请求内容长度无效"},
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            if content_length < 0:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "请求内容长度无效"},
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return
            if content_length > limit:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "文件超过大小限制"},
                    headers={"Cache-Control": "no-store"},
                )
                await response(scope, receive, send)
                return

        request_bytes = 0

        async def receive_with_limit() -> Message:
            nonlocal request_bytes
            message = await receive()
            if message["type"] == "http.request":
                request_bytes += len(message.get("body", b""))
                if request_bytes > limit:
                    raise UploadTooLargeError("文件超过大小限制")
            return message

        await self.app(scope, receive_with_limit, send)

    def _request_limit(self, scope: Scope) -> int | None:
        """Return the request-body cap for known upload endpoints, else ``None``."""
        path = scope.get("path", "")
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return None
        if (
            path.startswith(f"{self.api_v1_prefix}/answer-scripts/")
            and (path.endswith("/versions") or path.endswith("/parsing"))
        ) or (
            path.startswith(f"{self.api_v1_prefix}/exam-sessions/")
            and path.endswith("/answer-scripts")
        ):
            return 64 * 1024
        if path.startswith(f"{self.api_v1_prefix}/scan-batches/") and path.endswith("/files"):
            return self.scan_request_limit
        if path.startswith(self.upload_path_prefix) and path.endswith("/submissions"):
            return self.max_request_size_bytes
        if (
            self.student_import_request_limit is not None
            and path == f"{self.api_v1_prefix}/organization/student-imports"
        ):
            return self.student_import_request_limit
        return None
