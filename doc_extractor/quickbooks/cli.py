"""Command line: push invoices into QuickBooks Online as Bills.

    qbo-push samples/invoices_us/summit_it_invoice.pdf --out-dir qbo-results/ --dry-run
    qbo-push qbo-results/summit_it_invoice.qbo-input.json --out-dir qbo-results/
    qbo-push --delete-bill 145

A document is extracted first (header fields + line items, two Claude calls)
and the extraction is saved as <name>.qbo-input.json in --out-dir, so a dry
run and the real push can use exactly the same data without calling Claude
again. Pass that .json instead of the document to skip extraction.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..extract import ExtractionResult
from ..schemas import load_schema
from .bills import BillPusher, load_account_rules, plan_bill, summary_text
from .client import QBOClient, QBOError, ReauthorisationNeeded
from .line_items import AnthropicLineItemExtractor, LineItem, check_line_items

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ACCOUNTS = ROOT / "config" / "qbo_accounts.yaml"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qbo-push", description="Create QuickBooks Online Bills from invoices.")
    p.add_argument("inputs", nargs="*", help="invoice documents, or saved .qbo-input.json files")
    p.add_argument("--dry-run", action="store_true", help="show the Bill payload; make no QuickBooks call")
    p.add_argument("--out-dir", default="qbo-results", help="where extractions and run summaries are saved")
    p.add_argument("--review-file", default=None, help="CSV that held invoices are appended to "
                                                       "(default <out-dir>/review_list.csv)")
    p.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS), help="account mapping YAML")
    p.add_argument("--home-currency", default="USD", help="dry run only: the company's home currency")
    p.add_argument("--label", default="", help="text put at the start of each Bill's private note")
    p.add_argument("--environment", choices=["sandbox", "production"], default="sandbox")
    p.add_argument("--store", choices=["keychain", "file"], default=os.environ.get("QBO_TOKEN_STORE", "file"),
                   help="where credentials live (default: env vars + rotating token file)")
    p.add_argument("--keychain-account", default=os.environ.get("QBO_KEYCHAIN_ACCOUNT", "qbo"))
    p.add_argument("--token-file", default=os.environ.get("QBO_TOKEN_FILE", ".qbo_tokens.json"))
    p.add_argument("--delete-bill", metavar="ID", help="delete one Bill by id (to clean up test records)")
    p.add_argument("--model", default=None, help="Claude model id (default claude-sonnet-5-5)")
    return p


def make_store(args):
    from .tokens import FileTokenStore, KeychainTokenStore

    if args.store == "keychain":
        return KeychainTokenStore(account=args.keychain_account)
    return FileTokenStore(args.token_file)


def load_bundle(path: Path) -> tuple[ExtractionResult, list[LineItem]]:
    data = json.loads(path.read_text())
    result = ExtractionResult.from_dict(data["extraction"])
    return result, [LineItem(**li) for li in data["line_items"]]


def extract_bundle(path: Path, out_dir: Path, field_extractor, line_extractor) -> tuple[ExtractionResult, list[LineItem]]:
    from ..extract import extract_document
    from ..loaders import load

    doc = load(path)
    result = extract_document(doc, load_schema("invoice"), field_extractor)
    lines = check_line_items(line_extractor.extract_lines(doc.text), doc.text)
    bundle = {"extraction": result.to_dict(), "line_items": [li.to_dict() for li in lines]}
    (out_dir / f"{path.stem}.qbo-input.json").write_text(json.dumps(bundle, indent=2))
    return result, lines


def main(argv: list[str] | None = None, client=None, field_extractor=None, line_extractor=None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.delete_bill:
        client = client or QBOClient(make_store(args), args.environment)
        bill = client.read("Bill", args.delete_bill)
        client.delete("Bill", bill["Id"], bill["SyncToken"])
        print(f"deleted Bill {bill['Id']} ({bill.get('DocNumber')})")
        return 0
    if not args.inputs:
        build_parser().error("give at least one invoice document or .qbo-input.json")

    rules = load_account_rules(args.accounts)
    if not args.dry_run and client is None:
        client = QBOClient(make_store(args), args.environment)
    pusher = BillPusher(client, rules, review_file=args.review_file or out_dir / "review_list.csv", label=args.label)

    outcomes, failed = [], 0
    for name in args.inputs:
        path = Path(name)
        try:
            if path.name.endswith(".qbo-input.json"):
                result, lines = load_bundle(path)
            else:
                if field_extractor is None:
                    from ..llm import DEFAULT_MODEL, AnthropicExtractor

                    field_extractor = AnthropicExtractor(model=args.model or DEFAULT_MODEL)
                    line_extractor = line_extractor or AnthropicLineItemExtractor(model=args.model or DEFAULT_MODEL)
                result, lines = extract_bundle(path, out_dir, field_extractor, line_extractor)
            home = args.home_currency if args.dry_run else pusher.home_currency()
            outcome = pusher.push(plan_bill(result, lines, rules, home), dry_run=args.dry_run)
        except ReauthorisationNeeded as exc:
            print(f"{path.name}: STOPPED. {exc}", file=sys.stderr)
            return 3  # every other invoice would fail the same way
        except QBOError as exc:
            print(f"{path.name}: FAILED\n{exc}", file=sys.stderr)
            failed += 1
            continue
        outcomes.append(outcome)
        print(summary_text(outcome))
        if outcome.payload is not None and args.dry_run:
            print(json.dumps(outcome.payload, indent=2))

    stamp = "dry-run" if args.dry_run else "push"
    (out_dir / f"{stamp}-summary.json").write_text(json.dumps([o.to_dict() for o in outcomes], indent=2))
    counts = {s: sum(o.status == s for o in outcomes) for s in ("created", "duplicate", "held", "dry-run")}
    print("\n" + ", ".join(f"{v} {k}" for k, v in counts.items() if v) + (f", {failed} failed" if failed else ""))
    if client is not None and getattr(client, "refreshes", 0):
        print(f"token refreshed {client.refreshes}x; the new refresh token was saved to the {args.store} store")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
