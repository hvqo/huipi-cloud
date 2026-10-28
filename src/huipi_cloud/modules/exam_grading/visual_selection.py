"""Teacher-confirmed, immutable image rectangles bound to a current answer script."""

import hashlib
from io import BytesIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import distinct_on

from huipi_cloud.modules.canonical_documents.service import _read_checked_object
from huipi_cloud.modules.grading.protocol import json_sha256
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans import service as scans

from .models import ExamVisualRegion, ExamVisualSelection
from .visual_protocol import VisualRegion

V, R = ExamVisualSelection.__table__, ExamVisualRegion.__table__


def crop(data: bytes, coords: tuple[int, int, int, int], dimensions=None) -> tuple[bytes, int, int]:
    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 12_000_000:
                raise ValueError("image dimensions")
            if dimensions is not None and dimensions != (width, height):
                raise ValueError("image dimensions changed")
            x0, y0, x1, y1 = coords
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise ValueError("invalid image region")
            if x1 - x0 < 16 or y1 - y0 < 16:
                raise ValueError("image region too small")
            image.load()
            with image.convert("RGB").crop(coords) as selected:
                out = BytesIO()
                selected.save(out, format="PNG", optimize=True)
                value = out.getvalue()
            if len(value) > 1024 * 1024:
                raise ValueError("image region too large")
            return value, width, height
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
        raise AccessError("exam_visual_image_or_region_invalid", 422) from None


async def page_bytes(storage, row):
    if row["bucket"] != storage.bucket:
        raise AccessError("exam_visual_source_unavailable", 503)
    return (
        await _read_checked_object(
            storage,
            row["object_key"],
            expected_size=row["size_bytes"],
            expected_sha256=row["sha256"],
            maximum=2 * 1024 * 1024,
        )
    ).data


async def latest(db, wid):
    return (
        (
            await db.execute(
                select(V).where(V.c.workspace_id == wid).order_by(V.c.revision.desc()).limit(1)
            )
        )
        .mappings()
        .first()
    )


