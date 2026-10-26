"""Minimal OOXML: typed inline strings, decimal cells, no formulas or external links."""

from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZIP_DEFLATED, ZipFile

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def letter(number):
    result = ""
    while number:
        number, rest = divmod(number - 1, 26)
        result = chr(65 + rest) + result
    return result


def xml(root):
    return tostring(root, encoding="utf-8", xml_declaration=True)


def sheet(rows, numeric_columns=()):
    root = Element("worksheet", xmlns=NS)
    view = SubElement(SubElement(root, "sheetViews"), "sheetView", workbookViewId="0")
    SubElement(view, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    SubElement(root, "sheetFormatPr", defaultRowHeight="22")
    cols = SubElement(root, "cols")
    for j in range(1, len(rows[0]) + 1 if rows else 1):
        SubElement(
            cols, "col", min=str(j), max=str(j), width="32" if j <= 7 else "20", customWidth="1"
        )
    data = SubElement(root, "sheetData")
    for i, values in enumerate(rows, 1):
        row = SubElement(
            data, "row", r=str(i), **({"ht": "30", "customHeight": "1"} if i == 1 else {})
        )
        for j, value in enumerate(values, 1):
            if value is None:
                continue
            cell = SubElement(row, "c", r=f"{letter(j)}{i}")
            if i > 1 and j in numeric_columns:
                cell.set("t", "n")
                if not isinstance(value, int):
                    cell.set("s", "1")
                SubElement(cell, "v").text = str(value)
            else:
                cell.set("t", "inlineStr")
                if i == 1:
                    cell.set("s", "2")
                # Never infer formulas from leading =,+,-,@ or whitespace/control characters.
                safe = "".join(c for c in str(value) if ord(c) >= 32 or c in "\t\n\r")[:32767]
                SubElement(SubElement(cell, "is"), "t", {"xml:space": "preserve"}).text = safe
    SubElement(root, "sheetProtection", sheet="1", objects="1", scenarios="1")
    if rows:
        SubElement(root, "autoFilter", ref=f"A1:{letter(len(rows[0]))}{len(rows)}")
    return xml(root)


def styles():
    root = Element("styleSheet", xmlns=NS)
    fonts = SubElement(root, "fonts", count="2")
    for bold in [False, True]:
        font = SubElement(fonts, "font")
        SubElement(font, "sz", val="12")
        SubElement(font, "name", val="Calibri")
        if bold:
            SubElement(font, "b")
    fills = SubElement(root, "fills", count="3")
    for pattern in ["none", "gray125", "solid"]:
        fill = SubElement(SubElement(fills, "fill"), "patternFill", patternType=pattern)
        if pattern == "solid":
            SubElement(fill, "fgColor", rgb="FFEAF0F8")
    SubElement(SubElement(root, "borders", count="1"), "border")
    SubElement(
        SubElement(root, "cellStyleXfs", count="1"),
        "xf",
        numFmtId="0",
        fontId="0",
        fillId="0",
        borderId="0",
    )
    formats = SubElement(root, "cellXfs", count="3")
    for number, font, fill in [(0, 0, 0), (2, 0, 0), (0, 1, 2)]:
        xf = SubElement(
            formats,
            "xf",
            numFmtId=str(number),
            fontId=str(font),
            fillId=str(fill),
            borderId="0",
            xfId="0",
            applyAlignment="1",
        )
        SubElement(xf, "alignment", vertical="center", wrapText="1")
    SubElement(
        SubElement(root, "cellStyles", count="1"),
        "cellStyle",
        name="Normal",
        xfId="0",
        builtinId="0",
    )
    return xml(root)


def export(preview):
    columns = {}
    for student in preview.students:
        for q in student.questions:
            columns[str(q.mapping_id)] = f"{student.variant}卷·第{q.number}题"
    rows = [
        [
            "考试",
            "场次",
            "班级",
            "学号",
            "姓名",
            "考号",
            "卷别",
            *columns.values(),
            "已复核题数",
            "应复核题数",
            "已确认小计",
            "完整总分",
            "状态",
            "到考状态",
        ]
    ]
    labels = {
        "complete": "全部复核完成（未发布）",
        "incomplete": "未完成",
        "identity_pending": "身份待确认",
        "source_stale": "来源失效",
        "variant_unverified": "卷别待核验",
    }
    provenance = [
        [
            "参试身份",
            "名单版本",
            "答卷版本",
            "试卷版本",
            "题目映射",
            "复核记录",
            "复核版次",
            "题目状态",
        ]
    ]
    for student in preview.students:
        scores = {str(q.mapping_id): q.points for q in student.questions}
        rows.append(
            [
                preview.event_title,
                preview.session_title,
                student.class_name,
                student.student_code,
                student.student_name,
                student.exam_number,
                student.variant,
                *[scores.get(k) for k in columns],
                student.reviewed_count,
                student.required_count,
                student.subtotal,
                student.total,
                labels[student.status],
                "尚未确认到考/缺考",
            ]
        )
        for q in student.questions:
            provenance.append(
                [
                    student.participation_id,
                    preview.roster_revision_id,
                    student.script_version_id,
                    student.paper_version_id,
                    q.mapping_id,
                    q.review_id,
                    q.review_revision,
                    q.status,
                ]
            )
    numeric = set(range(8, 8 + len(columns))) | set(range(8 + len(columns), 12 + len(columns)))
    metadata = [
        ["项目", "内容"],
        ["性质", "内部教师预览，不是正式发布成绩"],
        ["生成时间UTC", preview.generated_at.isoformat()],
        ["一致性摘要", preview.snapshot_sha256],
        ["名单版本", preview.roster_revision_id],
        ["场次", preview.session_id],
        ["说明", "空白表示尚无有效复核分或不适用；0仅表示明确复核为零分。"],
        ["题目集合", "每份实际卷版的全部映射题目；未实现选做规则。"],
        ["保护", "工作表保护仅防误改，不是签名或成绩发布凭证。"],
    ]
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/styles.xml"
 ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/workbook.xml"
 ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml"
 ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml"
 ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet3.xml"
 ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        z.writestr(
            "_rels/.rels",
            """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1"
 Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
 Target="xl/workbook.xml"/></Relationships>""",
        )
        root = Element(
            "workbook",
            xmlns=NS,
            **{"xmlns:r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"},
        )
        sheets = SubElement(root, "sheets")
        for i, name in enumerate(["成绩预览", "来源版本", "导出说明"], 1):
            SubElement(sheets, "sheet", name=name, sheetId=str(i), **{"r:id": f"rId{i}"})
        z.writestr("xl/workbook.xml", xml(root))
        rels = Element(
            "Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships"
        )
        for i in range(1, 4):
            SubElement(
                rels,
                "Relationship",
                Id=f"rId{i}",
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
                Target=f"worksheets/sheet{i}.xml",
            )
        SubElement(
            rels,
            "Relationship",
            Id="rId4",
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            Target="styles.xml",
        )
        z.writestr("xl/styles.xml", styles())
        z.writestr("xl/_rels/workbook.xml.rels", xml(rels))
        z.writestr("xl/worksheets/sheet1.xml", sheet(rows, numeric))
        z.writestr("xl/worksheets/sheet2.xml", sheet(provenance))
        z.writestr("xl/worksheets/sheet3.xml", sheet(metadata))
    if buffer.tell() > 8 * 1024**2:
        from huipi_cloud.modules.identity.protocol import AccessError

        raise AccessError("result_export_size_limit", 413)
    return buffer.getvalue()
