from conftest import FakeExtractor, answer, brightwater_answers
from doc_extractor.extract import extract_document, extract_file, snippet_in_document
from doc_extractor.loaders import LoadedDocument
from doc_extractor.schemas import load_schema

INVOICE = load_schema("invoice")


def test_clean_document_has_nothing_to_review(sample_dir):
    r = extract_file(sample_dir / "invoices" / "brightwater_invoice.pdf", INVOICE, FakeExtractor(brightwater_answers()))
    assert r.needs_review == []
    assert r.values()["total_amount"] == 627.0
    assert r.fields["currency"].confidence == "medium"  # model's own judgement is kept when checks pass
    assert all(not f.checks for f in r.fields.values())


def _doc(text):
    return LoadedDocument("x.pdf", text, "pdf-text")


def test_invented_snippet_is_downgraded_and_sent_to_review():
    answers = brightwater_answers()
    answers["purchase_order"] = answer("PO-99999", "high", snippet="Purchase order PO-99999")
    doc = _doc("Brightwater Office Supplies Ltd\nInvoice No: BW-2026-0412\nYour PO: PO-77215")
    r = extract_document(doc, INVOICE, FakeExtractor(answers))
    po = r.fields["purchase_order"]
    assert po.confidence == "low"
    assert "source snippet not found in the document" in po.checks
    assert any(x["field"] == "purchase_order" for x in r.needs_review)


def test_value_that_differs_from_its_snippet_loses_a_level():
    # OCR said AEO7 (letter O); a model that silently "corrects" it to AE07 gets marked down
    doc = _doc("IBAN: AEO7 0331 2345 6789 0123 456")
    answers = {f.name: answer(None) for f in INVOICE.fields}
    answers["bank_iban"] = answer("AE070331234567890123456", "high", snippet="IBAN: AEO7 0331 2345 6789 0123 456")
    r = extract_document(doc, INVOICE, FakeExtractor(answers))
    assert r.fields["bank_iban"].confidence == "medium"
    assert r.fields["bank_iban"].checks == ["value not found word for word in the document"]


def test_bad_date_is_low():
    answers = brightwater_answers()
    answers["due_date"] = answer("13/11/2026", "high", snippet="Payment Due: 13 November 2026")
    doc = _doc("Payment Due: 13 November 2026")
    r = extract_document(doc, INVOICE, FakeExtractor(answers))
    assert r.fields["due_date"].confidence == "low"
    assert any("YYYY-MM-DD" in c for c in r.fields["due_date"].checks)


def test_missing_required_field_goes_to_review_but_missing_optional_does_not():
    answers = {f.name: answer(None) for f in INVOICE.fields}
    r = extract_document(_doc("blank page"), INVOICE, FakeExtractor(answers))
    flagged = {x["field"]: x["reason"] for x in r.needs_review}
    assert flagged["invoice_number"] == "required field missing"
    assert "due_date" not in flagged


def test_model_low_confidence_is_respected():
    answers = brightwater_answers()
    answers["supplier_vat_number"]["confidence"] = "low"
    doc = _doc("VAT No: GB 123 4567 89")
    r = extract_document(doc, INVOICE, FakeExtractor(answers))
    assert {"field": "supplier_vat_number", "reason": "model marked it low confidence"} in r.needs_review


def test_field_missing_from_reply_is_treated_as_empty():
    r = extract_document(_doc("x"), INVOICE, FakeExtractor({}))
    assert all(f.value is None for f in r.fields.values())


def test_snippet_matching_tolerates_spacing_and_small_ocr_noise():
    doc = "Bill to: Example Clinic Group Ltd .Invoice #. KMS-88317"
    assert snippet_in_document("Invoice #  KMS-88317", doc)
    assert snippet_in_document("invoice # kms-88317", doc)
    assert not snippet_in_document("Invoice # KMS-11111 dated yesterday", doc)
    assert not snippet_in_document("", doc)


def test_seen_but_unreadable_field_goes_to_review():
    # found live: a smudged PO number came back as null + low + the smudged snippet
    answers = {f.name: answer(None) for f in INVOICE.fields}
    answers["purchase_order"] = {"value": None, "confidence": "low", "source_snippet": "PO-Ref. . PO- 7am"}
    r = extract_document(_doc("PO-Ref. . PO- 7am"), INVOICE, FakeExtractor(answers))
    assert {"field": "purchase_order", "reason": "in the document but unreadable"} in r.needs_review
