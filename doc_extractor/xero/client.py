"""A small Xero Accounting API (2.0) client.

Only what the bill push needs: token refresh, GET, PUT, POST. Access tokens
live in memory only (they last 30 minutes).
- 401: one refresh and one retry; a second 401 is an error, not a loop.
- 429: wait for the Retry-After seconds Xero sends, up to a cap, then retry.
  A wait longer than the cap (the daily limit) stops with a clear message.
- 400: Xero's ValidationErrors turned into a readable list.
"""
from __future__ import annotations

import time

import requests

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
API_BASE = "https://api.xero.com/api.xro/2.0"


class XeroError(RuntimeError):
    pass


class ReauthorisationNeeded(XeroError):
    """The refresh token itself was refused. Only a person can fix this, once."""


class XeroAuthError(XeroError):
    pass


class RateLimited(XeroError):
    def __init__(self, problem: str | None, retry_after: float | None):
        self.problem, self.retry_after = problem, retry_after
        wait = "" if retry_after is None else f"; Xero says retry in {retry_after:.0f}s"
        super().__init__(f"Xero rate limit reached ({problem or 'unknown'} limit){wait}")


class XeroValidationError(XeroError):
    def __init__(self, status: int, messages: list[str], detail: str = ""):
        self.status = status
        self.messages = messages
        lines = [f"Xero rejected the request (HTTP {status}){': ' + detail if detail else ''}:"]
        lines += [f"  - {m}" for m in messages] or ["  - (no validation message given)"]
        super().__init__("\n".join(lines))


