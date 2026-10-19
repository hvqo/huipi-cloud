"""Keep the bounded legacy policy inventory aligned with actual models and HTTP."""

import json
from pathlib import Path

from huipi_cloud.infrastructure.database.base import Base
from huipi_cloud.main import app
from huipi_cloud.modules.resource_security.service import CATALOG
from huipi_cloud.modules.resource_security.visibility import rules


def test_classification_rules_cover_every_mapped_resource_parent():
    covered = {model for model, _ in rules()}
    resources = {
        mapper.class_
        for mapper in Base.registry.mappers
        if {"assignment_id", "submission_id", "question_id", "document_version_id"}
        & set(mapper.columns.keys())
        and not mapper.local_table.name.startswith("security_")
    }
    assert resources <= covered
    assert "exam.session.read" in CATALOG
    assert "exam.participation.read" not in CATALOG
    assert set().union(*CATALOG.values()) == {
        "installation",
        "assignment",
        "paper_version",
        "exam_event",
        "exam_session",
    }


def test_resource_security_call_inventory_matches_all_registered_methods_and_paths():
    document = json.loads(
        (Path(__file__).parents[2] / "docs/security/resource-security-paths.json").read_text()
    )
    entries = document["operations"]
    expected = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method in {"get", "head", "post", "put", "patch", "delete", "options"}
    } | {(method, route.path) for route in app.routes for method in getattr(route, "methods", ())}
    actual = {(item["method"], item["path"]) for item in entries}
    assert len(entries) == len(actual)
    assert actual == expected
    assert all(item["classification_boundary"] and item["entrypoint"] for item in entries)
