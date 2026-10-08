"""Tests for bounded process-group cancellation and parent-death cleanup."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from huipi_cloud.infrastructure.parsing.mineru import _terminate_process_group

_GUARD_MODULE = "huipi_cloud.infrastructure.parsing.child_guard"


def _is_running(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text().split()
    except OSError:
        return False
    return len(fields) > 2 and fields[2] != "Z"


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_sigkill_of_worker_parent_kills_guard_and_descendant_group(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "descendants.txt"
    guard_pid_file = tmp_path / "guard.txt"
    target = (
        "import pathlib, subprocess, time, os; "
        "child=subprocess.Popen(['sleep','60']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(f'{{os.getpid()}} {{child.pid}}'); "
        "time.sleep(60)"
    )
    worker = (
        "import pathlib, subprocess, sys, time; "
        f"guard=subprocess.Popen([sys.executable,'-m',{_GUARD_MODULE!r},'--',"
        f"sys.executable,'-c',{target!r}],cwd={str(Path.cwd())!r},"
        "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(guard_pid_file)!r}).write_text(str(guard.pid)); "
        "time.sleep(60)"
    )
    worker_process = subprocess.Popen([sys.executable, "-c", worker])
    guard_pid = 0
    process_ids: list[int] = []
    try:
        assert _wait_until(lambda: child_pid_file.exists() and guard_pid_file.exists())
        guard_pid = int(guard_pid_file.read_text())
        process_ids = [int(value) for value in child_pid_file.read_text().split()]

        worker_process.kill()
        worker_process.wait(timeout=3)

        assert _wait_until(lambda: all(not _is_running(pid) for pid in [guard_pid, *process_ids]))
    finally:
        if worker_process.poll() is None:
            worker_process.kill()
            worker_process.wait(timeout=3)
        if guard_pid and _is_running(guard_pid):
            try:
                os.killpg(guard_pid, 9)
            except ProcessLookupError:
                pass


def test_sigterm_then_sigkill_stops_uncooperative_parser_children(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.txt"
    target = (
        "import pathlib, signal, subprocess, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "child=subprocess.Popen(['sleep','60']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )

    async def run() -> tuple[int, int]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            _GUARD_MODULE,
            "--",
            sys.executable,
            "-c",
            target,
            cwd=Path.cwd(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        assert await asyncio.to_thread(
            _wait_until,
            child_pid_file.exists,
        )
        child_pid = int(child_pid_file.read_text())
        await _terminate_process_group(process, grace_seconds=0.05)
        return process.pid, child_pid

    group_id, child_id = asyncio.run(run())
    assert not _is_running(group_id)
    assert not _is_running(child_id)
