"""Explicit opt-in browser: real PDF generation, PG/S3 and OpenCV recognition."""

# ruff: noqa: F401, F811
import os

import pytest
from test_exam_omr import base_papers, papers
from test_exam_rosters import exam, roster
from test_scan_intake import auth_api, scan_context
from test_teacher_workbench_browser import test_teacher_workbench_browser_end_to_end as browser_run


@pytest.mark.anyio
@pytest.mark.skipif(os.getenv("RUN_OMR_BROWSER") != "1", reason="real OMR browser opt-in")
async def test_omr_browser(auth_api, postgres_engine, scan_context, tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_WORKBENCH_E2E", "1")
    await browser_run(auth_api, postgres_engine, scan_context, tmp_path, monkeypatch)
