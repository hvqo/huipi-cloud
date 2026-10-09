"""Linux child-process supervisor with a parent-death signal and PDF preflight."""

import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_PR_SET_PDEATHSIG = 1
_INVALID_PDF_EXIT = 70
_PAGE_LIMIT_EXIT = 71


def _kill_process_group_on_parent_death(_: int, __: object) -> None:
    """Kill this isolated process group if its Worker parent dies unexpectedly."""
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _install_parent_death_signal() -> None:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("MinerU process isolation requires Linux PR_SET_PDEATHSIG")
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGUSR1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "could not set parser parent-death signal")
    signal.signal(signal.SIGUSR1, _kill_process_group_on_parent_death)
    if os.getppid() != parent_pid:
        _kill_process_group_on_parent_death(signal.SIGUSR1, None)


def _validate_pdf(path: Path, page_limit: int) -> int:
    """Reject encrypted, malformed, and over-limit PDFs before model execution."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            return _INVALID_PDF_EXIT
        page_count = len(reader.pages)
    except Exception:
        return _INVALID_PDF_EXIT
    return _PAGE_LIMIT_EXIT if page_count > page_limit else 0


def _parse_args(arguments: Sequence[str]) -> tuple[Path | None, int | None, list[str]]:
    try:
        separator = arguments.index("--")
    except ValueError as error:
        raise ValueError("child command separator is missing") from error
    options = list(arguments[:separator])
    command = list(arguments[separator + 1 :])
    if not command:
        raise ValueError("child command is missing")

    pdf_path: Path | None = None
    page_limit: int | None = None
    while options:
        option = options.pop(0)
        if option == "--pdf-path" and options:
            pdf_path = Path(options.pop(0))
        elif option == "--max-pdf-pages" and options:
            page_limit = int(options.pop(0))
        else:
            raise ValueError("invalid child guard option")
    if (pdf_path is None) != (page_limit is None):
        raise ValueError("PDF preflight options must be set together")
    return pdf_path, page_limit, command


def main() -> int:
    """Preflight one PDF, then replaceable process supervision for the MinerU CLI."""
    try:
        pdf_path, page_limit, command = _parse_args(sys.argv[1:])
        _install_parent_death_signal()
    except (ValueError, OSError, RuntimeError):
        return 72

    if pdf_path is not None and page_limit is not None:
        preflight_result = _validate_pdf(pdf_path, page_limit)
        if preflight_result:
            if preflight_result == _PAGE_LIMIT_EXIT:
                print("PDF_PAGE_LIMIT_EXCEEDED", file=sys.stderr)
            else:
                print("PDF_INPUT_INVALID", file=sys.stderr)
            return preflight_result

    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    except OSError:
        return 73
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
