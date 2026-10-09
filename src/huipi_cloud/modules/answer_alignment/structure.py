"""Read Canonical content in source order without flattening away its identity."""

from dataclasses import dataclass

from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
)


@dataclass(frozen=True)
class SourceAtom:
    """A scalar content node or an asset-bearing structural node."""

    block: CanonicalBlock
    node: CanonicalContentNode
    content_pointer: str
    text: str | None
    inside_list: bool
    inside_nontext_structure: bool


def canonical_atoms(document: CanonicalDocument) -> list[SourceAtom]:
    atoms: list[SourceAtom] = []

    def visit(
        block: CanonicalBlock,
        node: CanonicalContentNode,
        pointer: str,
        *,
        inside_list: bool,
        inside_nontext_structure: bool,
    ) -> None:
        is_list = inside_list or node.source_type == "list"
        structured = inside_nontext_structure or node.normalized_type in {
            "formula",
            "image",
            "table",
            "chart",
            "code",
        }
        if node.value is not None or (not node.children and node.asset_refs) or (
            node.children and node.asset_refs
        ):
            atoms.append(
                SourceAtom(
                    block=block,
                    node=node,
                    content_pointer=pointer,
                    text=node.value,
                    inside_list=is_list,
                    inside_nontext_structure=structured,
                )
            )
        for child_index, child in enumerate(node.children):
            visit(
                block,
                child,
                f"{pointer}/children/{child_index}",
                inside_list=is_list,
                inside_nontext_structure=structured,
            )

    for page_index, page in enumerate(document.pages):
        for block_index, block in enumerate(page.blocks):
            visit(
                block,
                block.content,
                f"/pages/{page_index}/blocks/{block_index}/content",
                inside_list=block.normalized_type == "list",
                inside_nontext_structure=block.normalized_type
                in {"formula", "image", "table", "chart", "code"},
            )
    return atoms
