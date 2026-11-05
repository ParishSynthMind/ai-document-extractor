"""Turn a checked invoice extraction into a Xero bill (an Invoice of Type ACCPAY).

Order of work, and why:
1. Readiness check, no API calls. Any bill field that is missing or low
   confidence, line items that don't add up to the subtotal, or a subtotal +
   tax that isn't the total: the invoice is HELD and written to the review
   list. Nothing is guessed.
2. Dry run stops here and shows the exact bill payload, with the contact still
   to be found or created.
3. Organisation: an invoice not in the organisation's base currency is held.
4. Accounts: each line's account code is checked against the chart of
   accounts (active expense accounts only). A code the organisation doesn't
   have goes to the default account and the summary says so.
5. Tax (tax_mode: tax_type only): the tax type's rate is looked up, and if it
   doesn't give the tax printed on the invoice, the bill is held.
6. Contact: found by exact name. If it exists, a bill from that contact with
   the same invoice number (not deleted or voided) stops the push (DUPLICATE).
   If not, the contact is created.
7. Create the bill as DRAFT (a person approves it in Xero), add a history note
   saying where it came from, read it back by id and compare.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from ..quickbooks.bills import BILL_FIELDS, REQUIRED_FOR_BILL, TOLERANCE, append_review_list
from ..quickbooks.line_items import LineItem

EXPENSE_TYPES = {"EXPENSE", "OVERHEADS", "DIRECTCOSTS"}
TEXT_MAX = 255  # Xero's limit for InvoiceNumber and Contact Name
STATUSES = ("DRAFT", "SUBMITTED")  # never AUTHORISED: approving a bill stays a person's job


@dataclass
class Account:
    code: str
    name: str


@dataclass
class XeroRules:
    default_account: Account
    tax_account: Account
    tax_mode: str = "line"  # "line": tax is its own line; "tax_type": Xero works tax out per line
    tax_type: str | None = None
    rules: list[tuple[tuple[str, ...], Account]] = field(default_factory=list)

    def account_for(self, description: str) -> tuple[Account, str]:
        """Keywords match whole words only, so 'sign' does not fire on 'design'."""
        for keywords, account in self.rules:
            hit = next((k for k in keywords
                        if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", description.lower())), None)
            if hit:
                return account, f"rule '{hit}'"
        return self.default_account, "no rule matched, default account"


def _account(d: dict) -> Account:
    return Account(str(d["code"]), str(d.get("name", d["code"])))


def load_xero_rules(path: str | Path) -> XeroRules:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    mode = data.get("tax_mode", "line")
    if mode not in ("line", "tax_type"):
        raise ValueError("tax_mode must be 'line' or 'tax_type'")
    if mode == "tax_type" and not data.get("tax_type"):
        raise ValueError("tax_mode tax_type needs a tax_type (for example INPUT2)")
    rules = [(tuple(str(k) for k in r["keywords"]), _account(r)) for r in data.get("rules", [])]
    return XeroRules(_account(data["default_account"]), _account(data.get("tax_account", data["default_account"])),
                     mode, data.get("tax_type"), rules)


@dataclass
class PlannedLine:
    description: str
    amount: float
    quantity: float
    unit_amount: float
    code: str
    account: str
    why: str
    is_tax: bool = False


@dataclass
class XeroBillPlan:
    file: str
    contact: str | None
    invoice_number: str | None
    date: str | None
    due_date: str | None
    currency: str | None
    subtotal: float | None
    tax: float | None
    total: float | None
    lines: list[PlannedLine]
    hold_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # the review list writer is shared with the QuickBooks add-on
    @property
    def vendor(self):
        return self.contact

    @property
    def doc_number(self):
        return self.invoice_number


@dataclass
class XeroOutcome:
    file: str
    status: str  # created, held, duplicate, dry-run
    contact: str | None
    invoice_number: str | None
    total: float | None
    invoice_id: str | None = None
    contact_id: str | None = None
    contact_created: bool = False
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


def _qty_and_unit(li: LineItem) -> tuple[float, float]:
    """Keep the invoice's own quantity x price when Xero's 2-decimal unit price still gives the line amount."""
    q, u = li.quantity, li.unit_price
    if isinstance(q, (int, float)) and isinstance(u, (int, float)) and q > 0:
        if abs(round(q * round(u, 2), 2) - li.amount) < 0.005:  # to the cent, or Xero posts a different amount
            return float(q), round(float(u), 2)
    return 1.0, round(float(li.amount), 2)


def plan_xero_bill(result, line_items: list[LineItem], rules: XeroRules) -> XeroBillPlan:
    """Everything that can be decided without calling Xero."""
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

    contact, number = _val(result, "supplier_name"), _val(result, "invoice_number")
    subtotal, tax, total = _val(result, "subtotal"), _val(result, "tax_amount"), _val(result, "total_amount")
    if isinstance(contact, str) and len(contact) > TEXT_MAX:
        holds.append("supplier_name: longer than Xero's 255-character contact name limit")
    if isinstance(number, str) and (len(number) > TEXT_MAX or not number.isprintable() or not number.isascii()):
        holds.append("invoice_number: Xero takes printable ASCII up to 255 characters")

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
        if not isinstance(li.amount, (int, float)):
            continue
        account, why = rules.account_for(li.description)
        q, u = _qty_and_unit(li)
        lines.append(PlannedLine(li.description, round(float(li.amount), 2), q, u, account.code, account.name, why))
    if rules.tax_mode == "line" and isinstance(tax, (int, float)) and tax > 0:
        lines.append(PlannedLine("Tax shown on the invoice", round(float(tax), 2), 1.0, round(float(tax), 2),
                                 rules.tax_account.code, rules.tax_account.name, "tax line", is_tax=True))

    return XeroBillPlan(result.file, contact, number, _val(result, "invoice_date"), _val(result, "due_date"),
                        _val(result, "currency"), subtotal, tax, total, lines, holds, notes)


def line_amount_types(plan: XeroBillPlan, rules: XeroRules) -> str:
    if rules.tax_mode == "tax_type" and isinstance(plan.tax, (int, float)) and plan.tax > 0:
        return "Exclusive"
    return "NoTax"


def build_payload(plan: XeroBillPlan, contact_id: str, codes: dict[str, str], rules: XeroRules,
                  status: str = "DRAFT") -> dict:
    lat = line_amount_types(plan, rules)
    items = []
    for line in plan.lines:
        item = {"Description": line.description[:4000], "Quantity": line.quantity, "UnitAmount": line.unit_amount,
                "AccountCode": codes[line.code]}
        if lat == "Exclusive":
            item["TaxType"] = rules.tax_type
        items.append(item)
    payload = {
        "Type": "ACCPAY",
        "Contact": {"ContactID": contact_id},  # only the id: other fields would overwrite the contact
        "Date": plan.date,
        "InvoiceNumber": plan.invoice_number,
        "CurrencyCode": plan.currency,
        "Status": status,
        "LineAmountTypes": lat,
        "LineItems": items,
    }
    if plan.due_date:
        payload["DueDate"] = plan.due_date
    return payload


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class XeroBillPusher:
    def __init__(self, client, rules: XeroRules, review_file: str | Path | None = None, label: str = "",
                 source: str = "ai-document-extractor", status: str = "DRAFT"):
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        self.client = client
        self.rules = rules
        self.review_file = review_file
        self.label = label
        self.source = source
        self.status = status
        self._org: dict | None = None
        self._accounts: dict[str, dict] | None = None
        self._rates: dict[str, dict] | None = None

    # ---- lookups (each fetched once per run) --------------------------------
    def organisation(self) -> dict:
        if self._org is None:
            self._org = self.client.organisation()
        return self._org

    def expense_accounts(self) -> dict[str, dict]:
        if self._accounts is None:
            self._accounts = {a["Code"]: a for a in self.client.accounts()
                              if a.get("Code") and a.get("Status") == "ACTIVE" and a.get("Type") in EXPENSE_TYPES}
        return self._accounts

    def tax_rate(self, tax_type: str) -> dict | None:
        if self._rates is None:
            self._rates = {r["TaxType"]: r for r in self.client.tax_rates() if r.get("Status") == "ACTIVE"}
        return self._rates.get(tax_type)

    def find_contact(self, name: str) -> dict | None:
        rows = [c for c in self.client.search_contacts(name) if (c.get("Name") or "").casefold() == name.casefold()]
        rows.sort(key=lambda c: c.get("ContactStatus") != "ACTIVE")  # active one first
        return rows[0] if rows else None

    def find_duplicate(self, contact_id: str, number: str) -> dict | None:
        def same(b):
            return (b.get("Type") == "ACCPAY" and b.get("InvoiceNumber") == number
                    and b.get("Contact", {}).get("ContactID") == contact_id
                    and b.get("Status") not in ("DELETED", "VOIDED"))

        if "," not in number:  # the fast, filtered query (InvoiceNumbers is a comma-separated list)
            return next((b for b in self.client.bills(contact_id, number) if same(b)), None)
        page = 1
        while True:  # a comma in the number: page through the contact's bills and compare in code
            rows = self.client.bills(contact_id, None, page=page)
            hit = next((b for b in rows if same(b)), None)
            if hit or len(rows) < 1000:
                return hit
            page += 1

    def _note(self, plan: XeroBillPlan) -> str:
        note = f"Created by {self.source} from {plan.file}."
        return f"{self.label} {note}".strip() if self.label else note

    def _hold(self, out: XeroOutcome, plan: XeroBillPlan, reasons: list[str]) -> XeroOutcome:
        plan.hold_reasons = reasons
        out.status, out.reasons = "held", list(reasons)
        if self.review_file:
            append_review_list(self.review_file, plan)
        return out

    # ---- the push -----------------------------------------------------------
    def push(self, plan: XeroBillPlan, dry_run: bool = False) -> XeroOutcome:
        out = XeroOutcome(plan.file, "held", plan.contact, plan.invoice_number, plan.total,
                          notes=list(plan.notes), lines=[asdict(line) for line in plan.lines])
        if plan.hold_reasons:
            return self._hold(out, plan, plan.hold_reasons)

        if dry_run:  # no API call at all
            out.status = "dry-run"
            out.payload = build_payload(plan, "<found or created at push time>",
                                        {line.code: line.code for line in plan.lines}, self.rules, self.status)
            out.notes.append("not checked in a dry run: base currency, account codes, tax rate, duplicates")
            return out

        base = self.organisation().get("BaseCurrency")
        if plan.currency and base and plan.currency.upper() != base.upper():
            return self._hold(out, plan, [f"currency: invoice is {plan.currency}, the organisation's base currency "
                                          f"is {base} and foreign-currency bills are not handled"])

        accounts = self.expense_accounts()
        default = self.rules.default_account.code
        if default not in accounts:
            raise RuntimeError(f"the default account code {default} is not an active expense account "
                               "in this organisation; fix default_account in the config")
        codes = {}
        for line in plan.lines:
            if line.code not in codes:
                if line.code in accounts:
                    codes[line.code] = line.code
                else:
                    codes[line.code] = default
                    out.notes.append(f"account {line.code} ({line.account}) is not an active expense account here; "
                                     f"used the default {default} ({accounts[default].get('Name')})")
        for d in out.lines:
            d["code_used"] = codes[d["code"]]
            d["account_used"] = accounts[codes[d["code"]]].get("Name", d["account"])

        if line_amount_types(plan, self.rules) == "Exclusive":
            rate = self.tax_rate(self.rules.tax_type)
            if rate is None:
                return self._hold(out, plan, [f"tax: tax type {self.rules.tax_type} is not an active tax rate here"])
            pct = _num(rate.get("EffectiveRate")) or 0.0
            expected = round(sum(round(line.amount * pct / 100, 2) for line in plan.lines), 2)
            if abs(expected - plan.tax) > TOLERANCE * max(1, len(plan.lines)):
                return self._hold(out, plan, [f"tax: {rate.get('Name', self.rules.tax_type)} ({pct:g}%) gives "
                                              f"{expected:.2f} but the invoice shows {plan.tax:.2f}"])

        contact = self.find_contact(plan.contact)
        if contact:
            dup = self.find_duplicate(contact["ContactID"], plan.invoice_number)
            if dup:
                out.status = "duplicate"
                out.contact_id, out.invoice_id = contact["ContactID"], dup["InvoiceID"]
                out.reasons = [f"bill {dup['InvoiceID']} ({dup.get('Status')}) already exists for {plan.contact} "
                               f"with invoice number {plan.invoice_number}; nothing created"]
                return out
            if contact.get("ContactStatus") == "ARCHIVED":
                out.notes.append(f"contact '{plan.contact}' is archived in Xero; the bill was still added to it")
        else:
            contact = self.client.create_contact(plan.contact)
            out.contact_created = True
            out.notes.append(f"contact '{plan.contact}' did not exist; created it")
        out.contact_id = contact["ContactID"]

        out.payload = build_payload(plan, contact["ContactID"], codes, self.rules, self.status)
        created = self.client.create_invoice(out.payload)
        out.invoice_id = created["InvoiceID"]
        out.status = "created"
        for w in created.get("Warnings") or []:
            out.notes.append(f"Xero warning: {w.get('Message')}")
        try:
            self.client.add_history_note(out.invoice_id, self._note(plan))
        except Exception as exc:  # the bill exists; a missing note is worth saying, not worth failing
            out.notes.append(f"bill created but the history note failed: {str(exc).splitlines()[0]}")

        back = self.client.read_invoice(out.invoice_id)
        out.readback = back
        problems = []
        if abs((_num(back.get("Total")) or -1) - float(plan.total)) > TOLERANCE:
            problems.append(f"total {back.get('Total')}")
        if back.get("InvoiceNumber") != plan.invoice_number:
            problems.append(f"number {back.get('InvoiceNumber')}")
        if back.get("Contact", {}).get("ContactID") != contact["ContactID"]:
            problems.append("contact")
        if back.get("Type") != "ACCPAY":
            problems.append(f"type {back.get('Type')}")
        out.verified = not problems
        if problems:
            out.notes.append(f"read-back mismatch ({', '.join(problems)}); check bill {out.invoice_id} by hand")
        return out


def summary_text(o: XeroOutcome) -> str:
    head = {"created": "CREATED (draft bill)", "held": "HELD FOR REVIEW", "duplicate": "DUPLICATE, SKIPPED",
            "dry-run": "DRY RUN (no API call)"}[o.status]
    total = "" if o.total is None else f"  total {o.total:,.2f}"
    lines = [f"{o.file}: {head}  {o.contact or '?'} #{o.invoice_number or '?'}{total}"]
    if o.invoice_id:
        lines.append(f"  bill {o.invoice_id}" + ("" if o.verified is None
                                                  else f", read back {'OK' if o.verified else 'MISMATCH'}"))
    if o.status in ("created", "dry-run"):
        for d in o.lines:
            code = d.get("code_used", d["code"])
            lines.append(f"    {d['amount']:>10,.2f}  {code:<5} {d.get('account_used', d['account'])[:26]:<26} "
                         f"{d['description'][:40]}")
    lines += [f"  ! {r}" for r in o.reasons]
    lines += [f"  - {n}" for n in o.notes]
    return "\n".join(lines)
