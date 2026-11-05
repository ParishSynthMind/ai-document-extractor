"""Xero bill push. Every HTTP call goes to a fake Xero (the `responses` library
intercepts requests), built to the shapes in Xero's own docs, so no network,
keys or Xero login are needed. The connect test uses a real localhost socket."""
import base64
import json
import re
import socket
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from conftest import answer

from doc_extractor.extract import ExtractionResult, FieldResult
from doc_extractor.quickbooks.line_items import LineItem
from doc_extractor.xero import cli
from doc_extractor.xero.auth import SCOPES, authorize_url, connect, jwt_claims, pick_tenant, wait_for_code
from doc_extractor.xero.bills import XeroBillPusher, load_xero_rules, plan_xero_bill, summary_text
from doc_extractor.xero.client import (
    API_BASE,
    CONNECTIONS_URL,
    TOKEN_URL,
    RateLimited,
    ReauthorisationNeeded,
    XeroAuthError,
    XeroClient,
    XeroError,
    XeroValidationError,
)
from doc_extractor.xero.tokens import FileTokenStore, KeychainTokenStore, MemoryTokenStore, TokenStoreError

TENANT = "70784a63-d24b-46a9-a4db-0e70a274b056"
RULES = load_xero_rules(cli.DEFAULT_ACCOUNTS)
ACCOUNTS = [("429", "General Expenses", "EXPENSE"), ("485", "Subscriptions", "EXPENSE"),
            ("412", "Consulting & Accounting", "EXPENSE"), ("473", "Repairs and Maintenance", "EXPENSE"),
            ("461", "Printing & Stationery", "OVERHEADS"), ("400", "Advertising", "EXPENSE"),
            ("200", "Sales", "REVENUE")]


def store(refresh="rt-0"):
    return MemoryTokenStore(client_id="cid", client_secret="csecret", tenant_id=TENANT, refresh_token=refresh)


def fake_jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


