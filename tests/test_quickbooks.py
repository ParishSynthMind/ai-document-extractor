"""QuickBooks bill push. Every HTTP call goes to a fake QuickBooks (the `responses`
library intercepts requests), so no network, keys or Intuit account are needed."""
import json
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import responses
from conftest import answer

from doc_extractor.extract import ExtractionResult, FieldResult
from doc_extractor.quickbooks import cli
from doc_extractor.quickbooks.bills import AccountRules, BillPusher, load_account_rules, plan_bill, summary_text
from doc_extractor.quickbooks.client import (
    API_BASES,
    TOKEN_URL,
    QBOAuthError,
    QBOClient,
    QBOValidationError,
    ReauthorisationNeeded,
    quote,
)
from doc_extractor.quickbooks.line_items import LineItem, check_line_items
from doc_extractor.quickbooks.tokens import FileTokenStore, KeychainTokenStore, TokenStoreError

REALM = "1234567890"
BASE = f"{API_BASES['sandbox']}/v3/company/{REALM}"
RULES = load_account_rules(cli.DEFAULT_ACCOUNTS)


class MemoryStore:
    def __init__(self, refresh_token="rt-0"):
        self.values = {"client_id": "cid", "client_secret": "secret", "realm_id": REALM,
                       "refresh_token": refresh_token}
        self.saved = []

    def get(self, key):
        return self.values[key]

    def save_refresh_token(self, token):
        self.saved.append(token)
        self.values["refresh_token"] = token


class FakeQBO:
    """Just enough QuickBooks to exercise every path: tokens, query, create, read."""

    def __init__(self, rsps, accounts=("Uncategorized Expense", "Taxes & Licenses", "Office Expenses",
                                       "Stationery & Printing", "Advertising", "Supplies")):
        self.rsps = rsps
        self.vendors, self.bills = {}, {}
        self.accounts = {name: {"Id": str(i), "Name": name, "AccountType": "Expense"}
                         for i, name in enumerate(accounts, start=10)}
        self.next_id = 100
        self.token_calls = 0
        self.unauthorised = 0  # answer this many API calls with 401
        self.reject_bill = None  # a Fault body to answer bill creation with
        self.calls = []
        rsps.add_callback(responses.POST, TOKEN_URL, callback=self.token)
        rsps.add_callback(responses.GET, re.compile(re.escape(BASE) + r"/query.*"), callback=self.query)
        rsps.add_callback(responses.POST, re.compile(re.escape(BASE) + r"/(vendor|bill)\?.*"), callback=self.create)
        rsps.add_callback(responses.GET, re.compile(re.escape(BASE) + r"/bill/\d+.*"), callback=self.read)

    def _id(self):
        self.next_id += 1
        return str(self.next_id)

    def token(self, request):
        self.token_calls += 1
        body = parse_qs(request.body)
        assert body["grant_type"] == ["refresh_token"]
        assert request.headers["Authorization"].startswith("Basic ")
        if body["refresh_token"][0].startswith("revoked"):
            return 400, {}, json.dumps({"error": "invalid_grant"})
        n = self.token_calls
        return 200, {}, json.dumps({"access_token": f"at-{n}", "refresh_token": f"rt-{n}", "expires_in": 3600})

    def _auth(self, request):
        self.calls.append((request.method, request.url))
        assert parse_qs(urlparse(request.url).query)["minorversion"] == ["75"]
        if self.unauthorised:
            self.unauthorised -= 1
            return 401, {}, json.dumps({"fault": {"error": [{"message": "AuthenticationFailed", "code": "100"}]}})
        return None

    def query(self, request):
        if (bad := self._auth(request)) is not None:
            return bad
        q = parse_qs(urlparse(request.url).query)["query"][0]
        m = re.search(r"from (\w+)(?: where (\w+) = '((?:[^'\\]|\\.)*)')?", q)
        entity, value = m.group(1), (m.group(3) or "").replace("\\'", "'")
        if entity == "Preferences":
            rows = [{"CurrencyPrefs": {"MultiCurrencyEnabled": False, "HomeCurrency": {"value": "USD"}}}]
        elif entity == "Vendor":
            rows = [v for v in self.vendors.values() if v["DisplayName"] == value]
        elif entity == "Bill":
            rows = [b for b in self.bills.values() if b.get("DocNumber") == value]
        else:
            rows = [a for a in self.accounts.values() if a["Name"] == value]
        return 200, {}, json.dumps({"QueryResponse": {entity: rows} if rows else {}})

    def create(self, request):
        if (bad := self._auth(request)) is not None:
            return bad
        body = json.loads(request.body)
        if "/vendor?" in request.url:
            v = {**body, "Id": self._id(), "SyncToken": "0"}
            self.vendors[v["Id"]] = v
            return 200, {}, json.dumps({"Vendor": v})
        if self.reject_bill:
            return 400, {"intuit_tid": "tid-123"}, json.dumps(self.reject_bill)
        b = {**body, "Id": self._id(), "SyncToken": "0",
             "TotalAmt": round(sum(line["Amount"] for line in body["Line"]), 2)}
        self.bills[b["Id"]] = b
        return 200, {}, json.dumps({"Bill": b})

    def read(self, request):
        if (bad := self._auth(request)) is not None:
            return bad
        bill_id = re.search(r"/bill/(\d+)", request.url).group(1)
        return 200, {}, json.dumps({"Bill": self.bills[bill_id]})


