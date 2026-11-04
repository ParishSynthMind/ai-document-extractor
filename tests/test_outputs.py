import csv

import openpyxl
from conftest import FakeExtractor, answer, brightwater_answers
from pypdf import PdfReader

from doc_extractor.extract import extract_file
from doc_extractor.outputs import append_excel_row, fill_pdf_form, form_field_names, write_batch_csv
from doc_extractor.schemas import load_schema

INVOICE = load_schema("invoice")


def _result(sample_dir, **overrides):
    answers = brightwater_answers()
    answers.update(overrides)
    return extract_file(sample_dir / "invoices" / "brightwater_invoice.pdf", INVOICE, FakeExtractor(answers))


def test_form_fields_match_the_invoice_schema(sample_dir):
    assert form_field_names(sample_dir / "invoice_entry_form.pdf") == sorted(INVOICE.field_names)


def test_fill_form_writes_values(sample_dir, tmp_path):
    r = _result(sample_dir)
    out = tmp_path / "filled.pdf"
    info = fill_pdf_form(r, sample_dir / "invoice_entry_form.pdf", out)
    fields = PdfReader(str(out)).get_fields()
    assert fields["invoice_number"]["/V"] == "BW-2026-0412"
    assert fields["total_amount"]["/V"] == "627.00"
    assert len(info["filled"]) == 11 and info["not_in_form"] == [] and info["flagged"] == {"currency": "medium"}


def test_review_copy_boxes_the_doubtful_fields(sample_dir, tmp_path):
    r = _result(sample_dir, purchase_order=answer("PO-00000", "high", snippet="made up"))
    review = tmp_path / "review.pdf"
    info = fill_pdf_form(r, sample_dir / "invoice_entry_form.pdf", tmp_path / "f.pdf", review_copy=review)
    assert info["flagged"] == {"purchase_order": "low", "currency": "medium"}
    boxes = [a.get_object() for a in PdfReader(str(review)).pages[0]["/Annots"]
             if a.get_object()["/Subtype"] == "/Square"]
    notes = sorted(str(b["/Contents"]) for b in boxes)
    assert len(boxes) == 2
    assert notes[1].startswith("purchase_order: low confidence. source snippet not found")
    red = [b for b in boxes if [round(float(c), 2) for c in b["/C"]] == [1.0, 0.23, 0.19]]
    assert len(red) == 1


def test_excel_rows_append_with_colours(sample_dir, tmp_path):
    book = tmp_path / "invoices.xlsx"
    r1 = _result(sample_dir)
    r2 = _result(sample_dir, bank_iban=answer("GB00FAKE", "high", snippet="nowhere"))
    assert append_excel_row(r1, book) == 2
    assert append_excel_row(r2, book) == 3
    ws = openpyxl.load_workbook(book)["invoice"]
    assert ws["A1"].value == "file" and ws.cell(1, ws.max_column - 1).value == "needs_review"
    iban_col = ["file", *INVOICE.field_names].index("bank_iban") + 1
    assert ws.cell(3, iban_col).fill.fgColor.rgb == "FFFFC7CE"
    assert ws.cell(3, ws.max_column - 1).value == "bank_iban"
    assert ws.cell(2, ws.max_column - 1).value in ("", None)


def test_batch_csv(sample_dir, tmp_path):
    out = tmp_path / "batch.csv"
    write_batch_csv([_result(sample_dir)], out, errors=[{"file": "broken.pdf", "error": "boom"}])
    rows = list(csv.DictReader(open(out)))
    assert rows[0]["invoice_number"] == "BW-2026-0412" and rows[0]["total_amount"] == "627.00"
    assert rows[0]["currency_confidence"] == "medium"
    assert rows[1]["file"] == "broken.pdf" and rows[1]["error"] == "boom"
