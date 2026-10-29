"""Opt-in Chromium acceptance on isolated PostgreSQL/private S3; no model calls."""
# ruff: noqa: F401, F811

import os

import pytest
from test_exam_rosters import exam, roster
from test_paper_versions import papers
from test_scan_intake import auth_api, scan_context
from test_teacher_workbench_browser import (
    test_teacher_workbench_browser_end_to_end as browser_run,
)


@pytest.mark.anyio
@pytest.mark.skipif(os.getenv("RUN_EXAM_FINAL_BROWSER") != "1", reason="isolated browser opt-in")
async def test_final_browser(auth_api, postgres_engine, scan_context, tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_WORKBENCH_E2E", "1")
    monkeypatch.setenv("RUN_EXAM_GRADING_BROWSER", "1")
    monkeypatch.setenv("RUN_EXAM_RESULTS_BROWSER", "1")
    await browser_run(auth_api, postgres_engine, scan_context, tmp_path, monkeypatch)