@pytest.fixture
def qbo():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeQBO(rsps)


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
    return ExtractionResult("summit_it_invoice.pdf", "invoice", "pdf-text", 1, fields, review, "2026-11-04T00:00:00Z")


def summit_lines():
    return [LineItem("Managed IT support, October 2026", 1200.0, 1, 1200.0, "high"),
            LineItem("Microsoft 365 Business licences", 330.0, 15, 22.0, "high"),
            LineItem("Laptop repair (screen replacement)", 370.0, 2, 185.0, "high")]


def pusher(qbo_fake, tmp_path, store=None):
    client = QBOClient(store or MemoryStore(), "sandbox")
    return BillPusher(client, RULES, review_file=tmp_path / "review.csv", label="[TEST]"), client


# ---- plan (no API) ---------------------------------------------------------

def test_plan_maps_lines_to_accounts_and_adds_tax_line():
    plan = plan_bill(make_result(), summit_lines(), RULES, "USD")
    assert plan.hold_reasons == []
    assert [(line.account, line.amount) for line in plan.lines] == [
        ("Legal & Professional Fees", 1200.0), ("Dues & Subscriptions", 330.0),
        ("Computer Repairs", 370.0), ("Taxes & Licenses", 30.53)]
    assert "currency was medium confidence (USD)" in plan.notes


def test_keywords_match_whole_words_only():
    rules = AccountRules("Default", "Tax", [(("sign",), "Advertising")])
    assert rules.account_for("Design proof and setup")[0] == "Default"
    assert rules.account_for("Shop sign, painted")[0] == "Advertising"


@pytest.mark.parametrize("override, reason", [
    ({"invoice_date": answer("2026-03-10", "low")}, "invoice_date"),
    ({"invoice_number": answer(None)}, "invoice_number: missing"),
    ({"currency": answer("GBP")}, "currency: invoice is GBP"),
    ({"total_amount": answer(1999.0)}, "does not equal total"),
    ({"invoice_number": answer("X" * 22)}, "21-character limit"),
])
def test_doubtful_invoices_are_held_not_guessed(override, reason):
    plan = plan_bill(make_result(**override), summit_lines(), RULES, "USD")
    assert any(reason in r for r in plan.hold_reasons), plan.hold_reasons


def test_lines_that_do_not_add_up_are_held():
    lines = summit_lines()[:2]
    plan = plan_bill(make_result(), lines, RULES, "USD")
    assert any("add up to 1530.00" in r for r in plan.hold_reasons)


