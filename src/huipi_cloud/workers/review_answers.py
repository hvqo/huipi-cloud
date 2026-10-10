"""Locally controlled CLI for inspecting and recording answer-presence review."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.answer_alignment.errors import (
    AnswerAlignmentArtifactCorruptError,
    AnswerAlignmentNotFoundError,
    AnswerAlignmentNotReadyError,
    AnswerAlignmentResultNotFoundError,
)
from huipi_cloud.modules.answer_alignment.service import (
    VerifiedAlignmentBundle,
    load_verified_alignment_bundle,
)
from huipi_cloud.modules.answer_review import service
from huipi_cloud.modules.answer_review.errors import (
    AnswerReviewIdempotencyConflictError,
    AnswerReviewQuestionNotFoundError,
    AnswerReviewRegionInvalidError,
    AnswerReviewSourceChangedError,
    AnswerReviewSourceNotReadyError,
    AnswerReviewSubmissionNotFoundError,
)
from huipi_cloud.modules.answer_review.protocol import (
    AnswerReviewDecisionCreate,
    ReviewRegionSelection,
)
from huipi_cloud.modules.submissions.models import SubmissionFile

logger = logging.getLogger(__name__)
_EXCERPT_LIMIT = 360


def _parse_region(value: str) -> ReviewRegionSelection:
    parts = value.rsplit(":", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        try:
            return ReviewRegionSelection(
                content_pointer=parts[0],
                text_start=int(parts[1]),
                text_end=int(parts[2]),
            )
        except ValidationError as error:
            raise argparse.ArgumentTypeError("文本范围无效") from error
    if value.count(":") >= 2:
        raise argparse.ArgumentTypeError("区域格式应为CONTENT_POINTER或CONTENT_POINTER:START:END")
    return ReviewRegionSelection(content_pointer=value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地作业作答存在性人工复核工具")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="检查当前对齐证据")
    inspect.add_argument("--submission-id", required=True, type=UUID)

    record = commands.add_parser("record", help="追加一条人工决定")
    record.add_argument("--submission-id", required=True, type=UUID)
    record.add_argument("--question-id", required=True, type=UUID)
    record.add_argument("--request-id", required=True, type=UUID)
    record.add_argument("--reviewer-ref", required=True)
    record.add_argument(
        "--decision",
        required=True,
        choices=("response_present", "response_absent", "uncertain"),
    )
    record.add_argument("--response-region", action="append", type=_parse_region, default=[])
    record.add_argument("--prompt-region", action="append", type=_parse_region, default=[])
    record.add_argument("--uncertain-region", action="append", type=_parse_region, default=[])
    record.add_argument("--reason-code", action="append", required=True)

    history = commands.add_parser("history", help="查看追加修订历史")
    history.add_argument("--submission-id", required=True, type=UUID)
    history.add_argument("--question-id", type=UUID)

    export = commands.add_parser("export", help="导出脱敏后的人工标注")
    export.add_argument("--submission-id", required=True, type=UUID)
    export.add_argument("--output", type=Path)
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    if session_factory is None:
        _write({"status": "failed", "failure_code": "database_not_configured"})
        return 1
    try:
        storage = get_object_storage()
        if arguments.command == "inspect":
            bundle = await load_verified_alignment_bundle(
                session_factory,
                storage,
                arguments.submission_id,
            )
            async with session_factory() as session:
                decisions = await service.list_decisions(
                    session,
                    arguments.submission_id,
                )
                source_file = await session.scalar(
                    select(SubmissionFile).where(
                        SubmissionFile.submission_id == arguments.submission_id
                    )
                )
            _write(_inspection(bundle, decisions, source_file))
        elif arguments.command == "record":
            payload = AnswerReviewDecisionCreate(
                decision=arguments.decision,
                response_regions=arguments.response_region,
                excluded_prompt_regions=arguments.prompt_region,
                uncertain_regions=arguments.uncertain_region,
                reason_codes=arguments.reason_code,
                reviewer_ref=arguments.reviewer_ref,
            )
            result, created = await service.record_decision(
                session_factory,
                storage,
                submission_id=arguments.submission_id,
                question_id=arguments.question_id,
                request_id=arguments.request_id,
                payload=payload,
            )
            _write(
                {
                    "status": "created" if created else "replayed",
                    "decision_id": str(result.decision_id),
                    "question_id": str(result.question_id),
                    "decision": result.decision,
                    "revision": result.revision,
                    "alignment_status": result.alignment_status,
                    "source_bound": True,
                }
            )
        elif arguments.command == "history":
            await _history(arguments, storage)
        elif arguments.command == "export":
            return await _export(arguments)
        return 0
    except AnswerReviewSubmissionNotFoundError:
        return _failure("submission_not_found")
    except AnswerReviewQuestionNotFoundError:
        return _failure("question_not_in_assignment")
    except AnswerReviewRegionInvalidError:
        return _failure("source_region_invalid")
    except AnswerReviewSourceChangedError:
        return _failure("source_version_changed_retry_inspect")
    except AnswerReviewSourceNotReadyError:
        return _failure("alignment_not_ready")
    except AnswerReviewIdempotencyConflictError:
        return _failure("request_id_payload_conflict")
    except ValidationError:
        return _failure("review_payload_invalid")
    except (AnswerAlignmentNotFoundError, AnswerAlignmentResultNotFoundError):
        return _failure("submission_or_alignment_not_found")
    except AnswerAlignmentNotReadyError:
        return _failure("canonical_not_ready")
    except AnswerAlignmentArtifactCorruptError:
        return _failure("canonical_or_alignment_invalid")
    except StorageUnavailableError:
        return _failure("storage_unavailable")
    except DBAPIError as error:
        logger.error("Answer review database operation failed (%s)", type(error).__name__)
        return _failure("database_unavailable")
    except HTTPException:
        return _failure("object_store_not_configured")
    except OSError:
        return _failure("local_or_database_io_failed")


async def _history(arguments: argparse.Namespace, storage) -> None:
    async with session_factory() as session:
        records = await service.list_decisions(
            session,
            arguments.submission_id,
            question_id=arguments.question_id,
        )
    current_bundle: VerifiedAlignmentBundle | None = None
    current_source_available = True
    try:
        current_bundle = await load_verified_alignment_bundle(
            session_factory,
            storage,
            arguments.submission_id,
        )
    except (
        AnswerAlignmentNotFoundError,
        AnswerAlignmentNotReadyError,
        AnswerAlignmentResultNotFoundError,
    ):
        current_source_available = False
    _write(
        {
            "submission_id": str(arguments.submission_id),
            "current_source_available": current_source_available,
            "revisions": [
                {
                    "decision_id": str(item.decision_id),
                    "question_id": str(item.question_id),
                    "question_number": item.question_number,
                    "decision": item.decision,
                    "alignment_status": item.alignment_status,
                    "revision": item.revision,
                    "supersedes_decision_id": (
                        None
                        if item.supersedes_decision_id is None
                        else str(item.supersedes_decision_id)
                    ),
                    "created_at": item.created_at.isoformat(),
                    "source_version_matches": current_bundle is not None
                    and service.is_current_for_bundle(item, current_bundle),
                    "latest_revision_for_question": item.revision
                    == max(
                        candidate.revision
                        for candidate in records
                        if candidate.question_id == item.question_id
                    ),
                    "effective_for_current_source": current_bundle is not None
                    and service.is_current_for_bundle(item, current_bundle)
                    and item.revision
                    == max(
                        candidate.revision
                        for candidate in records
                        if candidate.question_id == item.question_id
                    ),
                }
                for item in records
            ],
        }
    )


async def _export(arguments: argparse.Namespace) -> int:
    async with session_factory() as session:
        records = await service.list_decisions(session, arguments.submission_id)
    value = service.export_records(records)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if arguments.output is None:
        _write(value)
        return 0
    path = arguments.output.expanduser().resolve()
    repository = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve()
    if path == repository or repository in path.parents:
        return _failure("export_path_must_be_outside_repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(encoded)
    _write({"status": "exported", "record_count": len(records), "path": str(path)})
    return 0


def _inspection(bundle: VerifiedAlignmentBundle, decisions, source_file) -> dict[str, object]:
    nodes = service._index_canonical_nodes(bundle.canonical_document)
    latest_by_question = {}
    for decision in decisions:
        if decision.question_id not in latest_by_question or (
            decision.revision > latest_by_question[decision.question_id].revision
        ):
            latest_by_question[decision.question_id] = decision
    by_question = []
    for answer in bundle.alignment_document.answers:
        latest = latest_by_question.get(answer.question_id)
        matches = latest is not None and service.is_current_for_bundle(latest, bundle)
        by_question.append(
            {
                "question_id": str(answer.question_id),
                "question_number": answer.question_number,
                "alignment_status": answer.matching_status,
                "answer_presence": latest.decision if matches else "unreviewed",
                "review_revision": latest.revision if matches else None,
                "stale_review_exists": latest is not None and not matches,
                "evidence": [_region_view(region, nodes) for region in answer.source_regions],
            }
        )
    unassigned = [
        {
            "reason_code": item.reason_code,
            "evidence": [_region_view(region, nodes) for region in item.source_regions],
        }
        for item in bundle.alignment_document.unassigned_regions
    ]
    return {
        "submission_id": str(bundle.context.submission_id),
        "assignment_id": str(bundle.context.assignment_id),
        "canonical_document_id": str(bundle.canonical_artifact.id),
        "canonical_sha256": bundle.canonical_artifact.canonical_sha256,
        "alignment_artifact_id": str(bundle.alignment_artifact.id),
        "alignment_sha256": bundle.alignment_artifact.alignment_sha256,
        "question_set_digest": bundle.questions_digest,
        "original_source": (
            None
            if source_file is None
            else {
                "file_id": str(source_file.id),
                "content_type": source_file.content_type,
                "size_bytes": source_file.size_bytes,
                "sha256": source_file.sha256,
            }
        ),
        "questions": by_question,
        "unassigned_regions": unassigned,
        "access_boundary": "本地受控CLI；reviewer_ref不是认证身份",
    }


def _region_view(region, nodes: dict[str, object]) -> dict[str, object]:
    location = nodes[region.content_pointer]
    value = location.node.value or ""
    excerpt = (
        value[region.text_start : region.text_end]
        if region.text_start is not None and region.text_end is not None
        else value
    )
    if len(excerpt) > _EXCERPT_LIMIT:
        excerpt = excerpt[:_EXCERPT_LIMIT] + "…"
    return {
        "page_index": region.page_index,
        "page_number": location.page_number,
        "block_id": str(region.source_block_id),
        "content_pointer": region.content_pointer,
        "bbox": region.bbox,
        "source_type": region.source_type,
        "normalized_type": region.normalized_type,
        "text_start": region.text_start,
        "text_end": region.text_end,
        "excerpt": excerpt,
        "asset_refs": [asset.model_dump(mode="json") for asset in region.asset_refs],
    }


def _write(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _failure(code: str) -> int:
    _write({"status": "failed", "failure_code": code})
    return 1


async def _main() -> int:
    try:
        return await _run(_parser().parse_args())
    except Exception as error:
        # Keep unexpected implementation or dependency failures from printing
        # a traceback that may include local paths or sensitive context.
        logger.error("Answer review command failed (%s)", type(error).__name__)
        return _failure("internal_error")
    finally:
        await dispose_database_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
