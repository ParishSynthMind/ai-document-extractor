import pytest
from conftest import tesseract_available

from doc_extractor.loaders import UnsupportedFile, load, load_pdf


def test_digital_pdf_uses_text_layer(sample_dir):
    d = load(sample_dir / "invoices" / "brightwater_invoice.pdf")
    assert d.method == "pdf-text" and d.ocr_pages == []
    assert "BW-2026-0412" in d.text and "Total due GBP 627.00" in d.text


def test_scanned_pdf_has_no_text_layer_and_goes_to_ocr(sample_dir):
    seen = []

    def fake_ocr(image):
        seen.append(image.size)
        return "KESTREL MARINE SERVICES LLC (fake OCR)"

    d = load_pdf(sample_dir / "invoices" / "kestrel_invoice_scanned.pdf", ocr=fake_ocr)
    assert d.method == "ocr" and d.ocr_pages == [1]
    assert len(seen) == 1 and seen[0][0] > 2000  # rendered at 300 dpi
    assert "fake OCR" in d.text


@pytest.mark.skipif(not tesseract_available(), reason="tesseract not installed")
def test_real_ocr_reads_the_scan(sample_dir):
    d = load(sample_dir / "invoices" / "kestrel_invoice_scanned.pdf")
    assert d.method == "ocr"
    for expected in ("KESTREL", "KMS-88317", "4,900.00", "245.00"):
        assert expected in d.text


def test_docx_keeps_document_order(sample_dir):
    text = load(sample_dir / "permit_application_riverside.docx").text
    assert text.index("Building Permit") < text.index("Riverside Bakery Partners") < text.index("Declaration")
    assert "11. Contractor licence / registration no." in text


def test_xlsx_and_csv(sample_dir):
    x = load(sample_dir / "invoices" / "oakfield_invoice.xlsx")
    assert x.method == "xlsx" and "Total GBP |  |  | 453.6" in x.text
    c = load(sample_dir / "staff_credential_export.csv")
    assert c.method == "csv" and "Registration number | RN-4471902" in c.text


def test_unsupported_file(tmp_path):
    p = tmp_path / "photo.heic"
    p.write_bytes(b"x")
    with pytest.raises(UnsupportedFile):
        load(p)
