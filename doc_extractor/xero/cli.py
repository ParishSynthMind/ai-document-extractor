"""Command line: push invoices into Xero as draft bills.

    xero-push --connect                        # once: log in to Xero, pick the organisation
    xero-push --show-setup                     # base currency, expense account codes, tax rates
    xero-push samples/invoices_us/*.* --dry-run --out-dir xero-results
    xero-push xero-results/*.xero-input.json --out-dir xero-results
    xero-push --delete-bill <InvoiceID>        # remove a test draft

A document is extracted first (header fields + line items, two Claude calls)
and saved as <name>.xero-input.json in --out-dir, so a dry run and the real
push use exactly the same data without calling Claude again. Saved
.qbo-input.json bundles from the QuickBooks add-on are the same format and
work too.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from ..extract import ExtractionResult
from ..quickbooks.line_items import LineItem
from .bills import STATUSES, XeroBillPusher, load_xero_rules, plan_xero_bill, summary_text
from .client import RateLimited, ReauthorisationNeeded, XeroClient, XeroError

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ACCOUNTS = ROOT / "config" / "xero_accounts.yaml"
BUNDLE_SUFFIXES = (".xero-input.json", ".qbo-input.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xero-push", description="Create Xero draft bills from invoices.")
    p.add_argument("inputs", nargs="*", help="invoice documents, or saved .xero-input.json files")
    p.add_argument("--dry-run", action="store_true", help="show the bill payload; make no Xero call")
    p.add_argument("--out-dir", default="xero-results", help="where extractions and run summaries are saved")
    p.add_argument("--review-file", default=None, help="CSV held invoices are appended to "
                                                       "(default <out-dir>/review_list.csv)")
    p.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS), help="account mapping YAML")
    p.add_argument("--status", choices=STATUSES, default="DRAFT", help="bill status in Xero (default DRAFT)")
    p.add_argument("--label", default="", help="text put at the start of each bill's history note")
    p.add_argument("--store", choices=["keychain", "file"], default=os.environ.get("XERO_TOKEN_STORE", "file"),
                   help="where credentials live (default: env vars + token file)")
    p.add_argument("--keychain-account", default=os.environ.get("XERO_KEYCHAIN_ACCOUNT", "neo"))
    p.add_argument("--token-file", default=os.environ.get("XERO_TOKEN_FILE", ".xero_tokens.json"))
    p.add_argument("--connect", action="store_true", help="log in to Xero once and save the organisation")
    p.add_argument("--redirect-uri", default="http://localhost:8723/callback",
                   help="must match the app's redirect URI exactly")
    p.add_argument("--tenant-name", default=None, help="with --connect: which organisation, if several")
    p.add_argument("--show-setup", action="store_true", help="print base currency, expense accounts, tax rates")
    p.add_argument("--delete-bill", metavar="INVOICE_ID", help="delete one DRAFT bill (to clean up test records)")
    p.add_argument("--model", default=None, help="Claude model id (default claude-sonnet-5-5)")
    return p


def make_store(args):
    from .tokens import FileTokenStore, KeychainTokenStore

    if args.store == "keychain":
        return KeychainTokenStore(account=args.keychain_account)
    return FileTokenStore(args.token_file)


def load_bundle(path: Path) -> tuple[ExtractionResult, list[LineItem]]:
    data = json.loads(path.read_text())
    return ExtractionResult.from_dict(data["extraction"]), [LineItem(**li) for li in data["line_items"]]


def extract_bundle(path: Path, out_dir: Path, field_extractor, line_extractor):
    from ..extract import extract_document
    from ..loaders import load
    from ..quickbooks.line_items import check_line_items
    from ..schemas import load_schema

    doc = load(path)
    result = extract_document(doc, load_schema("invoice"), field_extractor)
    lines = check_line_items(line_extractor.extract_lines(doc.text), doc.text)
    bundle = {"extraction": result.to_dict(), "line_items": [li.to_dict() for li in lines]}
    (out_dir / f"{path.stem}.xero-input.json").write_text(json.dumps(bundle, indent=2))
    return result, lines


def show_setup(client) -> None:
    org = client.organisation()
    print(f"Organisation: {org.get('Name')}  base currency {org.get('BaseCurrency')}  "
          f"country {org.get('CountryCode')}  demo company: {org.get('IsDemoCompany')}")
    print("\nActive expense accounts (code, name):")
    for a in sorted(client.accounts(), key=lambda a: a.get("Code") or ""):
        if a.get("Status") == "ACTIVE" and a.get("Type") in ("EXPENSE", "OVERHEADS", "DIRECTCOSTS") and a.get("Code"):
            print(f"  {a['Code']:<6} {a.get('Name')}  [{a.get('Type')}]")
    print("\nActive tax rates (tax type, rate, name):")
    for r in client.tax_rates():
        if r.get("Status") == "ACTIVE":
            print(f"  {r.get('TaxType'):<14} {float(r.get('EffectiveRate') or 0):>6.2f}%  {r.get('Name')}")


def main(argv: list[str] | None = None, client=None, field_extractor=None, line_extractor=None,
         open_browser=webbrowser.open) -> int:
    args = build_parser().parse_args(argv)
    out_dir = Path(args.out_dir)

    try:
        if args.connect:
            from .auth import connect

            store = make_store(args)
            client = client or XeroClient(store)
            chosen = connect(client, store, args.redirect_uri, args.tenant_name, open_browser=open_browser)
            print(f"Connected to {chosen['tenantName']!r}. Tenant id and refresh token saved to the {args.store} "
                  f"store. Granted scopes: {chosen.get('scope') or '(not reported)'}")
            return 0
        if args.show_setup:
            show_setup(client or XeroClient(make_store(args)))
            return 0
        if args.delete_bill:
            client = client or XeroClient(make_store(args))
            gone = client.delete_draft(args.delete_bill)
            print(f"deleted draft bill {gone.get('InvoiceID')} ({gone.get('InvoiceNumber')}), "
                  f"status now {gone.get('Status')}")
            return 0
    except ReauthorisationNeeded as exc:
        print(f"STOPPED. {exc}", file=sys.stderr)
        return 3
    except XeroError as exc:
        print(f"FAILED\n{exc}", file=sys.stderr)
        return 1
    if not args.inputs:
        build_parser().error("give at least one invoice document or .xero-input.json")

    out_dir.mkdir(parents=True, exist_ok=True)
    rules = load_xero_rules(args.accounts)
    if not args.dry_run and client is None:
        client = XeroClient(make_store(args))
    pusher = XeroBillPusher(client, rules, review_file=args.review_file or out_dir / "review_list.csv",
                            label=args.label, status=args.status)

    outcomes, failed = [], 0
    for name in args.inputs:
        path = Path(name)
        try:
            if path.name.endswith(BUNDLE_SUFFIXES):
                result, lines = load_bundle(path)
            else:
                if field_extractor is None:
                    from ..llm import DEFAULT_MODEL, AnthropicExtractor
                    from ..quickbooks.line_items import AnthropicLineItemExtractor

                    field_extractor = AnthropicExtractor(model=args.model or DEFAULT_MODEL)
                    line_extractor = line_extractor or AnthropicLineItemExtractor(model=args.model or DEFAULT_MODEL)
                result, lines = extract_bundle(path, out_dir, field_extractor, line_extractor)
            outcome = pusher.push(plan_xero_bill(result, lines, rules), dry_run=args.dry_run)
        except (ReauthorisationNeeded, RateLimited) as exc:
            print(f"{path.name}: STOPPED. {exc}", file=sys.stderr)
            return 3  # every other invoice would hit the same wall
        except XeroError as exc:
            print(f"{path.name}: FAILED\n{exc}", file=sys.stderr)
            failed += 1
            continue
        except RuntimeError as exc:  # a config problem (for example the default account code is missing)
            print(f"{path.name}: STOPPED. {exc}", file=sys.stderr)
            return 2
        outcomes.append(outcome)
        print(summary_text(outcome))
        if outcome.payload is not None and args.dry_run:
            print(json.dumps(outcome.payload, indent=2))

    stamp = "dry-run" if args.dry_run else "push"
    (out_dir / f"{stamp}-summary.json").write_text(json.dumps([o.to_dict() for o in outcomes], indent=2))
    counts = {s: sum(o.status == s for o in outcomes) for s in ("created", "duplicate", "held", "dry-run")}
    print("\n" + ", ".join(f"{v} {k}" for k, v in counts.items() if v) + (f", {failed} failed" if failed else ""))
    if client is not None:
        if getattr(client, "refreshes", 0):
            print(f"token refreshed {client.refreshes}x; the new refresh token was saved to the {args.store} store")
        if getattr(client, "waited", 0):
            print(f"waited {client.waited:.0f}s in total for Xero's rate limit")
        if getattr(client, "limits", None):
            print("calls left: " + ", ".join(f"{k.replace('X-', '').replace('-Remaining', '')} {v}"
                                             for k, v in client.limits.items()))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
