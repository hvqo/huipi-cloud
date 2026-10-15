"""Local operator material import/revoke/history/search. No anonymous HTTP ingestion."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.infrastructure.storage.dependencies import _configured_storage
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import MAX_SOURCE_BYTES, ImportRequest, KnowledgeError
from huipi_cloud.modules.knowledge.service import import_material, material_history, revoke_material


async def run(args):
    try:
        if session_factory is None:
            raise KnowledgeError("knowledge_database_unavailable")
        if args.command == "revoke":
            await revoke_material(session_factory, args.assignment_id, args.document_id)
            output = {"status": "revoked", "document_id": str(args.document_id)}
        elif args.command == "history":
            output = await material_history(session_factory, args.assignment_id, args.document_id)
        else:
            try:
                storage = _configured_storage()
            except ValueError:
                raise KnowledgeError("knowledge_storage_not_configured") from None
            if args.command == "import":
                path = Path(args.file)
                if path.suffix.lower() not in {".md", ".txt"}:
                    raise KnowledgeError("knowledge_source_type_not_allowed")
                try:
                    with path.open("rb") as file:
                        raw = file.read(MAX_SOURCE_BYTES + 1)
                except OSError:
                    raise KnowledgeError("knowledge_local_file_unavailable") from None
                request = ImportRequest(
                    assignment_id=args.assignment_id,
                    document_id=args.document_id,
                    title=args.title,
                    source_type="markdown" if path.suffix.lower() == ".md" else "text",
                    permission=args.permission,
                    source_sha256=args.sha256,
                )
                output = (await import_material(session_factory, storage, request, raw)).model_dump(
                    mode="json"
                )
            else:
                output = (
                    await LexicalRetriever(session_factory, storage).search(
                        args.assignment_id, args.query, args.top_k
                    )
                ).model_dump(mode="json")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (KnowledgeError, ValidationError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": error.code
                    if isinstance(error, KnowledgeError)
                    else "knowledge_invalid_input",
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        await dispose_database_engine()


def main():
    parser = argparse.ArgumentParser(description="受控本地教师参考材料；作用域不等于真实身份授权")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("import", "revoke", "history", "search"):
        command = sub.add_parser(name)
        command.add_argument("--assignment-id", required=True, type=UUID)
        if name in {"revoke", "history"}:
            command.add_argument("--document-id", required=True, type=UUID)
        elif name == "import":
            command.add_argument("--document-id", type=UUID)
            command.add_argument("--file", required=True)
            command.add_argument("--title", required=True)
            command.add_argument("--sha256", required=True)
            command.add_argument(
                "--permission",
                required=True,
                choices=["synthetic", "authorized"],
                help="操作员确认仅为已许可的教师参考文本，不是学生/私有答案/评分资料",
            )
        else:
            command.add_argument("--query", required=True)
            command.add_argument("--top-k", type=int, default=3)
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
