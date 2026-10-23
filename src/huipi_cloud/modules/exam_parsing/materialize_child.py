"""Bounded PDF page assembly in an isolated process; no network or model execution."""

import json
import resource
import sys
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from huipi_cloud.infrastructure.parsing.child_guard import _install_parent_death_signal


def main():
    _install_parent_death_signal()
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
    root = Path(sys.argv[1])
    data = json.loads((root / "selection.json").read_text())
    if not 1 <= len(data) <= 100:
        raise ValueError("page_limit")
    readers, writer = {}, PdfWriter()
    for item in data:
        name, index = item["file"], item["index"]
        if not name.startswith("source-") or Path(name).name != name or not 0 <= index < 50:
            raise ValueError("invalid_source")
        if name not in readers:
            readers[name] = PdfReader(root / name, strict=False)
        reader = readers[name]
        if reader.is_encrypted or index >= len(reader.pages):
            raise ValueError("invalid_source")
        # Import one selected source page, preserving its native resolution and text.
        writer.add_page(reader.pages[index])
    with (root / "assembled.pdf").open("wb") as output:
        writer.write(output)
    writer.close()


if __name__ == "__main__":
    main()