def validation_messages(body) -> list[str]:
    """Pull every ValidationErrors message out of a Xero 400 body (or a summarizeErrors=false reply)."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for err in node.get("ValidationErrors") or []:
                msg = err.get("Message") or err.get("Description")
                if msg and msg not in found:
                    found.append(msg)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(body)
    return found


def _retry_after(resp) -> float | None:
    try:
        return float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


class XeroClient:
    def __init__(self, store, session=None, timeout: float = 30, max_wait: float = 65, sleep=time.sleep,
                 max_429_retries: int = 3):
        self.store = store
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_wait = max_wait
        self.sleep = sleep
        self.max_429_retries = max_429_retries
        self._access_token: str | None = None
        self._tenant: str | None = None
        self.refreshes = 0
        self.waited = 0.0
        self.limits: dict[str, str] = {}

    @property
    def tenant_id(self) -> str:
        if self._tenant is None:
            self._tenant = self.store.get("tenant_id")
        return self._tenant

    # ---- tokens -----------------------------------------------------------
    def _token_post(self, data: dict) -> dict:
        resp = self.session.post(
            TOKEN_URL,
            auth=(self.store.get("client_id"), self.store.get("client_secret")),
            data=data,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", "")
            except ValueError:
                err = ""
            if err == "invalid_grant":
                raise ReauthorisationNeeded(
                    "Xero refused the stored refresh token (invalid_grant): it is over 60 days old, was "
                    "revoked, or was replaced more than 30 minutes ago. Run `xero-push --connect` once.")
            raise XeroError(f"Xero token request failed: HTTP {resp.status_code} {err}".strip())
        tokens = resp.json()
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise XeroError("Xero's token reply had no access or refresh token in it (was offline_access asked for?)")
        self.store.save("refresh_token", tokens["refresh_token"])  # before anything else can fail
        self._access_token = tokens["access_token"]
        return tokens

    def refresh(self) -> None:
        """Swap the refresh token for a new access token, saving the new refresh token first."""
        self._token_post({"grant_type": "refresh_token", "refresh_token": self.store.get("refresh_token")})
        self.refreshes += 1

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        """The one-time step of the connect flow: authorisation code -> tokens (refresh token saved)."""
        return self._token_post({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})

    # ---- calls ------------------------------------------------------------
    def _send(self, method: str, url: str, params=None, body=None, tenant: bool = True):
        if self._access_token is None:
            self.refresh()
        retries_429 = 0
        refreshed = False
        while True:
            headers = {"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"}
            if tenant:
                headers["xero-tenant-id"] = self.tenant_id
            if body is not None:
                headers["Content-Type"] = "application/json"
            resp = self.session.request(method, url, params=params, json=body, headers=headers, timeout=self.timeout)
            for h in ("X-DayLimit-Remaining", "X-MinLimit-Remaining", "X-AppMinLimit-Remaining"):
                if h in resp.headers:
                    self.limits[h] = resp.headers[h]
            if resp.status_code == 401 and not refreshed:
                self.refresh()  # access tokens last 30 minutes; get a fresh one and try once more
                refreshed = True
                continue
            if resp.status_code == 429:
                wait, problem = _retry_after(resp), resp.headers.get("X-Rate-Limit-Problem")
                if wait is None or wait > self.max_wait or retries_429 >= self.max_429_retries:
                    raise RateLimited(problem, wait)
                self.sleep(wait)
                self.waited += wait
                retries_429 += 1
                continue
            break
        if resp.status_code == 401:
            raise XeroAuthError("Xero returned 401 even with a fresh access token: the organisation may have "
                                "disconnected the app, or the tenant id is not one of this connection's")
        if resp.status_code == 403:
            raise XeroAuthError("Xero returned 403: the app was not granted the scope this call needs "
                                "(see ACCOUNT-NEEDS / README for the scope list)")
        if resp.status_code == 400:
            try:
                body_json = resp.json()
            except ValueError:
                raise XeroValidationError(400, [resp.text[:300]]) from None
            if isinstance(body_json, list):  # some 400s are a bare list of {"Message": ...}
                raise XeroValidationError(400, [m.get("Message", str(m)) for m in body_json if isinstance(m, dict)])
            raise XeroValidationError(400, validation_messages(body_json), body_json.get("Message", ""))
        if resp.status_code >= 400:
            raise XeroError(f"Xero HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def connections(self) -> list[dict]:
        return self._send("GET", CONNECTIONS_URL, tenant=False).json()

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._send("GET", f"{API_BASE}/{path}", params=params).json()

    def put(self, path: str, body: dict, params: dict | None = None) -> dict:
        return self._send("PUT", f"{API_BASE}/{path}", params=params, body=body).json()

    def post(self, path: str, body: dict, params: dict | None = None) -> dict:
        return self._send("POST", f"{API_BASE}/{path}", params=params, body=body).json()

    # ---- the few resources the push uses -----------------------------------
    def organisation(self) -> dict:
        rows = self.get("Organisation").get("Organisations") or []
        return rows[0] if rows else {}

    def accounts(self) -> list[dict]:
        return self.get("Accounts").get("Accounts") or []

    def tax_rates(self) -> list[dict]:
        return self.get("TaxRates").get("TaxRates") or []

    def search_contacts(self, term: str) -> list[dict]:
        return self.get("Contacts", {"searchTerm": term}).get("Contacts") or []

    def create_contact(self, name: str) -> dict:
        return self.put("Contacts", {"Contacts": [{"Name": name}]})["Contacts"][0]

    def bills(self, contact_id: str, invoice_number: str | None, page: int | None = None) -> list[dict]:
        params = {"ContactIDs": contact_id, "Statuses": "DRAFT,SUBMITTED,AUTHORISED,PAID"}
        if invoice_number is not None:
            params["InvoiceNumbers"] = invoice_number
        if page is not None:
            params.update(page=page, pageSize=1000)
        return self.get("Invoices", params).get("Invoices") or []

    def create_invoice(self, invoice: dict) -> dict:
        reply = self.put("Invoices", {"Invoices": [invoice]})
        return reply["Invoices"][0]

    def read_invoice(self, invoice_id: str) -> dict:
        return self.get(f"Invoices/{invoice_id}")["Invoices"][0]

    def add_history_note(self, invoice_id: str, text: str) -> None:
        self.put(f"Invoices/{invoice_id}/History", {"HistoryRecords": [{"Details": text[:2500]}]})

    def delete_draft(self, invoice_id: str) -> dict:
        return self.post(f"Invoices/{invoice_id}", {"InvoiceID": invoice_id, "Status": "DELETED"})["Invoices"][0]
