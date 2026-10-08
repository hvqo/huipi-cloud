"""Real MinerU 4.x executor with bounded input, process, and artifact handling."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.storage.s3 import (
    S3ObjectStorage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.modules.parsing.errors import (
    PermanentParsingError,
    RetryableParsingError,
    WorkerFatalParsingError,
)
from huipi_cloud.modules.parsing.executor import ParsedArtifactResult, ParsingInput

logger = logging.getLogger(__name__)
_CHUNK_SIZE = 1024 * 1024
_LOG_CAPTURE_LIMIT = 16 * 1024
_SUPPORTED_FORMATS = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}
_TOP_LEVEL_FILES = {
    "markdown.md",
    "middle_json.json",
    "structured_content.json",
}
_IMAGE_SUFFIX_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class _Member:
    name: str
    path: Path
    size_bytes: int
    sha256: str
    content_type: str


@dataclass(frozen=True)
class _ValidatedResult:
    parser_version: str
    page_count: int
    markdown: bytes
    middle_json: bytes
    structured_content: bytes
    schema_name: str
    schema_version: str
    assets: tuple[_Member, ...]


class MinerUParserExecutor:
    """Execute the locally configured MinerU Basic/ONNX parser in a child group."""

    def __init__(
        self,
        storage: S3ObjectStorage,
        *,
        executable: str,
        mineru_home: Path,
        tier: str,
        max_input_bytes: int,
        max_pdf_pages: int,
        max_output_bytes: int,
        max_text_bytes: int,
        max_archive_members: int,
        execution_timeout_seconds: float,
        cancellation_grace_seconds: float,
    ) -> None:
        if sys.platform != "linux":
            raise ValueError("受监督的MinerU解析目前需要Linux进程组和PR_SET_PDEATHSIG")
        if tier != "basic":
            raise ValueError("当前只启用离线Basic档位；Standard档位需要单独评估推理后端")
        resolved = shutil.which(executable) if not Path(executable).is_absolute() else executable
        if not resolved or not Path(resolved).is_file():
            raise ValueError("MINERU_EXECUTABLE必须指向已安装的mineru-kit程序")
        if not mineru_home.is_dir():
            raise ValueError("MINERU_HOME必须指向独立的MinerU运行目录")

        self.storage = storage
        self.executable = str(Path(resolved).resolve())
        self.mineru_home = mineru_home.resolve()
        self.tier = tier
        self.max_input_bytes = max_input_bytes
        self.max_pdf_pages = max_pdf_pages
        self.max_output_bytes = max_output_bytes
        self.max_text_bytes = max_text_bytes
        self.max_archive_members = max_archive_members
        self.execution_timeout_seconds = execution_timeout_seconds
        self.cancellation_grace_seconds = cancellation_grace_seconds
        self._verify_local_installation()

    async def execute(self, task: ParsingInput) -> ParsedArtifactResult:
        """Download, verify, parse, validate, and persist an immutable run bundle."""
        extension = _SUPPORTED_FORMATS.get(task.content_type)
        if extension is None:
            raise PermanentParsingError("unsupported_file_type")
        if task.size_bytes < 1 or task.size_bytes > self.max_input_bytes:
            raise PermanentParsingError("invalid_input")

        with tempfile.TemporaryDirectory(prefix="huipi-mineru-") as temporary_root:
            workdir = Path(temporary_root)
            os.chmod(workdir, 0o700)
            input_path = workdir / f"source{extension}"
            input_size, input_sha256 = await self._download_original(task, input_path)
            if input_size != task.size_bytes or input_sha256 != task.sha256:
                raise PermanentParsingError("corrupt_input")

            archive_path = workdir / "mineru-result.zip"
            await self._run_mineru(task, input_path, archive_path, workdir)
            validated = await asyncio.to_thread(
                _validate_archive,
                archive_path,
                workdir,
                self.tier,
                self.max_output_bytes,
                self.max_text_bytes,
                self.max_archive_members,
            )
            return await self._persist_result(task, input_sha256, archive_path, validated)

    async def _download_original(self, task: ParsingInput, destination: Path) -> tuple[int, str]:
        try:
            remote = await self.storage.download(task.object_key)
        except StorageObjectNotFoundError as error:
            raise PermanentParsingError("missing_source") from error
        except StorageUnavailableError as error:
            raise RetryableParsingError("object_store_unavailable") from error

        total = 0
        digest = hashlib.sha256()
        try:
            with destination.open("wb") as output:
                async for chunk in remote.chunks(_CHUNK_SIZE):
                    total += len(chunk)
                    if total > self.max_input_bytes or total > task.size_bytes:
                        raise PermanentParsingError("corrupt_input")
                    digest.update(chunk)
                    output.write(chunk)
        except StorageUnavailableError as error:
            raise RetryableParsingError("object_store_unavailable") from error
        return total, digest.hexdigest()

    async def _run_mineru(
        self,
        task: ParsingInput,
        input_path: Path,
        archive_path: Path,
        workdir: Path,
    ) -> None:
        command = [
            self.executable,
            "parse",
            str(input_path),
            "--output",
            str(archive_path),
            "--format",
            "zip",
            "--tier",
            self.tier,
        ]
        preflight: list[str] = []
        if task.content_type == "application/pdf":
            preflight = [
                "--pdf-path",
                str(input_path),
                "--max-pdf-pages",
                str(self.max_pdf_pages),
            ]
            command.extend(["--pages", "all"])
        child_command = [
            sys.executable,
            "-m",
            "huipi_cloud.infrastructure.parsing.child_guard",
            *preflight,
            "--",
            *command,
        ]
        child_environment = self._child_environment(workdir)
        try:
            process = await asyncio.create_subprocess_exec(
                *child_command,
                cwd=workdir,
                env=child_environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            raise RetryableParsingError("parser_unavailable") from error

        assert process.stdout is not None
        reader = asyncio.create_task(_drain_bounded_log(process.stdout))
        output_guard = asyncio.create_task(
            _watch_directory_size(workdir, self.max_output_bytes)
        )
        wait_task = asyncio.create_task(process.wait())
        reason: str | None = None
        try:
            deadline = asyncio.get_running_loop().time() + self.execution_timeout_seconds
            while not wait_task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    reason = "timeout"
                    await _terminate_process_group(
                        process,
                        self.cancellation_grace_seconds,
                    )
                    break
                done, _ = await asyncio.wait(
                    {wait_task, output_guard},
                    timeout=min(0.25, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if output_guard in done:
                    if output_guard.result():
                        reason = "output_limit"
                        await _terminate_process_group(
                            process,
                            self.cancellation_grace_seconds,
                        )
                        break
                    output_guard = asyncio.create_task(
                        _watch_directory_size(workdir, self.max_output_bytes)
                    )
            return_code = await wait_task
            await reader
            if _process_group_exists(process.pid):
                await _terminate_process_group(
                    process,
                    self.cancellation_grace_seconds,
                )
                raise RetryableParsingError("parser_unavailable")
        except asyncio.CancelledError:
            await _run_cleanup(
                _terminate_process_group(
                    process,
                    self.cancellation_grace_seconds,
                )
            )
            raise
        finally:
            if not output_guard.done():
                output_guard.cancel()
            await asyncio.gather(output_guard, return_exceptions=True)
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            if process.returncode is None or _process_group_exists(process.pid):
                await _run_cleanup(
                    _terminate_process_group(
                        process,
                        self.cancellation_grace_seconds,
                    )
                )

        if reason == "timeout":
            raise RetryableParsingError("execution_timeout")
        if reason == "output_limit":
            raise PermanentParsingError("invalid_result")
        if return_code == 70:
            raise PermanentParsingError("corrupt_input")
        if return_code == 71:
            raise PermanentParsingError("page_limit_exceeded")
        if return_code != 0:
            raise RetryableParsingError("parser_unavailable")

    async def _persist_result(
        self,
        task: ParsingInput,
        original_sha256: str,
        archive_path: Path,
        result: _ValidatedResult,
    ) -> ParsedArtifactResult:
        artifact_id = uuid4()
        prefix = self.storage.object_key_prefix
        run_prefix = "/".join(
            part
            for part in (
                prefix,
                "parsed",
                str(task.submission_id),
                "tasks",
                str(task.task_id),
                "runs",
                str(artifact_id),
            )
            if part
        )
        archive_key = f"{run_prefix}/result.zip"
        markdown_key = f"{run_prefix}/markdown.md"
        middle_json_key = f"{run_prefix}/middle_json.json"
        structured_key = f"{run_prefix}/structured_content.json"
        manifest_key = f"{run_prefix}/assets/manifest.json"

        archive_digest, archive_size = await self._upload_path(
            archive_path,
            archive_key,
            "application/zip",
        )
        markdown_digest, markdown_size = await self._upload_bytes(
            result.markdown,
            markdown_key,
            "text/markdown; charset=utf-8",
        )
        middle_digest, middle_size = await self._upload_bytes(
            result.middle_json,
            middle_json_key,
            "application/json",
        )
        structured_digest, structured_size = await self._upload_bytes(
            result.structured_content,
            structured_key,
            "application/json",
        )

        asset_records: list[dict[str, object]] = []
        for member in result.assets:
            object_key = f"{run_prefix}/assets/{member.sha256}"
            await self._upload_path(member.path, object_key, member.content_type)
            asset_records.append(
                {
                    "path": member.name,
                    "object_key": object_key,
                    "sha256": member.sha256,
                    "size_bytes": member.size_bytes,
                    "content_type": member.content_type,
                }
            )
        manifest_bytes = json.dumps(
            {"schema": "huipi.parsed-assets", "version": "1", "assets": asset_records},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(manifest_bytes) > self.max_text_bytes:
            raise PermanentParsingError("invalid_result")
        manifest_digest, manifest_size = await self._upload_bytes(
            manifest_bytes,
            manifest_key,
            "application/json",
        )

        for key, expected_size in (
            (archive_key, archive_size),
            (markdown_key, markdown_size),
            (middle_json_key, middle_size),
            (structured_key, structured_size),
            (manifest_key, manifest_size),
        ):
            metadata = await self.storage.head_object(key)
            if metadata.get("content_length") != expected_size:
                raise RetryableParsingError("object_store_unavailable")
        for member, record in zip(result.assets, asset_records, strict=True):
            metadata = await self.storage.head_object(str(record["object_key"]))
            if metadata.get("content_length") != member.size_bytes:
                raise RetryableParsingError("object_store_unavailable")

        return ParsedArtifactResult(
            artifact_id=artifact_id,
            task_id=task.task_id,
            submission_id=task.submission_id,
            bucket=self.storage.bucket,
            original_sha256=original_sha256,
            parser_name="MinerU",
            parser_version=result.parser_version,
            tier=self.tier,
            schema_name=result.schema_name,
            schema_version=result.schema_version,
            page_count=result.page_count,
            archive_key=archive_key,
            archive_sha256=archive_digest,
            archive_size_bytes=archive_size,
            markdown_key=markdown_key,
            markdown_sha256=markdown_digest,
            markdown_size_bytes=markdown_size,
            middle_json_key=middle_json_key,
            middle_json_sha256=middle_digest,
            middle_json_size_bytes=middle_size,
            structured_content_key=structured_key,
            structured_content_sha256=structured_digest,
            structured_content_size_bytes=structured_size,
            assets_manifest_key=manifest_key,
            assets_manifest_sha256=manifest_digest,
            assets_manifest_size_bytes=manifest_size,
            asset_count=len(asset_records),
        )

    async def _upload_path(self, path: Path, object_key: str, content_type: str) -> tuple[str, int]:
        digest, size_bytes = await asyncio.to_thread(_sha256_file, path)
        try:
            with path.open("rb") as fileobj:
                await self.storage.upload_fileobj(fileobj, object_key, content_type)
        except StorageUnavailableError as error:
            raise RetryableParsingError("object_store_unavailable") from error
        return digest, size_bytes

    async def _upload_bytes(
        self,
        payload: bytes,
        object_key: str,
        content_type: str,
    ) -> tuple[str, int]:
        from io import BytesIO

        try:
            await self.storage.upload_fileobj(BytesIO(payload), object_key, content_type)
        except StorageUnavailableError as error:
            raise RetryableParsingError("object_store_unavailable") from error
        return hashlib.sha256(payload).hexdigest(), len(payload)

    def _child_environment(self, workdir: Path) -> dict[str, str]:
        """Pass only parser-safe environment values; never forward app secrets."""
        environment = {
            "PATH": os.defpath,
            "HOME": str(workdir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "OMP_NUM_THREADS": "4",
            "MINERU_HOME": str(self.mineru_home),
            "MINERU_MODEL_SOURCE": "local",
            "MINERU_MODEL_SMALL_BACKEND": "onnx",
            "MINERU_TABLE_DEVICE": "cpu",
            "MINERU_INTRA_OP_NUM_THREADS": "4",
            "MINERU_INTER_OP_NUM_THREADS": "1",
            "TMPDIR": str(workdir),
            "TMP": str(workdir),
            "TEMP": str(workdir),
            "CUDA_VISIBLE_DEVICES": "",
        }
        return environment

    def _verify_local_installation(self) -> None:
        """Fail before claiming tasks if the configured CLI or local model is absent."""
        environment = self._child_environment(self.mineru_home)
        try:
            version = subprocess.run(
                [self.executable, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
                env=environment,
            )
            version_text = f"{version.stdout}\n{version.stderr}"
            match = re.search(r"(?:版本[:：]\s*)?(\d+)\.(\d+)\.(\d+)", version_text)
            if match is None or int(match.group(1)) != 4:
                raise ValueError("配置的mineru-kit版本必须是4.x")
            subprocess.run(
                [
                    self.executable,
                    "models",
                    "verify",
                    "--tier",
                    self.tier,
                    "--small-backend",
                    "onnx",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
                env=environment,
            )
        except subprocess.CalledProcessError:
            raise ValueError("MinerU 4.x或本地Basic/ONNX模型未通过启动校验") from None
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("MinerU 4.x或本地Basic/ONNX模型未通过启动校验") from error


def build_mineru_executor(config: Settings | None = None) -> MinerUParserExecutor:
    """Build the production parser from explicit local-only settings."""
    selected = config or settings
    if not selected.mineru_home:
        raise ValueError("必须配置独立的MINERU_HOME")
    if not selected.mineru_executable:
        raise ValueError("必须配置MINERU_EXECUTABLE")
    if not all(
        (
            selected.minio_endpoint_url,
            selected.minio_access_key,
            selected.minio_secret_key,
        )
    ):
        raise ValueError("对象存储必须完成配置")
    storage = S3ObjectStorage(
        endpoint_url=selected.minio_endpoint_url,
        access_key=selected.minio_access_key,
        secret_key=selected.minio_secret_key,
        bucket=selected.minio_bucket,
        object_key_prefix=selected.minio_object_key_prefix,
    )
    return MinerUParserExecutor(
        storage,
        executable=selected.mineru_executable,
        mineru_home=Path(selected.mineru_home).expanduser(),
        tier=selected.mineru_tier,
        max_input_bytes=selected.max_upload_size_bytes,
        max_pdf_pages=selected.mineru_max_pdf_pages,
        max_output_bytes=selected.mineru_max_output_bytes,
        max_text_bytes=selected.mineru_max_text_bytes,
        max_archive_members=selected.mineru_max_archive_members,
        execution_timeout_seconds=selected.parsing_execution_timeout_seconds,
        cancellation_grace_seconds=selected.parsing_cancel_grace_seconds,
    )


async def _drain_bounded_log(reader: asyncio.StreamReader) -> bytes:
    """Drain all child output while retaining at most a small non-public buffer."""
    captured = bytearray()
    while chunk := await reader.read(16 * 1024):
        remaining = _LOG_CAPTURE_LIMIT - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


async def _watch_directory_size(directory: Path, maximum: int) -> bool:
    """Return when output exceeds its cap; disk traversal runs off the event loop."""
    while True:
        if await asyncio.to_thread(_directory_size, directory, maximum) > maximum:
            return True
        await asyncio.sleep(0.25)


def _directory_size(directory: Path, stop_after: int) -> int:
    total = 0
    for root, directories, files in os.walk(directory, followlinks=False):
        directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
        for name in files:
            path = Path(root) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
            if total > stop_after:
                return total
    return total


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _terminate_process_group(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    """Terminate the full MinerU child group, then kill it after a bounded grace."""
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = asyncio.get_running_loop().time() + grace_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not _process_group_exists(process_group_id):
            break
        await asyncio.sleep(min(0.05, max(0.0, deadline - asyncio.get_running_loop().time())))
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError as error:
        raise WorkerFatalParsingError(
            "MinerU process group did not exit after SIGKILL; worker must stop"
        ) from error
    reap_deadline = asyncio.get_running_loop().time() + 5.0
    while _process_group_exists(process_group_id):
        if asyncio.get_running_loop().time() >= reap_deadline:
            raise WorkerFatalParsingError(
                "MinerU descendants remain after SIGKILL; worker must stop"
            )
        await asyncio.sleep(0.05)


async def _run_cleanup(awaitable: Awaitable[None]) -> None:
    """Finish process cleanup even if the caller's parser task was cancelled."""
    cleanup = asyncio.create_task(awaitable)
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