def test_low_confidence_line_is_held():
    lines = summit_lines()
    lines[1].confidence, lines[1].checks = "low", ["source snippet not found in the document"]
    plan = plan_bill(make_result(), lines, RULES, "USD")
    assert any(r.startswith("line 2") for r in plan.hold_reasons)


def test_review_only_fields_the_bill_does_not_use_do_not_block():
    result = make_result(bank_iban=answer("AE07", "low"))
    assert plan_bill(result, summit_lines(), RULES, "USD").hold_reasons == []


def test_line_item_checks():
    doc = "Widgets 2 $10.00 $20.00\nDelivery 1 15 15\nSubtotal $35.00"
    lines = check_line_items([
        {"description": "Widgets", "quantity": 2, "unit_price": 10.0, "amount": 20.0, "confidence": "high",
         "source_snippet": "Widgets 2 $10.00 $20.00"},
        {"description": "Delivery", "quantity": None, "unit_price": None, "amount": 15.0, "confidence": "high",
         "source_snippet": "Delivery 1 15 15"},
        {"description": "Gadgets", "quantity": 3, "unit_price": 5.0, "amount": 99.0, "confidence": "high",
         "source_snippet": "Gadgets 3 5 99"},
    ], doc)
    assert [li.confidence for li in lines] == ["high", "high", "low"]
    assert "source snippet not found in the document" in lines[2].checks
    assert "amount not found as written in the document" in lines[2].checks
    assert "3 x 5.0 does not equal 99.0" in lines[2].checks


def test_query_values_are_escaped():
    assert quote("O'Brien\\Sons") == "O\\'Brien\\\\Sons"


# ---- push paths (fake QuickBooks over HTTP) --------------------------------

def test_dry_run_makes_no_http_calls(tmp_path):
    with responses.RequestsMock() as rsps:  # any request at all would raise
        p = BillPusher(QBOClient(MemoryStore()), RULES)
        out = p.push(plan_bill(make_result(), summit_lines(), RULES, "USD"), dry_run=True)
        assert len(rsps.calls) == 0
    assert out.status == "dry-run"
    assert out.payload["DocNumber"] == "SR-10482" and len(out.payload["Line"]) == 4
    assert out.payload["VendorRef"]["name"] == "Summit Ridge IT Services LLC"


def test_creates_vendor_and_bill_then_reads_it_back(qbo, tmp_path):
    p, client = pusher(qbo, tmp_path)
    out = p.push(plan_bill(make_result(), summit_lines(), RULES, p.home_currency()))
    assert out.status == "created" and out.vendor_created and out.verified
    bill = qbo.bills[out.bill_id]
    assert bill["VendorRef"]["value"] == out.vendor_id
    assert bill["TotalAmt"] == 1930.53 and bill["DueDate"] == "2026-11-27"
    assert bill["PrivateNote"].startswith("[TEST] Created by ai-document-extractor")
    # two accounts in the rules don't exist in this company: both fall back to the default, and say so
    used = {line["Description"]: line["AccountBasedExpenseLineDetail"]["AccountRef"]["name"] for line in bill["Line"]}
    assert used["Managed IT support, October 2026"] == "Uncategorized Expense"
    assert used["Tax shown on the invoice"] == "Taxes & Licenses"
    assert any("'Legal & Professional Fees' not found; used the default" in n for n in out.notes)
    assert any(m == "GET" and "/bill/" in url for m, url in qbo.calls)  # the read-back happened
    assert "CREATED" in summary_text(out)


def test_existing_vendor_is_reused(qbo, tmp_path):
    qbo.vendors["7"] = {"Id": "7", "DisplayName": "Summit Ridge IT Services LLC"}
    p, _ = pusher(qbo, tmp_path)
    out = p.push(plan_bill(make_result(), summit_lines(), RULES, "USD"))
    assert out.status == "created" and not out.vendor_created and out.vendor_id == "7"
    assert len(qbo.vendors) == 1


