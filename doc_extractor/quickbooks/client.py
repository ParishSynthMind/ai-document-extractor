"""A small QuickBooks Online Accounting API (v3) client.

Only what the bill push needs: token refresh, query, create, read, delete.
Access tokens live in memory only. A 401 triggers one refresh and one retry;
a second 401 is an error, not a loop. A 400 is turned into a readable list of
QuickBooks' own validation messages.
"""
from __future__ import annotations

import requests

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
API_BASES = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
MINOR_VERSION = 75


class QBOError(RuntimeError):
    pass


class ReauthorisationNeeded(QBOError):
    """The refresh token itself was refused. Only a person can fix this, once."""


class QBOAuthError(QBOError):
    pass


class QBOValidationError(QBOError):
    def __init__(self, status: int, errors: list[dict], tid: str | None = None):
        self.status = status
        self.errors = errors
        lines = [f"QuickBooks rejected the request (HTTP {status}):"]
        for e in errors:
            code = f" [code {e['code']}]" if e.get("code") else ""
            detail = f": {e['detail']}" if e.get("detail") and e.get("detail") != e.get("message") else ""
            lines.append(f"  - {e.get('message') or 'validation error'}{detail}{code}")
        if tid:
            lines.append(f"  (intuit_tid {tid}, quote this to Intuit support)")
        super().__init__("\n".join(lines))


def quote(value: str) -> str:
    """Escape a value for a QuickBooks query string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _faults(resp) -> list[dict]:
    try:
        body = resp.json()
    except ValueError:
        return [{"message": resp.text[:300]}]
    fault = body.get("Fault") or body.get("fault") or {}
    errors = fault.get("Error") or fault.get("error") or []
    return [{"message": e.get("Message") or e.get("message"), "detail": e.get("Detail") or e.get("detail"),
             "code": e.get("code")} for e in errors] or [{"message": str(body)[:300]}]


class QBOClient:
    def __init__(self, store, environment: str = "sandbox", session=None, timeout: float = 30):
        if environment not in API_BASES:
            raise ValueError(f"environment must be one of {sorted(API_BASES)}")
        self.store = store
        self.environment = environment
        self.session = session or requests.Session()
        self.timeout = timeout
        self._access_token: str | None = None
        self.refreshes = 0
        self._realm: str | None = None

    @property
    def realm_id(self) -> str:
        if self._realm is None:
            self._realm = self.store.get("realm_id")
        return self._realm

    def refresh(self) -> None:
        """Swap the refresh token for a new access token, and save the new refresh token first."""
        resp = self.session.post(
            TOKEN_URL,
            auth=(self.store.get("client_id"), self.store.get("client_secret")),
            data={"grant_type": "refresh_token", "refresh_token": self.store.get("refresh_token")},
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
                    "Intuit refused the stored refresh token (invalid_grant): it has expired, been revoked or "
                    "was already used. Re-authorise the app once and store the new refresh token.")
            raise QBOError(f"token refresh failed: HTTP {resp.status_code} {err}".strip())
        data = resp.json()
        new_refresh = data.get("refresh_token")
        if not new_refresh or not data.get("access_token"):
            raise QBOError("token refresh reply had no tokens in it")
        self.store.save_refresh_token(new_refresh)  # before anything else can fail
        self._access_token = data["access_token"]
        self.refreshes += 1

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        if self._access_token is None:
            self.refresh()
        url = f"{API_BASES[self.environment]}/v3/company/{self.realm_id}/{path}"
        params = {"minorversion": MINOR_VERSION, **(params or {})}
        for attempt in (1, 2):
            resp = self.session.request(
                method, url, params=params, json=body, timeout=self.timeout,
                headers={"Authorization": f"Bearer {self._access_token}", "Accept": "application/json",
                         "Content-Type": "application/json"},
            )
            if resp.status_code == 401 and attempt == 1:
                self.refresh()  # access tokens last an hour; get a fresh one and try once more
                continue
            break
        tid = resp.headers.get("intuit_tid")
        if resp.status_code == 401:
            raise QBOAuthError("QuickBooks returned 401 even with a fresh access token: check the app's "
                               "accounting scope and that the realm id belongs to this connection")
        if resp.status_code >= 400:
            errors = _faults(resp)
            if resp.status_code == 400:
                raise QBOValidationError(400, errors, tid)
            raise QBOError(f"QuickBooks HTTP {resp.status_code}: "
                           + "; ".join(str(e.get("message")) for e in errors))
        return resp.json()

    def query(self, sql: str) -> list[dict]:
        data = self.request("GET", "query", params={"query": sql})
        qr = data.get("QueryResponse", {})
        for value in qr.values():
            if isinstance(value, list):
                return value
        return []

    def create(self, entity: str, body: dict) -> dict:
        return self.request("POST", entity.lower(), body=body)[entity]

    def read(self, entity: str, entity_id: str) -> dict:
        return self.request("GET", f"{entity.lower()}/{entity_id}")[entity]

    def update(self, entity: str, body: dict) -> dict:
        return self.request("POST", entity.lower(), body=body)[entity]

    def delete(self, entity: str, entity_id: str, sync_token: str) -> dict:
        return self.request("POST", entity.lower(), params={"operation": "delete"},
                            body={"Id": entity_id, "SyncToken": sync_token})

    def preferences(self) -> dict:
        rows = self.query("select * from Preferences")
        return rows[0] if rows else {}
