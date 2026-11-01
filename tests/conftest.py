import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = ROOT / "samples" / "expected"


def _load_make_samples():
    spec = importlib.util.spec_from_file_location("make_samples", ROOT / "samples" / "make_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def make_samples():
    return _load_make_samples()


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory, make_samples):
    """A fresh copy of every sample document, built from the generator."""
    d = tmp_path_factory.mktemp("samples")
    (d / "invoices").mkdir()
    make_samples.brightwater_pdf(d / "invoices" / "brightwater_invoice.pdf")
    make_samples.kestrel_scanned_pdf(d / "invoices" / "kestrel_invoice_scanned.pdf")
    make_samples.oakfield_xlsx(d / "invoices" / "oakfield_invoice.xlsx")
    make_samples.riverside_docx(d / "permit_application_riverside.docx")
    make_samples.credential_csv(d / "staff_credential_export.csv")
    make_samples.invoice_form(d / "invoice_entry_form.pdf")
    return d


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def answer(value, confidence="high", snippet=None):
    return {"value": value, "confidence": confidence,
            "source_snippet": snippet if snippet is not None else ("" if value is None else str(value))}


def brightwater_answers():
    """What a well-behaved model would return for the Brightwater invoice."""
    return {
        "supplier_name": answer("Brightwater Office Supplies Ltd"),
        "invoice_number": answer("BW-2026-0412", snippet="Invoice No: BW-2026-0412"),
        "invoice_date": answer("2026-10-14", snippet="Invoice Date: 14 October 2026"),
        "due_date": answer("2026-11-13", snippet="Payment Due: 13 November 2026"),
        "purchase_order": answer("PO-77215", snippet="Your PO: PO-77215"),
        "currency": answer("GBP", "medium", snippet="Total due GBP 627.00"),
        "subtotal": answer(522.5, snippet="Subtotal GBP 522.50"),
        "tax_amount": answer(104.5, snippet="VAT @ 20% GBP 104.50"),
        "total_amount": answer(627.0, snippet="Total due GBP 627.00"),
        "supplier_vat_number": answer("GB123456789", snippet="VAT No: GB 123 4567 89"),
        "bank_iban": answer("GB82WEST12345698765432", snippet="IBAN: GB82 WEST 1234 5698 7654 32"),
    }


class FakeExtractor:
    """Stands in for Claude. Returns canned answers and records each call."""

    def __init__(self, answers=None, by_marker=None, fail_on=None):
        self.answers = answers
        self.by_marker = by_marker or {}
        self.fail_on = fail_on
        self.calls = []

    def extract_fields(self, document_text, schema):
        self.calls.append((document_text, schema.name))
        if self.fail_on and self.fail_on in document_text:
            raise RuntimeError("simulated API failure")
        for marker, ans in self.by_marker.items():
            if marker in document_text:
                return ans
        if self.answers is not None:
            return self.answers
        return {f.name: answer(None) for f in schema.fields}


@pytest.fixture
def expected():
    return lambda stem: json.loads((EXPECTED / f"{stem}.json").read_text())
