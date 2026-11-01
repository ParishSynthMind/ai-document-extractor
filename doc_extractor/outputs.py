"""Where extracted data goes: a fillable PDF form, an Excel row, a batch CSV.

Form fields are matched to schema fields by name, so a form built for the
invoice schema has fields called supplier_name, invoice_number, and so on.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .extract import ExtractionResult

# Review copy colours (RGB hex for annotations, ARGB for Excel fills)
REVIEW_COLOURS = {"low": "ff3b30", "medium": "ffb020"}
EXCEL_FILLS = {"low": "FFFFC7CE", "medium": "FFFFEB9C"}


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return f"{value:.2f}"  # every number field in the built-in schemas is money
    return str(value)


def form_field_names(template: str | Path) -> list[str]:
    from pypdf import PdfReader

    return sorted((PdfReader(str(template)).get_fields() or {}).keys())


def fill_pdf_form(result: ExtractionResult, template: str | Path, out: str | Path,
                  review_copy: str | Path | None = None) -> dict:
    """Write the extracted values into the form. Returns what was filled and skipped.

    If review_copy is given, a second PDF is saved with a coloured box around
    every field that needs a look: red for low confidence or a missing required
    field, amber for medium.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(template))
    form_fields = set((reader.get_fields() or {}).keys())
    values = {k: _as_text(fr.value) for k, fr in result.fields.items() if k in form_fields}
    writer = PdfWriter(clone_from=reader)
    for page in writer.pages:
        writer.update_page_form_field_values(page, values, auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    with open(out, "wb") as fh:
        writer.write(fh)

    review = {r["field"] for r in result.needs_review}
    flagged = {}
    for name, fr in result.fields.items():
        if name in review:
            flagged[name] = "low"
        elif fr.confidence == "medium" and fr.value is not None:
            flagged[name] = "medium"

    if review_copy:
        _write_review_copy(writer, flagged, result, review_copy)

    return {
        "filled": sorted(k for k, v in values.items() if v),
        "not_in_form": sorted(set(result.fields) - form_fields),
        "flagged": flagged,
    }


def _write_review_copy(writer, flagged: dict, result: ExtractionResult, out) -> None:
    from pypdf.annotations import Rectangle
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, TextStringObject

    for page_index, page in enumerate(writer.pages):
        for annot_ref in list(page.get("/Annots") or []):
            annot = annot_ref.get_object()
            name = annot.get("/T")
            if name not in flagged:
                continue
            x1, y1, x2, y2 = [float(v) for v in annot["/Rect"]]
            colour = REVIEW_COLOURS[flagged[name]]
            box = Rectangle(rect=(x1 - 2, y1 - 2, x2 + 2, y2 + 2), interior_color=None)
            rgb = [int(colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            box[NameObject("/C")] = ArrayObject([FloatObject(c) for c in rgb])
            box[NameObject("/BS")] = DictionaryObject({NameObject("/W"): NumberObject(2)})
            fr = result.fields[name]
            note = next((r["reason"] for r in result.needs_review if r["field"] == name), None)
            box[NameObject("/Contents")] = TextStringObject(
                f"{name}: {flagged[name]} confidence. {note or '; '.join(fr.checks) or 'Check against the source.'}"
            )
            writer.add_annotation(page_number=page_index, annotation=box)
    with open(out, "wb") as fh:
        writer.write(fh)


def append_excel_row(result: ExtractionResult, workbook: str | Path, sheet: str | None = None) -> int:
    """Append one row per document to a workbook (created with a header if new).

    Low-confidence cells are filled red, medium amber. Returns the row number.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    sheet = sheet or result.schema
    header = ["file", *result.fields.keys(), "needs_review", "extracted_at"]
    path = Path(workbook)
    wb = openpyxl.load_workbook(path) if path.exists() else openpyxl.Workbook()
    if sheet in wb.sheetnames:
        ws = wb[sheet]
    elif wb.sheetnames == ["Sheet"] and wb["Sheet"].max_row == 1 and wb["Sheet"].cell(1, 1).value is None:
        ws = wb["Sheet"]
        ws.title = sheet
    else:
        ws = wb.create_sheet(sheet)
    if ws.max_row == 1 and ws.cell(1, 1).value is None:
        for col, name in enumerate(header, start=1):
            ws.cell(1, col, name).font = Font(bold=True)
    review = ", ".join(r["field"] for r in result.needs_review)
    row = ws.max_row + 1
    for col, value in enumerate([result.file, *(fr.value for fr in result.fields.values()), review,
                                 result.extracted_at], start=1):
        ws.cell(row, col, value)
    for col, fr in enumerate(result.fields.values(), start=2):
        if fr.value is not None and fr.confidence in EXCEL_FILLS:
            ws.cell(row=row, column=col).fill = PatternFill("solid", fgColor=EXCEL_FILLS[fr.confidence])
    wb.save(path)
    return row


def write_batch_csv(results: list[ExtractionResult], out: str | Path, errors: list[dict] | None = None) -> None:
    if not results and not errors:
        raise ValueError("nothing to write")
    field_names = list(results[0].fields) if results else []
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "method", *field_names, *(f"{n}_confidence" for n in field_names),
                    "needs_review", "error"])
        for r in results:
            w.writerow([
                r.file, r.method,
                *(_as_text(r.fields[n].value) for n in field_names),
                *(r.fields[n].confidence for n in field_names),
                "; ".join(f"{x['field']} ({x['reason']})" for x in r.needs_review),
                "",
            ])
        for e in errors or []:
            w.writerow([e["file"], "", *([""] * 2 * len(field_names)), "", e["error"]])
