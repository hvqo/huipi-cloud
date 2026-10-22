"""One bounded PDF-to-preview process. No OCR, network or identity assignment."""

import hashlib
import json
import resource
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1])
    maximum = int(sys.argv[2])
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024,) * 2)
    from huipi_cloud.infrastructure.parsing.child_guard import _install_parent_death_signal

    _install_parent_death_signal()
    from pypdf import PdfReader

    from huipi_cloud.core.config import Settings
    from huipi_cloud.infrastructure.visual_evidence.render import render_selected_pages
    from huipi_cloud.modules.visual_evidence.errors import VisualEvidenceError

    try:
        reader = PdfReader(root / "original.pdf", strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted")
        count = len(reader.pages)
        if not 1 <= count <= maximum:
            (root / "manifest.json").write_text(json.dumps({"error": "scan_pdf_page_limit"}))
            return 0
        config = Settings(_env_file=None).model_copy(
            update={
                "mineru_max_pdf_pages": maximum,
                "visual_evidence_max_source_pdf_pages": maximum,
                "visual_evidence_max_pages_per_question": maximum,
                "visual_evidence_render_dpi": 110,
                "visual_evidence_max_render_edge": 1600,
                "visual_evidence_max_image_pixels": 2560000,
                "visual_evidence_max_page_image_bytes": 2 * 1024 * 1024,
            }
        )
        images = render_selected_pages(
            root / "original.pdf",
            content_type="application/pdf",
            selected_page_indexes=list(range(count)),
            config=config,
        )
        pages, size = [], 0
        for image in images:
            size += len(image.image_bytes)
            if size > 32 * 1024 * 1024:
                (root / "manifest.json").write_text(json.dumps({"error": "scan_preview_limit"}))
                return 0
            (root / f"page-{image.page_index}.jpg").write_bytes(image.image_bytes)
            pages.append(
                dict(
                    page_index=image.page_index,
                    width=image.rendered_width,
                    height=image.rendered_height,
                    size_bytes=len(image.image_bytes),
                    sha256=hashlib.sha256(image.image_bytes).hexdigest(),
                )
            )
        (root / "manifest.json").write_text(json.dumps({"pages": pages}))
    except (VisualEvidenceError, ValueError, OSError, RuntimeError):
        (root / "manifest.json").write_text(json.dumps({"error": "scan_pdf_invalid"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