class FakeXero:
    """Enough of Xero to exercise every path: identity, connections, Organisation,
    Accounts, TaxRates, Contacts, Invoices (+ History)."""

    def __init__(self, rsps, base_currency="USD", accounts=ACCOUNTS, rates=None):
        self.rsps = rsps
        self.base_currency = base_currency
        self.accounts = [{"AccountID": f"acc-{c}", "Code": c, "Name": n, "Type": t, "Status": "ACTIVE",
                          "Class": "REVENUE" if t == "REVENUE" else "EXPENSE"} for c, n, t in accounts]
        self.rates = rates if rates is not None else [
            {"Name": "Tax on Purchases", "TaxType": "INPUT", "EffectiveRate": 15.0, "Status": "ACTIVE"},
            {"Name": "20% (VAT on Expenses)", "TaxType": "INPUT2", "EffectiveRate": 20.0, "Status": "ACTIVE"}]
        self.contacts, self.invoices, self.history = {}, {}, {}
        self.n = 0
        self.token_calls, self.token_bodies = 0, []
        self.unauthorised = 0
        self.rate_limited = []  # list of (retry_after or None, problem)
        self.reject_invoice = None
        self.calls = []
        self.events = []  # ordering check: token saves vs API calls
        rsps.add_callback(responses.POST, TOKEN_URL, callback=self.token)
        rsps.add_callback(responses.GET, CONNECTIONS_URL, callback=self.connections)
        rsps.add_callback(responses.GET, re.compile(re.escape(API_BASE) + r"/.*"), callback=self.get)
        rsps.add_callback(responses.PUT, re.compile(re.escape(API_BASE) + r"/.*"), callback=self.put)
        rsps.add_callback(responses.POST, re.compile(re.escape(API_BASE) + r"/.*"), callback=self.post)

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n:04d}-0000-4000-8000-000000000000"

    def envelope(self, key, rows):
        return json.dumps({"Id": "e4c4d1f6-0000-4000-8000-000000000000", "Status": "OK",
                           "ProviderName": "ai-document-extractor", "DateTimeUTC": "/Date(1762300800000)/",
                           key: rows})

    # identity -------------------------------------------------------------
    def token(self, request):
        self.token_calls += 1
        body = {k: v[0] for k, v in parse_qs(request.body).items()}
        self.token_bodies.append(body)
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"cid:csecret").decode()
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        if body["grant_type"] == "refresh_token" and body["refresh_token"].startswith("revoked"):
            return 400, {}, json.dumps({"error": "invalid_grant"})
        n = self.token_calls
        self.events.append(f"token{n}")
        return 200, {}, json.dumps({
            "id_token": "idt", "access_token": fake_jwt({"authentication_event_id": "evt-new"}) + f"-{n}",
            "expires_in": 1800, "token_type": "Bearer", "refresh_token": f"rt-{n}",
            "scope": " ".join(SCOPES)})

    def connections(self, request):
        assert "xero-tenant-id" not in request.headers
        return 200, {}, json.dumps([
            {"id": "c1", "authEventId": "evt-old", "tenantId": "11111111-0000-4000-8000-000000000000",
             "tenantType": "ORGANISATION", "tenantName": "Maple Florist",
             "createdDateUtc": "2019-07-09T23:40:30.1833130", "updatedDateUtc": "2020-05-15T01:35:13.8491980"},
            {"id": "c2", "authEventId": "evt-new", "tenantId": TENANT, "tenantType": "ORGANISATION",
             "tenantName": "Demo Company (Global)", "createdDateUtc": "2026-11-05T02:24:22.2328510",
             "updatedDateUtc": "2026-11-05T02:24:22.2328510"},
            {"id": "c3", "authEventId": "evt-new", "tenantId": "c3d5e782-0000-4000-8000-000000000000",
             "tenantType": "PRACTICEMANAGER", "tenantName": None,
             "createdDateUtc": "2020-01-30T01:33:36.2717380", "updatedDateUtc": "2020-02-02T19:21:08.5739590"}])

    # api ------------------------------------------------------------------
    def _gate(self, request):
        self.calls.append((request.method, request.url))
        self.events.append("api")
        assert request.headers["xero-tenant-id"] == TENANT
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Authorization"].startswith("Bearer ")
        if self.rate_limited:
            wait, problem = self.rate_limited.pop(0)
            headers = {"X-Rate-Limit-Problem": problem}
            if wait is not None:
                headers["Retry-After"] = str(wait)
            return 429, headers, ""
        if self.unauthorised:
            self.unauthorised -= 1
            return 401, {"WWW-Authenticate": 'OAuth Realm="api.xero.com"'}, json.dumps(
                {"Type": None, "Title": "Unauthorized", "Status": 401, "Detail": "AuthenticationUnsuccessful"})
        return None

    def get(self, request):
        if (bad := self._gate(request)) is not None:
            return bad
        url = urlparse(request.url)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path.split("/api.xro/2.0/")[1]
        limits = {"X-DayLimit-Remaining": "998", "X-MinLimit-Remaining": "59", "X-AppMinLimit-Remaining": "9999"}
        if path == "Organisation":
            return 200, limits, self.envelope("Organisations", [{
                "OrganisationID": "8be9db36-3598-4755-ba5c-c2dbc8c4a7a2", "Name": "Demo Company (Global)",
                "BaseCurrency": self.base_currency, "CountryCode": "US", "IsDemoCompany": True}])
        if path == "Accounts":
            return 200, limits, self.envelope("Accounts", self.accounts)
        if path == "TaxRates":
            return 200, limits, self.envelope("TaxRates", self.rates)
        if path == "Contacts":
            term = q["searchTerm"].casefold()
            rows = [c for c in self.contacts.values() if term in c["Name"].casefold()]
            return 200, limits, self.envelope("Contacts", rows)
        if path == "Invoices":
            rows = [i for i in self.invoices.values()
                    if i["Contact"]["ContactID"] in q["ContactIDs"].split(",")
                    and i["Status"] in q["Statuses"].split(",")
                    and ("InvoiceNumbers" not in q or i["InvoiceNumber"] in q["InvoiceNumbers"].split(","))]
            if "page" in q:
                size = int(q.get("pageSize", 100))
                rows = rows[(int(q["page"]) - 1) * size:int(q["page"]) * size]
            return 200, limits, self.envelope("Invoices", rows)
        m = re.match(r"Invoices/([0-9a-f-]+)$", path)
        if m and m.group(1) in self.invoices:
            return 200, limits, self.envelope("Invoices", [self.invoices[m.group(1)]])
        return 404, {}, "The resource you're looking for cannot be found"

    def put(self, request):
        if (bad := self._gate(request)) is not None:
            return bad
        path = urlparse(request.url).path.split("/api.xro/2.0/")[1]
        body = json.loads(request.body)
        if path == "Contacts":
            c = {**body["Contacts"][0], "ContactID": self._id("c0"), "ContactStatus": "ACTIVE"}
            self.contacts[c["ContactID"]] = c
            return 200, {}, self.envelope("Contacts", [c])
        if path == "Invoices":
            inv = body["Invoices"][0]
            if self.reject_invoice:
                return 400, {}, json.dumps(self.reject_invoice)
            assert set(inv["Contact"]) == {"ContactID"}  # docs: only send ContactID
            sub = round(sum(li["Quantity"] * li["UnitAmount"] for li in inv["LineItems"]), 2)
            tax = 0.0
            if inv["LineAmountTypes"] == "Exclusive":
                rate = {r["TaxType"]: r["EffectiveRate"] for r in self.rates}
                tax = round(sum(round(li["Quantity"] * li["UnitAmount"] * rate[li["TaxType"]] / 100, 2)
                                for li in inv["LineItems"]), 2)
            stored = {**inv, "InvoiceID": self._id("b0"), "SubTotal": sub, "TotalTax": tax,
                      "Total": round(sub + tax, 2), "DateString": f"{inv['Date']}T00:00:00",
                      "StatusAttributeString": "OK",
                      "Contact": {"ContactID": inv["Contact"]["ContactID"],
                                  "Name": self.contacts[inv["Contact"]["ContactID"]]["Name"]}}
            self.invoices[stored["InvoiceID"]] = stored
            return 200, {}, self.envelope("Invoices", [stored])
        m = re.match(r"Invoices/([0-9a-f-]+)/History$", path)
        if m:
            self.history.setdefault(m.group(1), []).extend(body["HistoryRecords"])
            return 200, {}, json.dumps({"HistoryRecords": body["HistoryRecords"]})
        return 404, {}, ""

    def post(self, request):
        if (bad := self._gate(request)) is not None:
            return bad
        m = re.match(r"Invoices/([0-9a-f-]+)$", urlparse(request.url).path.split("/api.xro/2.0/")[1])
        body = json.loads(request.body)
        inv = self.invoices[m.group(1)]
        if body.get("Status") == "DELETED":
            if inv["Status"] not in ("DRAFT", "SUBMITTED"):
                return 400, {}, json.dumps({"ErrorNumber": 10, "Type": "ValidationException",
                                            "Message": "A validation exception occurred",
                                            "Elements": [{"ValidationErrors": [
                                                {"Message": "Invoice not of valid status for modification"}]}]})
            inv["Status"] = "DELETED"
        return 200, {}, self.envelope("Invoices", [inv])


