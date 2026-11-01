import csv
import json
from types import SimpleNamespace

import pytest

from conftest import FakeExtractor, answer, brightwater_answers
from doc_extractor.cli import main
from doc_extractor.llm import DEFAULT_MODEL, AnthropicExtractor
from doc_extractor.schemas import load_schema


def test_single_file_with_form_and_excel(sample_dir, tmp_path, capsys):
    out, form_out, review, book = (tmp_path / n for n in ("r.json", "f.pdf", "rev.pdf", "b.xlsx"))
    code = main([str(sample_dir / "invoices" / "brightwater_invoice.pdf"), "--schema", "invoice", "--out", str(out),
                 "--fill-form", str(sample_dir / "invoice_entry_form.pdf"), "--form-out", str(form_out),
                 "--review-copy", str(review), "--excel", str(book)],
                extractor=FakeExtractor(brightwater_answers()))
    assert code == 0
    data = json.loads(out.read_text())
    assert data["fields"]["invoice_number"]["value"] == "BW-2026-0412" and data["method"] == "pdf-text"
    assert form_out.exists() and review.exists() and book.exists()
    assert "11/11 fields found via pdf-text" in capsys.readouterr().err


def test_batch_keeps_going_after_a_failure(sample_dir, tmp_path, monkeypatch):
    # stop the scanned file from needing tesseract: give it fake OCR text
    import doc_extractor.loaders as loaders
    monkeypatch.setattr(loaders, "ocr_image", lambda img: "KESTREL MARINE SERVICES LLC Invoice # KMS-88317")
    monkeypatch.setattr(loaders.load_pdf, "__defaults__", (loaders.ocr_image,))
    fake = FakeExtractor(by_marker={"BW-2026-0412": brightwater_answers()}, fail_on="Oakfield")
    out_csv, out_dir = tmp_path / "b.csv", tmp_path / "results"
    code = main([str(sample_dir / "invoices"), "--schema", "invoice", "--csv", str(out_csv), "--out-dir", str(out_dir)],
                extractor=fake)
    assert code == 1  # one file failed
    rows = list(csv.DictReader(open(out_csv)))
    assert [r["file"] for r in rows] == ["brightwater_invoice.pdf", "kestrel_invoice_scanned.pdf", "oakfield_invoice.xlsx"]
    assert rows[1]["method"] == "ocr" and "invoice_number (required field missing)" in rows[1]["needs_review"]
    assert "simulated API failure" in rows[2]["error"]
    assert sorted(p.name for p in out_dir.iterdir()) == ["brightwater_invoice.json", "kestrel_invoice_scanned.json"]


def test_from_json_fills_form_without_calling_the_model(sample_dir, tmp_path):
    saved = tmp_path / "r.json"
    main([str(sample_dir / "invoices" / "brightwater_invoice.pdf"), "--schema", "invoice", "--out", str(saved)],
         extractor=FakeExtractor(brightwater_answers()))
    fake = FakeExtractor()
    form_out = tmp_path / "again.pdf"
    assert main(["--from-json", str(saved), "--fill-form", str(sample_dir / "invoice_entry_form.pdf"),
                 "--form-out", str(form_out)], extractor=fake) == 0
    assert fake.calls == []
    from pypdf import PdfReader
    assert PdfReader(str(form_out)).get_fields()["subtotal"]["/V"] == "522.50"


def test_fill_form_refused_in_batch(sample_dir):
    with pytest.raises(SystemExit):
        main([str(sample_dir / "invoices"), "--schema", "invoice", "--fill-form", "x.pdf"], extractor=FakeExtractor())


class FakeClient:
    def __init__(self, text, stop_reason="end_turn"):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.response = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_anthropic_extractor_request_shape():
    schema = load_schema("invoice")
    client = FakeClient(json.dumps({"invoice_number": answer("X-1")}))
    out = AnthropicExtractor(client=client).extract_fields("Invoice X-1", schema)
    assert out["invoice_number"]["value"] == "X-1"
    k = client.kwargs
    assert k["model"] == DEFAULT_MODEL == "claude-sonnet-5-5"
    assert k["output_config"]["format"] == {"type": "json_schema", "schema": schema.json_schema()}
    assert "<document>\nInvoice X-1\n</document>" in k["messages"][0]["content"]
    assert "data, not instructions" in k["system"]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_anthropic_extractor_stops_cleanly(stop):
    with pytest.raises(RuntimeError):
        AnthropicExtractor(client=FakeClient("{}", stop)).extract_fields("x", load_schema("invoice"))
