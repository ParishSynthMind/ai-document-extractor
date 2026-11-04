"""Turn a checked invoice extraction into a QuickBooks Online Bill.

Order of work, and why:
1. Readiness check, no API calls. Any bill field that is missing or low
   confidence, a currency the company can't take, line items that don't add up
   to the subtotal, or a subtotal + tax that isn't the total: the invoice is
   HELD and written to the review list. Nothing is guessed.
2. Dry run stops here and shows the exact Bill payload, with the vendor and
   account ids still to be looked up.
3. Vendor: find by exact display name. If it exists, check for a Bill with the
   same invoice number for that vendor; if there is one, stop (DUPLICATE). If
   the vendor doesn't exist, create it (a new vendor can't have a duplicate).
4. Accounts: each line's account is looked up by name. If it isn't in the
   company, the line goes to the default account and the summary says so.
5. Create the Bill, read it back by id, and compare the total and invoice number.
"""
from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .client import quote
from .line_items import LineItem

# Header fields the bill uses. Fields the bill doesn't use (IBAN, PO, VAT number)
# don't block a push even if they are on the review list.
BILL_FIELDS = ("supplier_name", "invoice_number", "invoice_date", "due_date", "currency",
               "subtotal", "tax_amount", "total_amount")
REQUIRED_FOR_BILL = ("supplier_name", "invoice_number", "invoice_date", "currency", "total_amount")
EXPENSE_TYPES = {"Expense", "Other Expense", "Cost of Goods Sold"}
DOC_NUMBER_MAX = 21  # QuickBooks' limit for Bill.DocNumber
VENDOR_NAME_MAX = 100
TOLERANCE = 0.01


@dataclass
class AccountRules:
    default_account: str
    tax_account: str
    rules: list[tuple[tuple[str, ...], str]] = field(default_factory=list)

    def account_for(self, description: str) -> tuple[str, str]:
        """Return (account name, why) for a line description. Keywords match whole words only,
        so 'sign' does not fire on 'design'."""
        for keywords, account in self.rules:
            hit = next((k for k in keywords
                        if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", description.lower())), None)
            if hit:
                return account, f"rule '{hit}'"
        return self.default_account, "no rule matched, default account"


def load_account_rules(path: str | Path) -> AccountRules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    rules = [(tuple(str(k) for k in r["keywords"]), str(r["account"])) for r in data.get("rules", [])]
    return AccountRules(str(data["default_account"]), str(data["tax_account"]), rules)


@dataclass
class PlannedLine:
    description: str
    amount: float
    account: str
    why: str


@dataclass
class BillPlan:
    file: str
    vendor: str | None
    doc_number: str | None
    txn_date: str | None
    due_date: str | None
    currency: str | None
    total: float | None
    lines: list[PlannedLine]
    hold_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PushOutcome:
    file: str
    status: str  # created, held, duplicate, dry-run
    vendor: str | None
    doc_number: str | None
    total: float | None
    bill_id: str | None = None
    vendor_id: str | None = None
    vendor_created: bool = False
    reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)
    payload: dict | None = None
    readback: dict | None = None
    verified: bool | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _val(result, name):
    fr = result.fields.get(name)
    return None if fr is None else fr.value


def plan_bill(result, line_items: list[LineItem], rules: AccountRules, home_currency: str) -> BillPlan:
    """Everything that can be decided without calling QuickBooks."""
    holds, notes = [], []
    review = {r["field"]: r["reason"] for r in result.needs_review}
    for name in BILL_FIELDS:
        fr = result.fields.get(name)
        if fr is None or fr.value is None:
            if name in REQUIRED_FOR_BILL:
                holds.append(f"{name}: missing")
            elif name in review:
                holds.append(f"{name}: {review[name]}")
            continue
        if fr.confidence == "low" or name in review:
            holds.append(f"{name}: {review.get(name) or 'low confidence'}")
        elif fr.confidence == "medium":
            notes.append(f"{name} was medium confidence ({fr.value})")

    vendor = _val(result, "supplier_name")
    doc_number = _val(result, "invoice_number")
    currency = _val(result, "currency")
    subtotal, tax, total = _val(result, "subtotal"), _val(result, "tax_amount"), _val(result, "total_amount")

    if isinstance(vendor, str) and (":" in vendor or len(vendor) > VENDOR_NAME_MAX):
        holds.append("supplier_name: QuickBooks can't take a vendor name with ':' or over 100 characters")
    if isinstance(doc_number, str) and len(doc_number) > DOC_NUMBER_MAX:
        holds.append(f"invoice_number: longer than QuickBooks' {DOC_NUMBER_MAX}-character limit")
    if currency and str(currency).upper() != home_currency.upper():
        holds.append(f"currency: invoice is {currency}, the company's home currency is {home_currency} "
                     "and multicurrency bills are not handled")

    if not line_items:
        holds.append("line items: none found")
    for i, li in enumerate(line_items, start=1):
        if li.confidence == "low":
            holds.append(f"line {i} '{li.description[:40]}': {'; '.join(li.checks) or 'low confidence'}")
    line_sum = round(sum(li.amount for li in line_items if isinstance(li.amount, (int, float))), 2)
    expected_net = subtotal if isinstance(subtotal, (int, float)) else (
        total - (tax or 0) if isinstance(total, (int, float)) else None)
    if line_items and expected_net is not None and abs(line_sum - expected_net) > TOLERANCE:
        holds.append(f"line items add up to {line_sum:.2f} but the invoice's net amount is {expected_net:.2f}")
    if isinstance(total, (int, float)) and isinstance(subtotal, (int, float)):
        if abs(subtotal + (tax or 0) - total) > TOLERANCE:
            holds.append(f"subtotal {subtotal:.2f} + tax {tax or 0:.2f} does not equal total {total:.2f}")

    lines = []
    for li in line_items:
        account, why = rules.account_for(li.description)
        lines.append(PlannedLine(li.description, round(float(li.amount), 2), account, why))
    if isinstance(tax, (int, float)) and tax > 0:
        lines.append(PlannedLine("Tax shown on the invoice", round(float(tax), 2), rules.tax_account, "tax line"))

    return BillPlan(result.file, vendor, doc_number, _val(result, "invoice_date"), _val(result, "due_date"),
                    currency, total, lines, holds, notes)


def build_payload(plan: BillPlan, vendor_ref: dict, account_refs: dict[str, dict], private_note: str) -> dict:
    payload = {
        "VendorRef": vendor_ref,
        "TxnDate": plan.txn_date,
        "DocNumber": plan.doc_number,
        "PrivateNote": private_note,
        "Line": [
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": line.amount,
                "Description": line.description[:4000],
                "AccountBasedExpenseLineDetail": {"AccountRef": account_refs[line.account]},
            }
            for line in plan.lines
        ],
    }
    if plan.due_date:
        payload["DueDate"] = plan.due_date
    return payload