@pytest.fixture
def xero():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeXero(rsps)


def make_result(**overrides):
    values = {
        "supplier_name": answer("Summit Ridge IT Services LLC"),
        "invoice_number": answer("SR-10482"),
        "invoice_date": answer("2026-10-28"),
        "due_date": answer("2026-11-27"),
        "purchase_order": answer(None),
        "currency": answer("USD", "medium"),
        "subtotal": answer(1900.0),
        "tax_amount": answer(30.53),
        "total_amount": answer(1930.53),
        "supplier_vat_number": answer(None),
        "bank_iban": answer(None),
    }
    values.update(overrides)
    fields = {k: FieldResult(v["value"], v["confidence"], v["source_snippet"]) for k, v in values.items()}
    review = [{"field": k, "reason": "model marked it low confidence"}
              for k, f in fields.items() if f.value is not None and f.confidence == "low"]
    return ExtractionResult("summit_it_invoice.pdf", "invoice", "pdf-text", 1, fields, review, "2026-11-05T00:00:00Z")


def summit_lines():
    return [LineItem("Managed IT support, October 2026", 1200.0, 1, 1200.0, "high"),
            LineItem("Microsoft 365 Business licences", 330.0, 15, 22.0, "high"),
            LineItem("Laptop repair (screen replacement)", 370.0, 2, 185.0, "high")]


def pusher(tmp_path, s=None, rules=RULES, **kw):
    client = XeroClient(s or store(), sleep=kw.pop("sleep", lambda s: None))
    return XeroBillPusher(client, rules, review_file=tmp_path / "review.csv", label="[TEST]", **kw), client


def tax_type_rules(tmp_path, tax_type="INPUT"):
    p = tmp_path / "rules.yaml"
    p.write_text(cli.DEFAULT_ACCOUNTS.read_text().replace("tax_mode: line", "tax_mode: tax_type")
                 .replace("# tax_type: INPUT2", f"tax_type: {tax_type}"))
    return load_xero_rules(p)


# ---- plan and config (no API) ----------------------------------------------

def test_plan_maps_lines_to_codes_and_adds_tax_line():
    plan = plan_xero_bill(make_result(), summit_lines(), RULES)
    assert plan.hold_reasons == []
    assert [(line.code, line.amount) for line in plan.lines] == [
        ("412", 1200.0), ("485", 330.0), ("473", 370.0), ("429", 30.53)]
    assert plan.lines[1].quantity == 15 and plan.lines[1].unit_amount == 22.0
    assert plan.lines[-1].is_tax and "currency was medium confidence (USD)" in plan.notes


def test_tax_type_mode_has_no_tax_line(tmp_path):
    plan = plan_xero_bill(make_result(), summit_lines(), tax_type_rules(tmp_path))
    assert len(plan.lines) == 3 and not any(line.is_tax for line in plan.lines)


