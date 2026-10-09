"""Conservative line-boundary recognition for explicit student question labels."""

import re
from dataclasses import dataclass

from huipi_cloud.modules.answer_alignment.structure import SourceAtom, canonical_atoms
from huipi_cloud.modules.canonical_documents.protocol import CanonicalDocument

_LINE_MARKER = re.compile(
    r"^[ \t]*(?:"
    r"第\s*(?P<explicit>\d{1,4})\s*题"
    r"|（\s*(?P<fullwidth>\d{1,4})\s*）"
    r"|\(\s*(?P<ascii>\d{1,4})\s*\)"
    r"|(?P<dot>\d{1,4})\.(?=[ \t]|$)"
    r"|(?P<comma>\d{1,4})、"
    r"|(?P<right_paren>\d{1,4})\)(?=[ \t]|$)"
    r")"
)
_DETECTABLE_TYPES = {"text", "paragraph_title", "doc_title", "ref_text"}


@dataclass(frozen=True)
class DetectedCandidate:
    candidate_id: str
    question_number: int
    marker_kind: str
    marker_text: str
    atom_index: int
    text_start: int
    text_end: int
    inside_list: bool
    source_type: str


def detect_question_candidates(
    document: CanonicalDocument,
) -> tuple[list[SourceAtom], list[DetectedCandidate]]:
    """Find only known line-prefix forms in ordinary text nodes.

    Numeric expressions in formulas, tables, lists, page numbers, and prose are
    not treated as question labels. A later matcher still decides whether each
    recognized marker is strong enough for automatic alignment.
    """

    atoms = canonical_atoms(document)
    candidates: list[DetectedCandidate] = []
    for atom_index, atom in enumerate(atoms):
        if (
            atom.text is None
            or atom.inside_list
            or atom.inside_nontext_structure
            or atom.node.source_type not in _DETECTABLE_TYPES
        ):
            continue
        for line_match in re.finditer(r"(?m)^.*$", atom.text):
            line = line_match.group(0)
            marker_match = _LINE_MARKER.match(line)
            if marker_match is None:
                continue
            marker_kind, number = _matched_kind_and_number(marker_match)
            marker_start, marker_end = marker_match.span()
            leading = len(line) - len(line.lstrip(" \t"))
            absolute_start = line_match.start() + leading
            absolute_end = line_match.start() + marker_end
            candidates.append(
                DetectedCandidate(
                    candidate_id=f"candidate-{len(candidates) + 1:05d}",
                    question_number=int(number),
                    marker_kind=marker_kind,
                    marker_text=atom.text[absolute_start:absolute_end],
                    atom_index=atom_index,
                    text_start=absolute_start,
                    text_end=absolute_end,
                    inside_list=False,
                    source_type=atom.node.source_type,
                )
            )
            # Keep the local only for a useful assertion during maintenance;
            # span includes any indentation while the evidence starts at marker.
            assert marker_start <= marker_end
    return atoms, candidates


def _matched_kind_and_number(match: re.Match[str]) -> tuple[str, str]:
    for group, kind in (
        ("explicit", "explicit_chinese"),
        ("fullwidth", "wrapped_parentheses"),
        ("ascii", "wrapped_parentheses"),
        ("dot", "arabic_dot"),
        ("comma", "chinese_comma"),
        ("right_paren", "arabic_right_paren"),
    ):
        value = match.group(group)
        if value is not None:
            return kind, value
    raise AssertionError("marker regex matched without a number")