def test_same_vendor_and_invoice_number_never_creates_a_second_bill(qbo, tmp_path):
    p, _ = pusher(qbo, tmp_path)
    first = p.push(plan_bill(make_result(), summit_lines(), RULES, "USD"))
    again = p.push(plan_bill(make_result(), summit_lines(), RULES, "USD"))
    assert first.status == "created" and again.status == "duplicate"
    assert again.bill_id == first.bill_id and len(qbo.bills) == 1


def test_same_invoice_number_from_another_vendor_is_not_a_duplicate(qbo, tmp_path):
    qbo.vendors["7"] = {"Id": "7", "DisplayName": "Summit Ridge IT Services LLC"}
    qbo.bills["50"] = {"Id": "50", "DocNumber": "SR-10482", "VendorRef": {"value": "999"}}
    p, _ = pusher(qbo, tmp_path)
    assert p.push(plan_bill(make_result(), summit_lines(), RULES, "USD")).status == "created"


def test_held_invoice_goes_to_review_list_with_no_api_calls(qbo, tmp_path):
    p, _ = pusher(qbo, tmp_path)
    out = p.push(plan_bill(make_result(invoice_date=answer("2026-03-10", "low")), summit_lines(), RULES, "USD"))
    assert out.status == "held" and qbo.calls == [] and qbo.vendors == {} and qbo.bills == {}
    review = (tmp_path / "review.csv").read_text()
    assert "SR-10482" in review and "invoice_date: model marked it low confidence" in review


def test_401_refreshes_once_saves_the_new_token_and_retries(qbo, tmp_path):
    store = MemoryStore()
    p, client = pusher(qbo, tmp_path, store)
    p.home_currency()  # first refresh happens here
    qbo.unauthorised = 1  # the access token "expires"
    out = p.push(plan_bill(make_result(), summit_lines(), RULES, "USD"))
    assert out.status == "created"
    assert client.refreshes == 2 and store.saved == ["rt-1", "rt-2"]
    assert store.values["refresh_token"] == "rt-2"


def test_401_twice_is_a_clear_error_not_a_loop(qbo, tmp_path):
    p, client = pusher(qbo, tmp_path)
    qbo.unauthorised = 5
    with pytest.raises(QBOAuthError, match="401 even with a fresh access token"):
        p.home_currency()
    assert client.refreshes == 2


def test_400_validation_error_is_readable(qbo, tmp_path):
    qbo.reject_bill = {"Fault": {"Error": [{"Message": "Invalid Reference Id", "Detail":
                                            "Invalid Reference Id : Accounts element id 99 not found",
                                            "code": "2500"}], "type": "ValidationFault"}}
    p, _ = pusher(qbo, tmp_path)
    with pytest.raises(QBOValidationError) as exc:
        p.push(plan_bill(make_result(), summit_lines(), RULES, "USD"))
    msg = str(exc.value)
    assert "HTTP 400" in msg and "Invalid Reference Id" in msg and "code 2500" in msg and "tid-123" in msg
    assert qbo.bills == {}


def test_revoked_refresh_token_asks_for_reauthorisation(qbo):
    client = QBOClient(MemoryStore("revoked"))
    with pytest.raises(ReauthorisationNeeded, match="invalid_grant"):
        client.query("select * from Preferences")


def test_token_value_never_appears_in_errors(qbo):
    client = QBOClient(MemoryStore("revoked-tok-7f3a"))
    with pytest.raises(ReauthorisationNeeded) as exc:
        client.refresh()
    assert "tok-7f3a" not in str(exc.value) and "secret" not in str(exc.value)


# ---- token stores ------------------------------------------------------------