def test_rules_file_is_validated(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("default_account: {code: '429'}\ntax_mode: tax_type\n")
    with pytest.raises(ValueError, match="needs a tax_type"):
        load_xero_rules(p)
    p.write_text("default_account: {code: '429'}\ntax_mode: guess\n")
    with pytest.raises(ValueError, match="tax_mode"):
        load_xero_rules(p)


def test_keywords_match_whole_words_only():
    assert RULES.account_for("Design proof and setup")[0].code == "429"
    assert RULES.account_for("Vinyl banner, 3 x 6 ft")[0].code == "400"


def test_unit_price_that_does_not_survive_two_decimals_becomes_one_line():
    li = LineItem("Cables", 10.0, 3, 3.3333, "high")
    plan = plan_xero_bill(make_result(subtotal=answer(10.0), tax_amount=answer(0.0), total_amount=answer(10.0)),
                          [li], RULES)
    assert (plan.lines[0].quantity, plan.lines[0].unit_amount) == (1.0, 10.0)


@pytest.mark.parametrize("override,reason", [
    ({"invoice_date": answer("2026-10-03", "low")}, "invoice_date"),
    ({"invoice_number": answer(None)}, "invoice_number: missing"),
    ({"total_amount": answer(1999.0)}, "does not equal total"),
    ({"invoice_number": answer("X" * 256)}, "255"),
    ({"invoice_number": answer("SR–10482")}, "printable ASCII"),
])
def test_doubtful_invoices_are_held_not_guessed(override, reason):
    plan = plan_xero_bill(make_result(**override), summit_lines(), RULES)
    assert any(reason in r for r in plan.hold_reasons), plan.hold_reasons


def test_lines_that_do_not_add_up_and_low_lines_are_held():
    lines = summit_lines()[:2]
    assert any("add up to 1530.00" in r for r in plan_xero_bill(make_result(), lines, RULES).hold_reasons)
    lines = summit_lines()
    lines[0].confidence, lines[0].checks = "low", ["source snippet not found in the document"]
    assert any("line 1" in r for r in plan_xero_bill(make_result(), lines, RULES).hold_reasons)


# ---- push --------------------------------------------------------------------

def test_dry_run_makes_no_http_calls(tmp_path):
    with responses.RequestsMock() as rsps:  # any request at all would fail this test
        p, _ = pusher(tmp_path)
        out = p.push(plan_xero_bill(make_result(), summit_lines(), RULES), dry_run=True)
        assert len(rsps.calls) == 0
    assert out.status == "dry-run" and out.payload["Type"] == "ACCPAY" and out.payload["Status"] == "DRAFT"
    assert out.payload["LineAmountTypes"] == "NoTax"


def test_creates_contact_and_draft_bill_then_reads_it_back(xero, tmp_path):
    p, client = pusher(tmp_path)
    out = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    assert out.status == "created" and out.verified and out.contact_created
    bill = xero.invoices[out.invoice_id]
    assert bill["Type"] == "ACCPAY" and bill["Status"] == "DRAFT" and bill["InvoiceNumber"] == "SR-10482"
    assert bill["Total"] == 1930.53 and bill["DueDate"] == "2026-11-27" and bill["CurrencyCode"] == "USD"
    assert [li["AccountCode"] for li in bill["LineItems"]] == ["412", "485", "473", "429"]
    assert xero.history[out.invoice_id] == [{"Details": "[TEST] Created by ai-document-extractor from "
                                                        "summit_it_invoice.pdf."}]
    assert client.limits["X-MinLimit-Remaining"] == "59"
    assert "CREATED (draft bill)" in summary_text(out) and "read back OK" in summary_text(out)


def test_existing_contact_is_reused_and_near_names_are_not(xero, tmp_path):
    xero.contacts["c-west"] = {"ContactID": "c-west", "Name": "Summit Ridge IT Services LLC West",
                               "ContactStatus": "ACTIVE"}
    xero.contacts["c-real"] = {"ContactID": "c-real", "Name": "SUMMIT RIDGE IT SERVICES LLC", "ContactStatus": "ACTIVE"}
    p, _ = pusher(tmp_path)
    out = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    assert out.contact_id == "c-real" and not out.contact_created and len(xero.contacts) == 2


def test_same_contact_and_number_never_creates_a_second_bill(xero, tmp_path):
    p, _ = pusher(tmp_path)
    plan = lambda: plan_xero_bill(make_result(), summit_lines(), RULES)
    first = p.push(plan())
    second = p.push(plan())
    assert second.status == "duplicate" and second.invoice_id == first.invoice_id and len(xero.invoices) == 1
    assert "nothing created" in second.reasons[0]


def test_a_deleted_bill_is_not_a_duplicate(xero, tmp_path):
    p, client = pusher(tmp_path)
    first = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    client.delete_draft(first.invoice_id)
    again = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    assert again.status == "created" and len(xero.invoices) == 2


def test_same_number_from_another_contact_is_not_a_duplicate(xero, tmp_path):
    p, _ = pusher(tmp_path)
    p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    other = p.push(plan_xero_bill(make_result(supplier_name=answer("Harborline Print Co.")), summit_lines(), RULES))
    assert other.status == "created" and len(xero.invoices) == 2


def test_number_with_a_comma_is_checked_by_paging_not_the_list_filter(xero, tmp_path):
    p, _ = pusher(tmp_path)
    plan = lambda: plan_xero_bill(make_result(invoice_number=answer("SR-1,482")), summit_lines(), RULES)
    p.push(plan())
    assert p.push(plan()).status == "duplicate"
    queries = [parse_qs(urlparse(u).query) for m, u in xero.calls if m == "GET" and "/Invoices?" in u]
    assert all("InvoiceNumbers" not in q and q["page"] == ["1"] for q in queries)


def test_foreign_currency_is_held_before_any_contact_is_made(xero, tmp_path):
    xero.base_currency = "GBP"
    p, _ = pusher(tmp_path)
    out = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    assert out.status == "held" and "base currency is GBP" in out.reasons[0]
    assert xero.contacts == {} and "base currency is GBP" in (tmp_path / "review.csv").read_text()


def test_held_invoice_goes_to_review_list_with_no_api_calls(xero, tmp_path):
    p, _ = pusher(tmp_path)
    out = p.push(plan_xero_bill(make_result(invoice_date=answer("2026-10-03", "low")), summit_lines(), RULES))
    assert out.status == "held" and xero.calls == []
    assert "summit_it_invoice.pdf" in (tmp_path / "review.csv").read_text()


def test_unknown_account_code_falls_back_to_default_and_says_so(tmp_path):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        fake = FakeXero(rsps, accounts=[a for a in ACCOUNTS if a[0] != "473"])
        p, _ = pusher(tmp_path)
        out = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
        assert out.status == "created"
        assert [li["AccountCode"] for li in fake.invoices[out.invoice_id]["LineItems"]] == ["412", "485", "429", "429"]
        assert any("account 473" in n and "used the default 429" in n for n in out.notes)


def test_missing_default_account_stops_with_a_config_error(tmp_path):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        FakeXero(rsps, accounts=[("200", "Sales", "REVENUE")])
        p, _ = pusher(tmp_path)
        with pytest.raises(RuntimeError, match="default account code 429"):
            p.push(plan_xero_bill(make_result(), summit_lines(), RULES))


def test_tax_type_mode_posts_exclusive_lines_when_the_rate_matches(tmp_path):
    rules = tax_type_rules(tmp_path, "INPUT2")
    result = make_result(currency=answer("USD"), tax_amount=answer(380.0), total_amount=answer(2280.0))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        fake = FakeXero(rsps)
        p, _ = pusher(tmp_path, rules=rules)
        out = p.push(plan_xero_bill(result, summit_lines(), rules))
        bill = fake.invoices[out.invoice_id]
    assert out.status == "created" and out.verified
    assert bill["LineAmountTypes"] == "Exclusive" and {li["TaxType"] for li in bill["LineItems"]} == {"INPUT2"}
    assert bill["TotalTax"] == 380.0


def test_tax_type_mode_holds_when_the_rate_does_not_give_the_invoice_tax(xero, tmp_path):
    rules = tax_type_rules(tmp_path, "INPUT2")
    p, _ = pusher(tmp_path, rules=rules)
    out = p.push(plan_xero_bill(make_result(), summit_lines(), rules))
    assert out.status == "held" and "gives 380.00 but the invoice shows 30.53" in out.reasons[0]
    assert xero.invoices == {}


def test_tax_type_mode_holds_on_an_unknown_tax_type(xero, tmp_path):
    rules = tax_type_rules(tmp_path, "NOPE")
    p, _ = pusher(tmp_path, rules=rules)
    assert "not an active tax rate" in p.push(plan_xero_bill(make_result(), summit_lines(), rules)).reasons[0]


def test_only_draft_or_submitted_is_allowed(tmp_path):
    with pytest.raises(ValueError):
        pusher(tmp_path, status="AUTHORISED")


# ---- client: tokens, errors, limits -----------------------------------------------

def test_refresh_token_is_saved_before_the_first_api_call(xero):
    s = store()
    client = XeroClient(s)
    client.organisation()
    assert s.saved == [("refresh_token", "rt-1")] and xero.events == ["token1", "api"]
    assert xero.token_bodies[0] == {"grant_type": "refresh_token", "refresh_token": "rt-0"}


def test_401_refreshes_once_saves_the_new_token_and_retries(xero):
    s = store()
    client = XeroClient(s)
    client.organisation()
    xero.unauthorised = 1
    client.accounts()
    assert client.refreshes == 2 and s.values["refresh_token"] == "rt-2"


def test_401_twice_is_a_clear_error_not_a_loop(xero):
    client = XeroClient(store())
    xero.unauthorised = 5
    with pytest.raises(XeroAuthError, match="disconnected"):
        client.organisation()
    assert xero.token_calls == 2


def test_revoked_refresh_token_asks_to_connect_again(xero):
    with pytest.raises(ReauthorisationNeeded, match="--connect"):
        XeroClient(store("revoked-1")).organisation()


def test_429_waits_for_retry_after_then_succeeds(xero):
    waits = []
    client = XeroClient(store(), sleep=waits.append)
    xero.rate_limited = [(3, "minute"), (2, "minute")]
    assert client.organisation()["BaseCurrency"] == "USD"
    assert waits == [3.0, 2.0] and client.waited == 5.0


def test_429_daily_limit_stops_instead_of_sleeping_for_hours(xero):
    waits = []
    client = XeroClient(store(), sleep=waits.append)
    xero.rate_limited = [(43200, "day")]
    with pytest.raises(RateLimited, match="day limit.*43200s") as exc:
        client.organisation()
    assert waits == [] and exc.value.problem == "day"


def test_429_without_retry_after_and_endless_429_both_stop(xero):
    client = XeroClient(store(), sleep=lambda s: None)
    xero.rate_limited = [(None, "concurrent")]
    with pytest.raises(RateLimited, match="concurrent"):
        client.organisation()
    xero.rate_limited = [(1, "minute")] * 10
    with pytest.raises(RateLimited):
        client.organisation()


def test_400_validation_errors_are_readable(xero, tmp_path):
    xero.reject_invoice = {"ErrorNumber": 10, "Type": "ValidationException",
                           "Message": "A validation exception occurred",
                           "Elements": [{"ValidationErrors": [{"Message": "Account code '999' is not a valid code "
                                                                         "for this document."}]}]}
    p, _ = pusher(tmp_path)
    with pytest.raises(XeroValidationError) as exc:
        p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    assert "Account code '999' is not a valid code" in str(exc.value)
    assert "A validation exception occurred" in str(exc.value)


def test_400_with_a_bare_list_body_is_readable():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, TOKEN_URL, json={"access_token": "a", "refresh_token": "rt-1"})
        rsps.add(responses.GET, f"{API_BASE}/Organisation", status=400,
                 json=[{"Message": "Insufficient stock. Items are eligible for backorder."}])
        with pytest.raises(XeroValidationError, match="backorder"):
            XeroClient(store()).organisation()