def _validate_archive(
    archive_path: Path,
    workdir: Path,
    expected_tier: str,
    max_output_bytes: int,
    max_text_bytes: int,
    max_members: int,
) -> _ValidatedResult:
    """Verify ZIP paths, result contracts, bounded expansion, and local assets."""
    if not archive_path.is_file() or archive_path.stat().st_size > max_output_bytes:
        raise PermanentParsingError("invalid_result")
    if not zipfile.is_zipfile(archive_path):
        raise PermanentParsingError("invalid_result")

    output_root = workdir / "validated"
    output_root.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if not members or len(members) > max_members:
                raise PermanentParsingError("invalid_result")
            total_uncompressed = 0
            by_name = {}
            for info in members:
                pure_path = PurePosixPath(info.filename)
                if (
                    pure_path.is_absolute()
                    or ".." in pure_path.parts
                    or "\\" in info.filename
                    or not pure_path.parts
                    or len(info.filename.encode("utf-8")) > 1024
                    or info.filename in by_name
                ):
                    raise PermanentParsingError("invalid_result")
                if info.file_size < 0:
                    raise PermanentParsingError("invalid_result")
                total_uncompressed += info.file_size
                if total_uncompressed > max_output_bytes:
                    raise PermanentParsingError("invalid_result")
                by_name[info.filename] = info

            if not _TOP_LEVEL_FILES.issubset(by_name):
                raise PermanentParsingError("invalid_result")
            payloads: dict[str, bytes] = {}
            for name in _TOP_LEVEL_FILES:
                payloads[name] = _read_member_limited(
                    archive,
                    by_name[name],
                    max_text_bytes,
                )
            try:
                middle = json.loads(payloads["middle_json.json"])
                structured = json.loads(payloads["structured_content.json"])
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise PermanentParsingError("invalid_result") from error

            schema_name = middle.get("schema") if isinstance(middle, dict) else None
            schema_version = middle.get("schema_version") if isinstance(middle, dict) else None
            metadata = middle.get("metadata", {}) if isinstance(middle, dict) else {}
            producer = metadata.get("producer", {}) if isinstance(metadata, dict) else {}
            extensions = middle.get("extensions", {}) if isinstance(middle, dict) else {}
            mineru = extensions.get("mineru", {}) if isinstance(extensions, dict) else {}
            version = producer.get("version") if isinstance(producer, dict) else None
            actual_tier = mineru.get("tier") if isinstance(mineru, dict) else None
            pages = middle.get("pages") if isinstance(middle, dict) else None
            structured_pages = (
                structured.get("pages") if isinstance(structured, dict) else None
            )
            if (
                schema_name != "docvortex.middle"
                or schema_version != "2.0"
                or producer.get("name") != "mineru"
                or not isinstance(version, str)
                or not re.fullmatch(r"4\.\d+\.\d+", version)
                or actual_tier != expected_tier
                or not isinstance(pages, list)
                or not pages
                or not isinstance(structured_pages, list)
                or len(pages) != len(structured_pages)
            ):
                raise PermanentParsingError("invalid_result")

            page_indexes = []
            for page in pages:
                if not isinstance(page, dict) or not isinstance(page.get("page_idx"), int):
                    raise PermanentParsingError("invalid_result")
                if not isinstance(page.get("blocks"), list):
                    raise PermanentParsingError("invalid_result")
                page_indexes.append(page["page_idx"])
            if sorted(page_indexes) != list(range(len(page_indexes))):
                raise PermanentParsingError("invalid_result")

            asset_members: list[_Member] = []
            image_paths: set[str] = set()
            for member_name, info in by_name.items():
                if not member_name.startswith("images/") or info.is_dir():
                    continue
                suffix = PurePosixPath(member_name).suffix.lower()
                content_type = _IMAGE_SUFFIX_TYPES.get(suffix)
                if content_type is None:
                    raise PermanentParsingError("invalid_result")
                target = output_root / f"asset-{len(asset_members):06d}{suffix}"
                digest, size_bytes = _extract_member(archive, info, target, max_output_bytes)
                asset_members.append(_Member(member_name, target, size_bytes, digest, content_type))
                image_paths.add(member_name)
            markdown = payloads["markdown.md"]
            if not _image_references_exist(markdown, middle, structured, image_paths):
                raise PermanentParsingError("invalid_result")

            return _ValidatedResult(
                parser_version=version,
                page_count=len(pages),
                markdown=markdown,
                middle_json=payloads["middle_json.json"],
                structured_content=payloads["structured_content.json"],
                schema_name=schema_name,
                schema_version=schema_version,
                assets=tuple(asset_members),
            )
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as error:
        raise PermanentParsingError("invalid_result") from error


