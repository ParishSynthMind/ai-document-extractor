"""Command line: one file, or a whole folder in batch mode.

    extract invoice.pdf --schema invoice --out result.json
    extract invoice.pdf --schema invoice --out result.json \
        --fill-form forms/invoice_entry_form.pdf --form-out filled.pdf --review-copy review.pdf \
        --excel invoices.xlsx
    extract samples/invoices/ --schema invoice --csv invoices.csv --out-dir results/
    extract --from-json result.json --fill-form forms/invoice_entry_form.pdf --form-out filled.pdf
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extract import ExtractionResult, extract_file
from .loaders import SUPPORTED
from .outputs import append_excel_row, fill_pdf_form, write_batch_csv
from .schemas import available, load_schema


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="extract", description="Extract structured fields from PDF, Word and Excel files.")
    p.add_argument("path", nargs="?", help="a file, or a folder for batch mode")
    p.add_argument("--schema", help="schema name (see --list-schemas) or path to a .yaml/.json schema")
    p.add_argument("--out", help="single file: where to write the JSON result (default: print it)")
    p.add_argument("--out-dir", help="batch: also write one JSON per file into this folder")
    p.add_argument("--csv", help="batch: write one CSV row per file here")
    p.add_argument("--excel", help="append a row per document to this .xlsx workbook")
    p.add_argument("--fill-form", metavar="TEMPLATE", help="fillable PDF to write the values into (single file)")
    p.add_argument("--form-out", help="where to save the filled form")
    p.add_argument("--review-copy", help="also save a copy of the filled form with fields to check boxed in colour")
    p.add_argument("--model", default=None, help="Claude model id (default claude-sonnet-5-5)")
    p.add_argument("--from-json", metavar="RESULT", help="skip extraction: fill the form / Excel from a saved result")
    p.add_argument("--list-schemas", action="store_true", help="list the built-in schemas and exit")
    return p


def make_extractor(model: str | None):
    from .llm import DEFAULT_MODEL, AnthropicExtractor

    return AnthropicExtractor(model=model or DEFAULT_MODEL)


def summarise(result) -> str:
    counts = {"high": 0, "medium": 0, "low": 0}
    found = 0
    for fr in result.fields.values():
        if fr.value is not None:
            found += 1
            counts[fr.confidence] += 1
    line = (f"{result.file}: {found}/{len(result.fields)} fields found via {result.method} "
            f"(high {counts['high']}, medium {counts['medium']}, low {counts['low']})")
    if result.needs_review:
        line += "\n  needs review: " + "; ".join(f"{r['field']} ({r['reason']})" for r in result.needs_review)
    return line


def main(argv: list[str] | None = None, extractor=None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_schemas:
        print("\n".join(available()))
        return 0
    if args.from_json:
        result = ExtractionResult.from_dict(json.loads(Path(args.from_json).read_text()))
        if not (args.fill_form or args.excel):
            build_parser().error("--from-json needs --fill-form and/or --excel")
        _write_outputs(result, args, Path(result.file))
        return 0
    if not args.path or not args.schema:
        build_parser().error("a path and --schema are required")
    schema = load_schema(args.schema)
    target = Path(args.path)
    if not target.exists():
        print(f"not found: {target}", file=sys.stderr)
        return 1
    extractor = extractor or make_extractor(args.model)

    if target.is_dir():
        if args.fill_form:
            build_parser().error("--fill-form works on a single file, not a folder")
        files = sorted(f for f in target.iterdir() if f.suffix.lower() in SUPPORTED and not f.name.startswith("."))
        if not files:
            print(f"no supported files in {target}", file=sys.stderr)
            return 1
        results, errors = [], []
        out_dir = Path(args.out_dir) if args.out_dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            try:
                r = extract_file(f, schema, extractor)
            except Exception as exc:  # one bad file must not stop the batch
                errors.append({"file": f.name, "error": f"{type(exc).__name__}: {exc}"})
                print(f"{f.name}: FAILED ({exc})", file=sys.stderr)
                continue
            results.append(r)
            print(summarise(r), file=sys.stderr)
            if out_dir:
                (out_dir / f"{f.stem}.json").write_text(json.dumps(r.to_dict(), indent=2))
            if args.excel:
                append_excel_row(r, args.excel)
        if args.csv:
            write_batch_csv(results, args.csv, errors)
            print(f"wrote {args.csv} ({len(results)} ok, {len(errors)} failed)", file=sys.stderr)
        return 1 if errors else 0

    result = extract_file(target, schema, extractor)
    payload = json.dumps(result.to_dict(), indent=2)
    if args.out:
        Path(args.out).write_text(payload)
    else:
        print(payload)
    print(summarise(result), file=sys.stderr)
    _write_outputs(result, args, target)
    return 0


def _write_outputs(result, args, target: Path) -> None:
    if args.excel:
        row = append_excel_row(result, args.excel)
        print(f"added row {row} to {args.excel}", file=sys.stderr)
    if args.fill_form:
        form_out = args.form_out or f"{target.stem}_filled.pdf"
        info = fill_pdf_form(result, args.fill_form, form_out, review_copy=args.review_copy)
        print(f"filled {len(info['filled'])} form fields into {form_out}"
              + (f"; review copy {args.review_copy} flags {len(info['flagged'])}" if args.review_copy else ""),
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