def test_403_names_the_scope_problem():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, TOKEN_URL, json={"access_token": "a", "refresh_token": "rt-1"})
        rsps.add(responses.GET, f"{API_BASE}/Accounts", status=403, body="")
        with pytest.raises(XeroAuthError, match="scope"):
            XeroClient(store()).accounts()


def test_token_reply_without_refresh_token_is_refused():
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, TOKEN_URL, json={"access_token": "a", "expires_in": 1800})
        with pytest.raises(XeroError, match="offline_access"):
            XeroClient(store()).organisation()


def test_secret_and_token_values_never_appear_in_errors(xero):
    s = store("revoked-SECRETVALUE")
    for call in (lambda: XeroClient(s).organisation(),):
        with pytest.raises(XeroError) as exc:
            call()
        assert "SECRETVALUE" not in str(exc.value) and "csecret" not in str(exc.value)
    xero.unauthorised = 9
    with pytest.raises(XeroError) as exc:
        XeroClient(store()).organisation()
    assert "rt-" not in str(exc.value) and "csecret" not in str(exc.value)


def test_delete_draft_only_works_on_drafts(xero, tmp_path):
    p, client = pusher(tmp_path)
    out = p.push(plan_xero_bill(make_result(), summit_lines(), RULES))
    assert client.delete_draft(out.invoice_id)["Status"] == "DELETED"
    xero.invoices[out.invoice_id]["Status"] = "AUTHORISED"
    with pytest.raises(XeroValidationError, match="not of valid status"):
        client.delete_draft(out.invoice_id)