def _read_member_limited(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    if info.file_size > limit:
        raise PermanentParsingError("invalid_result")
    output = bytearray()
    with archive.open(info) as source:
        while chunk := source.read(min(_CHUNK_SIZE, limit + 1 - len(output))):
            output.extend(chunk)
            if len(output) > limit:
                raise PermanentParsingError("invalid_result")
    return bytes(output)


def _extract_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    maximum: int,
) -> tuple[str, int]:
    total = 0
    digest = hashlib.sha256()
    with archive.open(info) as source, target.open("xb") as destination:
        while chunk := source.read(_CHUNK_SIZE):
            total += len(chunk)
            if total > info.file_size or total > maximum:
                raise PermanentParsingError("invalid_result")
            destination.write(chunk)
            digest.update(chunk)
    if total != info.file_size:
        raise PermanentParsingError("invalid_result")
    return digest.hexdigest(), total


def _image_references_exist(
    markdown: bytes,
    middle_json: object,
    structured_content: object,
    image_paths: set[str],
) -> bool:
    """Check local Markdown and JSON image references against ZIP image assets."""
    try:
        text = markdown.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise PermanentParsingError("invalid_result") from error
    references = set(re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text))
    references.update(re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)", text, flags=re.I))
    for root in (middle_json, structured_content):
        references.update(_json_image_references(root))
    for reference in references:
        normalized = reference.strip().strip("\"'").split("#", 1)[0]
        if not normalized or normalized.startswith(("http://", "https://", "data:")):
            continue
        path = PurePosixPath(normalized)
        if "images" in path.parts:
            try:
                position = path.parts.index("images")
            except ValueError:
                continue
            asset_name = "/".join(path.parts[position:])
            if asset_name not in image_paths:
                return False
    return True


def _json_image_references(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(_json_image_references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_json_image_references(item))
    elif isinstance(value, str):
        if value.startswith("images/"):
            found.add(value)
        found.update(re.findall(r"(?:src=[\"'])(images/[^\"']+)", value, flags=re.I))
    return found


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK_SIZE):
            size_bytes += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size_bytes