def test_file_store_keeps_rotated_token_private_and_prefers_it(tmp_path):
    env = {"QBO_CLIENT_ID": "cid", "QBO_CLIENT_SECRET": "s", "QBO_REALM_ID": REALM, "QBO_REFRESH_TOKEN": "rt-env"}
    store = FileTokenStore(tmp_path / "tokens.json", environ=env)
    assert store.get("refresh_token") == "rt-env"
    store.save_refresh_token("rt-new.1")
    assert store.get("refresh_token") == "rt-new.1"  # the newer token wins over the env seed
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(TokenStoreError):
        store.save_refresh_token('bad" token')


def test_file_store_rotation_through_the_client(qbo, tmp_path):
    env = {"QBO_CLIENT_ID": "cid", "QBO_CLIENT_SECRET": "s", "QBO_REALM_ID": REALM, "QBO_REFRESH_TOKEN": "rt-0"}
    store = FileTokenStore(tmp_path / "tokens.json", environ=env)
    QBOClient(store).query("select * from Preferences")
    assert json.loads((tmp_path / "tokens.json").read_text()) == {"refresh_token": "rt-1"}


def test_keychain_store_writes_through_stdin_not_argv():
    calls, keychain = [], {}

    def runner(cmd, input=None, **kw):
        calls.append((cmd, input))
        if cmd[:2] == ["security", "-i"]:
            keychain[re.search(r'-s "([^"]+)"', input).group(1)] = re.search(r'-w "([^"]+)"', input).group(1)
            return SimpleNamespace(returncode=0, stdout="")
        item = cmd[cmd.index("-s") + 1]
        return SimpleNamespace(returncode=0 if item in keychain else 44, stdout=keychain.get(item, ""))

    store = KeychainTokenStore(account="acct", runner=runner)
    store.save_refresh_token("rt-9")
    assert store.get("refresh_token") == "rt-9"
    assert all("rt-9" not in " ".join(cmd) for cmd, _ in calls)  # never on a command line
    with pytest.raises(TokenStoreError, match="intuit-client-id"):
        store.get("client_id")


# ---- command line ------------------------------------------------------------

def bundle(tmp_path, result=None, lines=None):
    path = tmp_path / "summit_it_invoice.qbo-input.json"
    path.write_text(json.dumps({"extraction": (result or make_result()).to_dict(),
                                "line_items": [li.to_dict() for li in (lines or summit_lines())]}))
    return path


def test_cli_dry_run_then_push_then_duplicate(qbo, tmp_path, capsys):
    b, out_dir = bundle(tmp_path), tmp_path / "out"
    assert cli.main([str(b), "--dry-run", "--out-dir", str(out_dir)]) == 0
    assert qbo.calls == [] and '"DocNumber": "SR-10482"' in capsys.readouterr().out
    client = QBOClient(MemoryStore())
    assert cli.main([str(b), "--out-dir", str(out_dir), "--label", "[TEST]"], client=client) == 0
    assert cli.main([str(b), "--out-dir", str(out_dir)], client=client) == 0
    text = capsys.readouterr().out
    assert "1 created" in text and "1 duplicate" in text and len(qbo.bills) == 1
    summary = json.loads((out_dir / "push-summary.json").read_text())
    assert summary[0]["status"] == "duplicate"


def test_cli_extracts_documents_with_fakes_and_saves_the_bundle(qbo, sample_dir, tmp_path, make_samples, capsys):
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
                    client=QBOClient(MemoryStore()), field_extractor=Fields(), line_extractor=Lines())
    assert code == 0, capsys.readouterr()
    saved = json.loads((out_dir / "summit_it_invoice.qbo-input.json").read_text())
    assert [li["confidence"] for li in saved["line_items"]] == ["high", "high", "high"]
    assert len(qbo.bills) == 1


def test_cli_stops_everything_on_invalid_grant(qbo, tmp_path, capsys):
    b = bundle(tmp_path)
    code = cli.main([str(b), str(b), "--out-dir", str(tmp_path / "o")], client=QBOClient(MemoryStore("revoked")))
    assert code == 3 and "Re-authorise" in capsys.readouterr().err