# ---- token stores --------------------------------------------------------------

def test_file_store_keeps_written_values_private_and_prefers_them(tmp_path):
    path = tmp_path / "t.json"
    s = FileTokenStore(path, environ={"XERO_REFRESH_TOKEN": "old", "XERO_CLIENT_ID": "cid"})
    assert s.get("refresh_token") == "old"
    s.save("refresh_token", "new-1")
    s.save("tenant_id", TENANT)
    assert s.get("refresh_token") == "new-1" and s.get("tenant_id") == TENANT
    assert (path.stat().st_mode & 0o777) == 0o600
    with pytest.raises(TokenStoreError, match="XERO_CLIENT_SECRET"):
        s.get("client_secret")


def test_stores_refuse_odd_values_and_never_write_the_secret(tmp_path):
    s = FileTokenStore(tmp_path / "t.json", environ={})
    with pytest.raises(TokenStoreError):
        s.save("refresh_token", 'x" ; rm -rf /')
    with pytest.raises(TokenStoreError, match="never written"):
        s.save("client_secret", "abc")


def test_keychain_store_writes_through_stdin_not_argv():
    seen = []

    def runner(cmd, **kw):
        seen.append((cmd, kw.get("input")))
        if cmd[1] == "find-generic-password":
            return SimpleNamespace(returncode=0, stdout="rt-9\n")
        return SimpleNamespace(returncode=0, stdout="")

    s = KeychainTokenStore(account="neo", runner=runner)
    s.save("refresh_token", "rt-9")
    write_cmd, stdin = seen[0]
    assert write_cmd == ["security", "-i"] and "rt-9" not in " ".join(write_cmd)
    assert '-a "neo" -s "xero-refresh-token" -w "rt-9"' in stdin
    assert s.get("refresh_token") == "rt-9"