async def region_values(db, selection, source_rows):
    rows = (
        (
            await db.execute(
                select(R).where(R.c.selection_id == selection["id"]).order_by(R.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    sources = {r["id"]: r for r in source_rows}
    values = []
    for row in rows:
        page = sources.get(row["scan_page_id"])
        if page is None or page["sha256"] != row["page_sha256"]:
            raise AccessError("exam_visual_source_changed", 409)
        values.append(
            VisualRegion(
                region_index=row["ordinal"],
                scan_page_id=row["scan_page_id"],
                scan_file_id=page["file_id"],
                source_page_index=page["page_index"],
                page_sha256=row["page_sha256"],
                crop_sha256=row["crop_sha256"],
                width=row["width"],
                height=row["height"],
                x0=row["x0"],
                y0=row["y0"],
                x1=row["x1"],
                y1=row["y1"],
            )
        )
    if (
        len(values) != (len(rows) if selection["active"] else 0)
        or (selection["active"] and not values)
        or [v.region_index for v in values] != list(range(len(values)))
    ):
        raise AccessError("exam_visual_selection_invalid", 409)
    if json_sha256(values) != selection["regions_sha256"]:
        raise AccessError("exam_visual_selection_digest_mismatch", 409)
    return values


async def read(db, c):
    selection = await latest(db, c.workspace["id"])
    if selection is None:
        return None
    if selection["script_version_id"] != c.source[1]["id"]:
        raise AccessError("exam_visual_source_changed", 409)
    values = await region_values(db, selection, c.source[2])
    return dict(
        id=selection["id"],
        revision=selection["revision"],
        active=selection["active"],
        regions=values,
    )


async def reset_for_text(db, c, p, answer_review_id):
    """A new explicit text confirmation supersedes an active image selection."""
    current = await latest(db, c.workspace["id"])
    if current:
        if current["revision"] >= 100:
            raise AccessError("exam_visual_revision_limit", 409)
        await db.execute(
            insert(V).values(
                id=uuid4(),
                workspace_id=c.workspace["id"],
                script_version_id=c.source[1]["id"],
                revision=current["revision"] + 1,
                active=False,
                text_answer_review_id=answer_review_id,
                regions_sha256=json_sha256([]),
                actor_user_id=p.user_id,
            )
        )


async def _check_other_regions(db, c, values):
    """A selected rectangle cannot silently appropriate another question's current region."""
    from . import service as g

    active = (
        select(V.c.id, V.c.workspace_id, V.c.active)
        .join(g.W, g.W.c.id == V.c.workspace_id)
        .where(g.W.c.parsing_job_id == c.job["id"], V.c.workspace_id != c.workspace["id"])
        .ext(distinct_on(V.c.workspace_id))
        .order_by(V.c.workspace_id, V.c.revision.desc())
        .subquery()
    )
    others = (
        (
            await db.execute(
                select(R).join(active, active.c.id == R.c.selection_id).where(active.c.active)
            )
        )
        .mappings()
        .all()
    )
    for value in values:
        for other in others:
            if value.scan_page_id == other["scan_page_id"] and (
                max(value.x0, other["x0"]) < min(value.x1, other["x1"])
                and max(value.y0, other["y0"]) < min(value.y1, other["y1"])
            ):
                raise AccessError("exam_visual_region_owned_by_other_question", 409)


async def select_regions(db, p, storage, wid, request):
    from huipi_cloud.modules.marking_tasks.access import require_working

    from . import service as g

    digest = json_sha256(
        dict(action="visual_select", workspace=str(wid), body=request.model_dump(mode="json"))
    )
    async with scans.transaction(db):
        c = await g.workspace_context(db, p, wid, "exam.answer.review")
        await require_working(db, p, wid)
        prior = await g.replay(db, p, request.request_id, digest)
        if prior:
            return prior
        current = await latest(db, wid)
        if (current["revision"] if current else 0) != request.expected_revision:
            raise AccessError("exam_visual_revision_conflict", 409)
        source_version, rows = c.source[1]["id"], c.source[2]
    await g.parsing.scripts.verify_sources(storage, rows)
    by_id = {r["id"]: r for r in rows}
    cached = {}
    values = []
    for ordinal, rect in enumerate(request.regions):
        page = by_id.get(rect.scan_page_id)
        if page is None:
            raise AccessError("resource_not_found_or_forbidden", 404)
        if page["id"] not in cached:
            cached[page["id"]] = await page_bytes(storage, page)
        data = cached[page["id"]]
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise AccessError("exam_visual_image_or_region_invalid", 422) from None
        x0, y0 = rect.x0 * width // 10000, rect.y0 * height // 10000
        x1 = (rect.x1 * width + 9999) // 10000
        y1 = (rect.y1 * height + 9999) // 10000
        selected, width, height = crop(data, (x0, y0, x1, y1))
        values.append(
            VisualRegion(
                region_index=ordinal,
                scan_page_id=page["id"],
                scan_file_id=page["file_id"],
                source_page_index=page["page_index"],
                page_sha256=page["sha256"],
                crop_sha256=hashlib.sha256(selected).hexdigest(),
                width=width,
                height=height,
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
            )
        )
    async with scans.transaction(db):
        c = await g.workspace_context(db, p, wid, "exam.answer.review")
        await require_working(db, p, wid)
        prior = await g.replay(db, p, request.request_id, digest)
        if prior:
            return prior
        current = await latest(db, wid)
        if (current["revision"] if current else 0) != request.expected_revision:
            raise AccessError("exam_visual_revision_conflict", 409)
        if c.source[1]["id"] != source_version or any(
            not any(
                row["id"] == v.scan_page_id and row["sha256"] == v.page_sha256
                for row in c.source[2]
            )
            for v in values
        ):
            raise AccessError("exam_visual_source_changed", 409)
        await _check_other_regions(db, c, values)
        selection_id = uuid4()
        await db.execute(
            insert(V).values(
                id=selection_id,
                workspace_id=wid,
                script_version_id=source_version,
                revision=request.expected_revision + 1,
                active=bool(values),
                regions_sha256=json_sha256(values),
                actor_user_id=p.user_id,
            )
        )
        if values:
            await db.execute(
                insert(R),
                [
                    dict(
                        selection_id=selection_id,
                        ordinal=v.region_index,
                        script_version_id=source_version,
                        scan_page_id=v.scan_page_id,
                        page_sha256=v.page_sha256,
                        crop_sha256=v.crop_sha256,
                        width=v.width,
                        height=v.height,
                        x0=v.x0,
                        y0=v.y0,
                        x1=v.x1,
                        y1=v.y1,
                    )
                    for v in values
                ],
            )
        result = dict(selection_id=str(selection_id), revision=request.expected_revision + 1)
        await g.audit(db, p, c.job["id"], request.request_id, "visual_select", digest, result)
        return result


async def materialize(storage, regions, source_rows):
    sources = {r["id"]: r for r in source_rows}
    cached = {}
    images = []
    for region in regions:
        page = sources.get(region.scan_page_id)
        if page is None or page["sha256"] != region.page_sha256:
            raise AccessError("exam_visual_source_changed", 409)
        if page["id"] not in cached:
            cached[page["id"]] = await page_bytes(storage, page)
        value, _, _ = crop(
            cached[page["id"]],
            (region.x0, region.y0, region.x1, region.y1),
            (region.width, region.height),
        )
        if hashlib.sha256(value).hexdigest() != region.crop_sha256:
            raise AccessError("exam_visual_crop_mismatch", 409)
        images.append(value)
    return images
