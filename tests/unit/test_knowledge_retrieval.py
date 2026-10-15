"""Deterministic Unicode, protocol, and lexical ranking tests; no paid model."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.copilot.citations import cited_ids
from huipi_cloud.modules.copilot.protocol import CopilotError
from huipi_cloud.modules.copilot.tools import validate_arguments
from huipi_cloud.modules.knowledge.chunking import chunk_text, decode_source, sha256
from huipi_cloud.modules.knowledge.retriever import lexical_score, terms
from huipi_cloud.modules.knowledge.schemas import ImportRequest, KnowledgeError, RetrievalHit


def test_unicode_ranges_paragraphs_and_overlap_are_reproducible():
    text = "# 教师资料\n\n" + ("水的循环😀与 evaporation。\n\n" * 95)
    first = chunk_text(text)
    assert first == chunk_text(text) and len(first) > 3
    assert first[0].start == 0 and first[-1].end == len(text)
    for i, c in enumerate(first):
        assert c.text == text[c.start : c.end] and c.sha256 == sha256(c.text.encode())
        assert c.end - c.start == len(c.text) <= 400
        if i:
            assert first[i - 1].end - c.start == 48
    assert "".join(c.text if i == 0 else c.text[48:] for i, c in enumerate(first)) == text
    assert len(text.encode()) != len(text)


def test_source_hash_size_utf8_and_empty_are_distinct():
    raw = "中文原文".encode()
    assert decode_source(raw, sha256(raw)) == "中文原文"
    for data, digest, code in [
        (raw, "0" * 64, "sha_mismatch"),
        (b"x" * 65537, "0" * 64, "size_limit"),
        (b"\xff", sha256(b"\xff"), "invalid_utf8"),
        (b"  ", sha256(b"  "), "invalid_text"),
    ]:
        with pytest.raises(KnowledgeError, match=code):
            decode_source(data, digest)


def test_lexical_chinese_english_normalization_and_score_meaning():
    assert lexical_score("热传导", "热传导通过接触传递能量。") == 1
    assert lexical_score("ＮＥＷＴＯＮ", "Newton定律") == 1
    assert lexical_score("ZXQ9999", "热传导") == 0
    assert terms("水") == {"水": 1}
    assert 0 < lexical_score("热传导 光合作用", "热传导") < 1
    assert lexical_score("热传导", "热传导" * 100) == 1  # No invented BM25 frequency model.


def test_import_excludes_unknown_artifact_types_and_fields():
    base = dict(
        assignment_id=uuid4(),
        title="教师材料",
        source_type="markdown",
        permission="synthetic",
        source_sha256="a" * 64,
    )
    assert ImportRequest(**base).material_kind == "teacher_reference"
    for extra in [
        {"source_type": "student_pdf"},
        {"permission": "unknown"},
        {"material_kind": "GradeProposal"},
        {"object_key": "other/data"},
    ]:
        with pytest.raises(ValidationError):
            ImportRequest(**{**base, **extra})


def test_search_tool_forbids_scope_sql_url_parameters_but_accepts_literal_query():
    assert validate_arguments("search_assignment_materials", '{"query":"热传导"}').top_k == 3
    for params in [
        '{"query":"水","assignment_id":"other"}',
        '{"query":"水","url":"https://example.com"}',
        '{"sql":"DROP TABLE assignments"}',
        '{"query":"水","top_k":6}',
    ]:
        with pytest.raises(CopilotError, match="invalid_tool_arguments"):
            validate_arguments("search_assignment_materials", params)


def test_citations_are_run_bound_and_format_is_not_silently_repaired():
    cid = "K" + uuid4().hex
    assert cited_ids(f"资料内容[[{cid}]]和再次引用[[{cid}]]", {cid: object()}) == [cid]
    for answer, code in [
        ("根据资料[[K" + "0" * 32 + "]]", "unknown_citation_id"),
        ("参考[[Kabcdef", "invalid_citation_format"),
        ("参考[[other]]", "unknown_citation_id"),
    ]:
        with pytest.raises(CopilotError, match=code):
            cited_ids(answer, {cid: object()})


def test_hit_identity_offsets_are_unicode_not_bytes():
    chunk_id = uuid4()
    hit = RetrievalHit(
        citation_id="K" + chunk_id.hex,
        document_id=uuid4(),
        title="资料",
        source_version=1,
        source_sha256="a" * 64,
        chunk_id=chunk_id,
        chunk_text_sha256="b" * 64,
        text_start=2,
        text_end=4,
        excerpt="中文",
        relevance_score=1,
    )
    for changes in [{"text_end": 8}, {"citation_id": "K" + uuid4().hex}]:
        with pytest.raises(ValidationError):
            RetrievalHit(**{**hit.model_dump(), **changes})