def append_review_list(path: str | Path, plan: BillPlan) -> None:
    p = Path(path)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["held_at", "file", "supplier", "invoice_number", "total", "reasons"])
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), plan.file, plan.vendor or "",
                    plan.doc_number or "", "" if plan.total is None else f"{plan.total:.2f}",
                    " | ".join(plan.hold_reasons)])


class BillPusher:
    def __init__(self, client, rules: AccountRules, review_file: str | Path | None = None,
                 label: str = "", source: str = "ai-document-extractor"):
        self.client = client
        self.rules = rules
        self.review_file = review_file
        self.label = label
        self.source = source
        self._accounts: dict[str, dict | None] = {}
        self._home_currency: str | None = None

    # ---- lookups -------------------------------------------------------
    def home_currency(self) -> str:
        if self._home_currency is None:
            prefs = self.client.preferences().get("CurrencyPrefs", {})
            self._home_currency = prefs.get("HomeCurrency", {}).get("value", "USD")
        return self._home_currency

    def find_vendor(self, name: str) -> dict | None:
        rows = self.client.query(f"select * from Vendor where DisplayName = '{quote(name)}'")
        return rows[0] if rows else None

    def find_duplicate(self, vendor_id: str, doc_number: str) -> dict | None:
        rows = self.client.query(f"select * from Bill where DocNumber = '{quote(doc_number)}'")
        return next((b for b in rows if b.get("VendorRef", {}).get("value") == vendor_id), None)

    def find_account(self, name: str) -> dict | None:
        if name not in self._accounts:
            rows = self.client.query(f"select * from Account where Name = '{quote(name)}' and Active = true")
            rows = sorted((r for r in rows if r.get("AccountType") in EXPENSE_TYPES), key=lambda r: int(r["Id"]))
            self._accounts[name] = rows[0] if rows else None
            if len(rows) > 1:
                self._accounts[name]["_note"] = f"{len(rows)} expense accounts are called '{name}'; used Id {rows[0]['Id']}"
        return self._accounts[name]

    def _private_note(self, plan: BillPlan) -> str:
        note = f"Created by {self.source} from {plan.file}."
        return f"{self.label} {note}".strip() if self.label else note

    # ---- the push ------------------------------------------------------
    def push(self, plan: BillPlan, dry_run: bool = False) -> PushOutcome:
        out = PushOutcome(plan.file, "held", plan.vendor, plan.doc_number, plan.total,
                          reasons=list(plan.hold_reasons), notes=list(plan.notes),
                          lines=[asdict(line) for line in plan.lines])
        if plan.hold_reasons:
            if self.review_file:
                append_review_list(self.review_file, plan)
            return out

        if dry_run:
            # No API call at all: show the payload with the lookups still to do.
            refs = {line.account: {"name": line.account, "value": "<looked up at push time>"} for line in plan.lines}
            out.status = "dry-run"
            out.payload = build_payload(plan, {"name": plan.vendor, "value": "<found or created at push time>"},
                                        refs, self._private_note(plan))
            return out

        vendor = self.find_vendor(plan.vendor)
        if vendor:
            dup = self.find_duplicate(vendor["Id"], plan.doc_number)
            if dup:
                out.status = "duplicate"
                out.vendor_id, out.bill_id = vendor["Id"], dup["Id"]
                out.reasons = [f"Bill {dup['Id']} already exists for {plan.vendor} with invoice number "
                               f"{plan.doc_number}; nothing created"]
                return out
        else:
            vendor = self.client.create("Vendor", {"DisplayName": plan.vendor, "CompanyName": plan.vendor})
            out.vendor_created = True
            out.notes.append(f"vendor '{plan.vendor}' did not exist; created it (Id {vendor['Id']})")
        out.vendor_id = vendor["Id"]

        refs = {}
        for line in plan.lines:
            if line.account in refs:
                continue
            acct = self.find_account(line.account)
            if acct is None:
                fallback = self.find_account(self.rules.default_account)
                if fallback is None:
                    raise RuntimeError(f"neither '{line.account}' nor the default account "
                                       f"'{self.rules.default_account}' exists in this company")
                out.notes.append(f"account '{line.account}' not found; used the default "
                                 f"'{self.rules.default_account}' instead")
                acct = fallback
            elif acct.get("_note"):
                out.notes.append(acct["_note"])
            refs[line.account] = {"value": acct["Id"], "name": acct["Name"]}
        for d in out.lines:
            d["account_used"] = refs[d["account"]]["name"]

        out.payload = build_payload(plan, {"value": vendor["Id"], "name": vendor["DisplayName"]}, refs,
                                    self._private_note(plan))
        created = self.client.create("Bill", out.payload)
        out.bill_id = created["Id"]
        out.status = "created"

        back = self.client.read("Bill", created["Id"])
        out.readback = back
        out.verified = (abs(float(back.get("TotalAmt", -1)) - float(plan.total)) <= TOLERANCE
                        and back.get("DocNumber") == plan.doc_number
                        and back.get("VendorRef", {}).get("value") == vendor["Id"])
        if not out.verified:
            out.notes.append(f"read-back mismatch: QuickBooks shows total {back.get('TotalAmt')} and number "
                             f"{back.get('DocNumber')}; check Bill {created['Id']} by hand")
        return out


def summary_text(o: PushOutcome) -> str:
    head = {"created": "CREATED", "held": "HELD FOR REVIEW", "duplicate": "DUPLICATE, SKIPPED",
            "dry-run": "DRY RUN (no API call)"}[o.status]
    total = "" if o.total is None else f"  total {o.total:,.2f}"
    lines = [f"{o.file}: {head}  {o.vendor or '?'} #{o.doc_number or '?'}{total}"]
    if o.bill_id:
        lines.append(f"  bill Id {o.bill_id}" + (f", vendor Id {o.vendor_id}" if o.vendor_id else "")
                     + ("" if o.verified is None else f", read back {'OK' if o.verified else 'MISMATCH'}"))
    if o.status in ("created", "dry-run"):
        for d in o.lines:
            acct = d.get("account_used", d["account"])
            lines.append(f"    {d['amount']:>10,.2f}  {acct:<26} {d['description'][:44]}")
    lines += [f"  ! {r}" for r in o.reasons]
    lines += [f"  - {n}" for n in o.notes]
    return "\n".join(lines)