# ---- connect flow ------------------------------------------------------------------

def free_port():
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def test_authorize_url_has_the_least_privilege_scopes_and_state():
    url = urlparse(authorize_url("cid", "http://localhost:8723/callback", "st8"))
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "login.xero.com" and url.path == "/identity/connect/authorize"
    assert q == {"response_type": "code", "client_id": "cid", "redirect_uri": "http://localhost:8723/callback",
                 "scope": "offline_access accounting.invoices accounting.contacts accounting.settings.read",
                 "state": "st8"}
    assert "accounting.transactions" not in q["scope"]  # deprecated broad scope


def test_pick_tenant_prefers_this_login_and_ignores_practice_manager():
    conns = FakeXero.connections(SimpleNamespace(), SimpleNamespace(headers={}))[2]
    conns = json.loads(conns)
    assert pick_tenant(conns, "evt-new", None)["tenantName"] == "Demo Company (Global)"
    with pytest.raises(XeroError, match="several"):
        pick_tenant(conns, None, None)
    assert pick_tenant(conns, None, "maple florist")["tenantId"].startswith("1111")
    with pytest.raises(XeroError, match="no connected organisation"):
        pick_tenant(conns, None, "Nope Ltd")


def test_jwt_claims_reads_the_auth_event_and_survives_junk():
    assert jwt_claims(fake_jwt({"authentication_event_id": "e1"}))["authentication_event_id"] == "e1"
    assert jwt_claims("not-a-jwt") == {}


