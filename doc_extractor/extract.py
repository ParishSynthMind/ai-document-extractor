"""Load a document, ask the model for the fields, then check the answer.

The model's own confidence is only the starting point. Every field is then
checked in code against the document text:
- the source snippet must really be in the document,
- for text fields, the value itself must be in the document,
- dates must be real dates in YYYY-MM-DD form,
- required fields must not be empty.
A failed check lowers the confidence and records why. Anything that ends up
"low", a missing required field, or a field the model could see but not read,
goes on the needs_review list.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .loaders import LoadedDocument, load
from .schemas import DocSchema

LEVELS = ["low", "medium", "high"]
SNIPPET_TOKEN_MATCH = 0.8  # share of snippet words that must appear in the document


@dataclass
class FieldResult:
    value: object
    confidence: str
    source_snippet: str
    checks: list[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    file: str
    schema: str
    method: str
    pages: int
    fields: dict[str, FieldResult]
    needs_review: list[dict]
    extracted_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractionResult":
        """Rebuild a result from its saved JSON, e.g. to fill a form later without re-extracting."""
        fields = {k: FieldResult(**v) for k, v in data["fields"].items()}
        return cls(**{**data, "fields": fields})

    def values(self) -> dict:
        return {k: f.value for k, f in self.fields.items()}


def _norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def _compact(text: str) -> str:
    return re.sub(r"[\s\-_.,:;/]", "", str(text).lower())


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text).lower())


def snippet_in_document(snippet: str, doc_text: str) -> bool:
    if not snippet.strip():
        return False
    if _norm(snippet) in _norm(doc_text):
        return True
    words = _words(snippet)
    if not words:
        return False
    doc_words = set(_words(doc_text))
    return sum(w in doc_words for w in words) / len(words) >= SNIPPET_TOKEN_MATCH


def _lower(level: str, steps: int = 1) -> str:
    return LEVELS[max(0, LEVELS.index(level) - steps)]


def check_field(spec, raw: dict, doc_text: str) -> FieldResult:
    value = raw.get("value")
    confidence = raw.get("confidence") if raw.get("confidence") in LEVELS else "low"
    snippet = raw.get("source_snippet") or ""
    fr = FieldResult(value, confidence, snippet)

    if value is None or (isinstance(value, str) and not value.strip()):
        fr.value = None
        return fr

    if not snippet_in_document(snippet, doc_text):
        fr.confidence = "low"
        fr.checks.append("source snippet not found in the document")

    if spec.type == "string" and _compact(value) not in _compact(doc_text):
        fr.confidence = _lower(fr.confidence)
        fr.checks.append("value not found word for word in the document")

    if spec.type == "date":
        try:
            date.fromisoformat(str(value))
        except ValueError:
            fr.confidence = "low"
            fr.checks.append(f"not a valid YYYY-MM-DD date: {value!r}")

    if spec.type == "number" and not isinstance(value, (int, float)):
        fr.confidence = "low"
        fr.checks.append(f"not a number: {value!r}")

    return fr


def review_reasons(schema: DocSchema, fields: dict[str, FieldResult]) -> list[dict]:
    out = []
    for spec in schema.fields:
        fr = fields[spec.name]
        if fr.value is None and fr.confidence == "low" and fr.source_snippet.strip():
            # the model saw the field but could not read it (smudge, bad scan)
            out.append({"field": spec.name, "reason": "in the document but unreadable"})
        elif spec.required and fr.value is None:
            out.append({"field": spec.name, "reason": "required field missing"})
        elif fr.value is not None and fr.confidence == "low":
            reason = "; ".join(fr.checks) or "model marked it low confidence"
            out.append({"field": spec.name, "reason": reason})
    return out


def extract_document(doc: LoadedDocument, schema: DocSchema, extractor) -> ExtractionResult:
    raw = extractor.extract_fields(doc.text, schema)
    fields = {}
    for spec in schema.fields:
        item = raw.get(spec.name) or {"value": None, "confidence": "low", "source_snippet": ""}
        fields[spec.name] = check_field(spec, item, doc.text)
    return ExtractionResult(
        file=Path(doc.path).name,
        schema=schema.name,
        method=doc.method,
        pages=doc.pages,
        fields=fields,
        needs_review=review_reasons(schema, fields),
        extracted_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def extract_file(path, schema: DocSchema, extractor) -> ExtractionResult:
    return extract_document(load(path), schema, extractor)
