"""Bound total multipart request bytes before Starlette spools uploaded files."""

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
    ) -> None:
        self.app = app
        self.max_request_size_bytes = max_upload_size_bytes + multipart_overhead_bytes
        self.upload_path_prefix = f"{api_v1_prefix.rstrip('/')}/assignments/"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_submission_upload(scope):
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
                )
                await response(scope, receive, send)
                return
            if content_length < 0:
                response = JSONResponse(
                    status_code=400,
                    content={"detail": "请求内容长度无效"},
                )
                await response(scope, receive, send)
                return
            if content_length > self.max_request_size_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "文件超过大小限制"},
                )
                await response(scope, receive, send)
                return

        request_bytes = 0

        async def receive_with_limit() -> Message:
            nonlocal request_bytes
            message = await receive()
            if message["type"] == "http.request":
                request_bytes += len(message.get("body", b""))
                if request_bytes > self.max_request_size_bytes:
                    raise UploadTooLargeError("文件超过大小限制")
            return message

        await self.app(scope, receive_with_limit, send)

    def _is_submission_upload(self, scope: Scope) -> bool:
        path = scope.get("path", "")
        return (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and path.startswith(self.upload_path_prefix)
            and path.endswith("/submissions")
        )
