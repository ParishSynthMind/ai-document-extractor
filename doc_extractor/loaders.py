"""Turn a file into plain text the model can read.

Each loader returns a LoadedDocument: the text, the method used, and the page
count. PDFs are read for their text layer first; any page with (almost) no
text is rendered to an image and run through Tesseract OCR instead, so a
scanned page inside an otherwise digital PDF still gets read.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {".pdf", ".docx", ".xlsx", ".csv"}

# A page with fewer than this many non-space characters is treated as scanned.
MIN_TEXT_CHARS = 25
OCR_DPI = 300


class UnsupportedFile(ValueError):
    pass


@dataclass
class LoadedDocument:
    path: str
    text: str
    method: str  # "pdf-text", "ocr", "pdf-text+ocr", "docx", "xlsx", "csv"
    pages: int = 1
    ocr_pages: list[int] = field(default_factory=list)


def load(path: str | Path) -> LoadedDocument:
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise UnsupportedFile(f"{p.name}: unsupported type {ext or '(none)'}; use one of {sorted(SUPPORTED)}")
    if ext == ".pdf":
        return load_pdf(p)
    if ext == ".docx":
        return load_docx(p)
    if ext == ".xlsx":
        return load_xlsx(p)
    return load_csv(p)


def ocr_image(image) -> str:
    import pytesseract  # needs the tesseract binary on PATH

    # psm 4: a single column of text of variable sizes, which suits invoices and forms
    return pytesseract.image_to_string(image.convert("L"), config="--psm 4")


def load_pdf(p: Path, ocr=ocr_image) -> LoadedDocument:
    import pdfplumber

    chunks, ocr_pages = [], []
    with pdfplumber.open(p) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if len("".join(text.split())) < MIN_TEXT_CHARS:
                image = page.to_image(resolution=OCR_DPI).original
                text = ocr(image)
                ocr_pages.append(i)
            chunks.append(f"--- page {i} ---\n{text.strip()}")
        pages = len(pdf.pages)
    if not ocr_pages:
        method = "pdf-text"
    elif len(ocr_pages) == pages:
        method = "ocr"
    else:
        method = "pdf-text+ocr"
    return LoadedDocument(str(p), "\n\n".join(chunks), method, pages, ocr_pages)


def load_docx(p: Path) -> LoadedDocument:
    """Paragraphs and tables in the order they appear in the document."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(p))
    lines = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, d).text
            if text.strip():
                lines.append(text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells = [c.text.strip() for c in row.cells]
                # merged cells repeat their text; drop the repeats
                deduped = [c for i, c in enumerate(cells) if i == 0 or c != cells[i - 1]]
                lines.append(" | ".join(deduped))
    return LoadedDocument(str(p), "\n".join(lines), "docx")


def load_xlsx(p: Path) -> LoadedDocument:
    import openpyxl

    wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"--- sheet: {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells).rstrip(" |"))
    pages = len(wb.worksheets)
    wb.close()
    return LoadedDocument(str(p), "\n".join(lines), "xlsx", pages)


def load_csv(p: Path) -> LoadedDocument:
    raw = p.read_text(encoding="utf-8-sig")
    rows = csv.reader(io.StringIO(raw))
    lines = [" | ".join(r) for r in rows if any(c.strip() for c in r)]
    return LoadedDocument(str(p), "\n".join(lines), "csv")