def _hit(url):
    try:
        return urllib.request.urlopen(url, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_callback_with_the_wrong_state_is_refused():
    port = free_port()
    ready, errors = threading.Event(), []

    def serve():
        try:
            wait_for_code(f"http://localhost:{port}/callback", "right", timeout=5, ready=ready)
        except XeroError as exc:
            errors.append(str(exc))

    t = threading.Thread(target=serve)
    t.start()
    ready.wait(5)
    assert _hit(f"http://localhost:{port}/callback?code=abc&state=forged") == 400
    t.join(6)
    assert errors and "state did not match" in errors[0]


def test_redirect_must_be_localhost_not_127():
    with pytest.raises(XeroError, match="localhost"):
        wait_for_code("http://127.0.0.1:8723/callback", "s", timeout=1)


def test_connect_end_to_end_saves_refresh_token_and_tenant(xero):
    responses_passthru = xero.rsps
    port = free_port()
    responses_passthru.add_passthru(f"http://localhost:{port}")
    s = MemoryTokenStore(client_id="cid", client_secret="csecret")
    client = XeroClient(s)

    def browser(url):  # stands in for the person logging in and clicking Allow
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        threading.Thread(target=_hit, args=(f"http://localhost:{port}/callback?code=one-time&state={q['state']}"
                                            "&scope=offline_access",)).start()

    chosen = connect(client, s, f"http://localhost:{port}/callback", open_browser=browser, timeout=10,
                     printer=lambda *_: None)
    assert chosen["tenantId"] == TENANT and chosen["tenantName"] == "Demo Company (Global)"
    assert s.saved == [("refresh_token", "rt-1"), ("tenant_id", TENANT)]
    assert xero.token_bodies[0] == {"grant_type": "authorization_code", "code": "one-time",
                                    "redirect_uri": f"http://localhost:{port}/callback"}


# ---- CLI -------------------------------------------------------------------------------

def bundle(tmp_path, result=None, lines=None, suffix=".xero-input.json"):
    path = tmp_path / f"summit_it_invoice{suffix}"
    path.write_text(json.dumps({"extraction": (result or make_result()).to_dict(),
                                "line_items": [li.to_dict() for li in (lines or summit_lines())]}))
    return path


def test_cli_dry_run_then_push_then_duplicate(xero, tmp_path, capsys):
    b, out_dir = bundle(tmp_path), tmp_path / "out"
    assert cli.main([str(b), "--dry-run", "--out-dir", str(out_dir)]) == 0
    assert xero.calls == [] and '"InvoiceNumber": "SR-10482"' in capsys.readouterr().out
    client = XeroClient(store())
    assert cli.main([str(b), "--out-dir", str(out_dir), "--label", "[TEST]"], client=client) == 0
    assert cli.main([str(b), "--out-dir", str(out_dir)], client=client) == 0
    text = capsys.readouterr().out
    assert "1 created" in text and "1 duplicate" in text and len(xero.invoices) == 1
    assert "calls left:" in text
    assert json.loads((out_dir / "push-summary.json").read_text())[0]["status"] == "duplicate"


def test_cli_takes_quickbooks_bundles_too(xero, tmp_path, capsys):
    b = bundle(tmp_path, suffix=".qbo-input.json")
    assert cli.main([str(b), "--out-dir", str(tmp_path / "o")], client=XeroClient(store())) == 0
    assert len(xero.invoices) == 1


def test_cli_extracts_documents_with_fakes_and_saves_the_bundle(xero, tmp_path, make_samples, capsys):
    make_samples.summit_it_pdf(tmp_path / "summit_it_invoice.pdf")

    class Fields:
        def extract_fields(self, text, schema):
            return {k: {"value": f.value, "confidence": f.confidence, "source_snippet": f.source_snippet}
                    for k, f in make_result(invoice_date=answer("2026-10-28", snippet="October 28, 2026"),
                                            due_date=answer("2026-11-27", snippet="November 27, 2026"),
                                            subtotal=answer(1900.0, snippet="Subtotal $1,900.00"),
                                            tax_amount=answer(30.53, snippet="Sales tax $30.53"),
                                            total_amount=answer(1930.53, snippet="Total due (USD) $1,930.53"),
                                            currency=answer("USD", snippet="Total due (USD)")).fields.items()}

    class Lines:
        def extract_lines(self, text):
            return [{"description": li.description, "quantity": li.quantity, "unit_price": li.unit_price,
                     "amount": li.amount, "confidence": "high", "source_snippet": li.description}
                    for li in summit_lines()]

    out_dir = tmp_path / "out"
    code = cli.main([str(tmp_path / "summit_it_invoice.pdf"), "--out-dir", str(out_dir)],
                    client=XeroClient(store()), field_extractor=Fields(), line_extractor=Lines())
    assert code == 0, capsys.readouterr()
    saved = json.loads((out_dir / "summit_it_invoice.xero-input.json").read_text())
    assert [li["confidence"] for li in saved["line_items"]] == ["high", "high", "high"]
    assert len(xero.invoices) == 1


def test_cli_stops_everything_on_invalid_grant(xero, tmp_path, capsys):
    b = bundle(tmp_path)
    code = cli.main([str(b), str(b), "--out-dir", str(tmp_path / "o")], client=XeroClient(store("revoked")))
    assert code == 3 and "--connect" in capsys.readouterr().err


def test_cli_stops_on_the_daily_rate_limit(xero, tmp_path, capsys):
    xero.rate_limited = [(40000, "day")]
    code = cli.main([str(bundle(tmp_path)), "--out-dir", str(tmp_path / "o")], client=XeroClient(store()))
    assert code == 3 and "day limit" in capsys.readouterr().err


def test_cli_config_error_stops_cleanly(tmp_path, capsys):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        FakeXero(rsps, accounts=[("200", "Sales", "REVENUE")])
        code = cli.main([str(bundle(tmp_path)), "--out-dir", str(tmp_path / "o")], client=XeroClient(store()))
    assert code == 2 and "default account code 429" in capsys.readouterr().err


def test_cli_show_setup_and_delete(xero, tmp_path, capsys):
    client = XeroClient(store())
    assert cli.main(["--show-setup"], client=client) == 0
    text = capsys.readouterr().out
    assert "base currency USD" in text and "461    Printing & Stationery" in text and "INPUT2" in text
    assert "200" not in text.split("Active expense accounts")[1].split("Active tax")[0]  # revenue not listed
    cli.main([str(bundle(tmp_path)), "--out-dir", str(tmp_path / "o")], client=client)
    bill_id = next(iter(xero.invoices))
    assert cli.main(["--delete-bill", bill_id], client=client) == 0
    assert xero.invoices[bill_id]["Status"] == "DELETED"
