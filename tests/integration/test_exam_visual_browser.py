"""Opt-in browser workflow; RUN_VISUAL_REAL is required for any billed model call."""
# ruff: noqa: F401, F811
import os

import pytest
from test_exam_rosters import exam, roster
from test_paper_versions import papers
from test_scan_intake import auth_api, scan_context
from test_teacher_workbench_browser import test_teacher_workbench_browser_end_to_end as browser_run


@pytest.mark.anyio
@pytest.mark.deepseek_e2e
@pytest.mark.skipif(os.getenv("RUN_EXAM_VISUAL_BROWSER") != "1", reason="visual browser opt-in")
async def test_visual_browser(auth_api, postgres_engine, scan_context, tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_WORKBENCH_E2E", "1")
    await browser_run(auth_api, postgres_engine, scan_context, tmp_path, monkeypatch)
