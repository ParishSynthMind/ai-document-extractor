"""Document-type schemas: which fields to pull out, and what type each one is.

A schema is a YAML (or JSON) file in schemas/. Adding a new document type is
a new file, no code change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

FIELD_TYPES = {"string", "number", "date", "boolean"}
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "schemas"


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    description: str
    required: bool = False


@dataclass(frozen=True)
class DocSchema:
    name: str
    description: str
    fields: tuple[FieldSpec, ...]

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def json_schema(self) -> dict:
        """The JSON schema Claude's reply is constrained to.

        Every field returns the same shape: the value (or null if the document
        doesn't contain it), a confidence level and the exact snippet it came from.
        """
        value_types = {"string": "string", "date": "string", "number": "number", "boolean": "boolean"}
        props = {}
        for f in self.fields:
            props[f.name] = {
                "type": "object",
                "description": f.description,
                "properties": {
                    "value": {"anyOf": [{"type": value_types[f.type]}, {"type": "null"}]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "source_snippet": {"type": "string"},
                },
                "required": ["value", "confidence", "source_snippet"],
                "additionalProperties": False,
            }
        return {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        }


def parse_schema(data: dict, origin: str = "<schema>") -> DocSchema:
    if not isinstance(data, dict) or "name" not in data or "fields" not in data:
        raise SchemaError(f"{origin}: needs 'name' and 'fields'")
    fields, seen = [], set()
    for raw in data["fields"]:
        name = str(raw.get("name", "")).strip()
        if not name.isidentifier():
            raise SchemaError(f"{origin}: field name {name!r} must be a simple identifier")
        if name in seen:
            raise SchemaError(f"{origin}: duplicate field {name!r}")
        ftype = raw.get("type", "string")
        if ftype not in FIELD_TYPES:
            raise SchemaError(f"{origin}: field {name!r} has unknown type {ftype!r}")
        seen.add(name)
        fields.append(FieldSpec(name, ftype, str(raw.get("description", "")), bool(raw.get("required", False))))
    if not fields:
        raise SchemaError(f"{origin}: no fields")
    return DocSchema(str(data["name"]), str(data.get("description", "")), tuple(fields))


def load_schema(name_or_path: str, schema_dir: Path = DEFAULT_DIR) -> DocSchema:
    p = Path(name_or_path)
    if not p.suffix:
        for ext in (".yaml", ".yml", ".json"):
            if (schema_dir / f"{name_or_path}{ext}").exists():
                p = schema_dir / f"{name_or_path}{ext}"
                break
        else:
            raise SchemaError(f"no schema called {name_or_path!r} in {schema_dir} (have: {', '.join(available(schema_dir))})")
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    return parse_schema(data, str(p.name))


def available(schema_dir: Path = DEFAULT_DIR) -> list[str]:
    return sorted(p.stem for p in schema_dir.glob("*") if p.suffix in (".yaml", ".yml", ".json"))
