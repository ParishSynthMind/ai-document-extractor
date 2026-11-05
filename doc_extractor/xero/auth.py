"""The one-time connect step: a person logs in to Xero and picks the organisation.

    xero-push --connect

1. Builds Xero's authorise URL (standard auth code flow) with a random `state`
   and opens it in the browser.
2. Waits on http://localhost:<port>/callback for Xero to send the person back
   with a one-time code (it expires after 5 minutes). A wrong or missing
   `state` is refused, so a forged redirect can't connect someone else's org.
3. Swaps the code for tokens (the refresh token is saved straight away).
4. Calls /connections and keeps the organisation connected in THIS login
   (matched on the access token's authentication_event_id), then saves its
   tenant id. If several were connected, --tenant-name picks one.

After this, every run refreshes on its own for up to 60 days of not being used.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from .client import XeroError

AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
# Least privilege for posting bills. Xero assigns granular scopes to apps created since
# March 2026 (accounting.invoices replaces the deprecated accounting.transactions).
SCOPES = ("offline_access", "accounting.invoices", "accounting.contacts", "accounting.settings.read")
DEFAULT_REDIRECT = "http://localhost:8723/callback"


def authorize_url(client_id: str, redirect_uri: str, state: str, scopes=SCOPES) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
             "scope": " ".join(scopes), "state": state}
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def jwt_claims(token: str) -> dict:
    """Read (not verify) the claims of Xero's access token. It came straight from Xero's
    token endpoint over TLS, and only authentication_event_id is used, to pick the tenant."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def pick_tenant(connections: list[dict], auth_event_id: str | None, tenant_name: str | None) -> dict:
    orgs = [c for c in connections if c.get("tenantType") == "ORGANISATION"]
    if auth_event_id and any(c.get("authEventId") == auth_event_id for c in orgs):
        orgs = [c for c in orgs if c.get("authEventId") == auth_event_id]
    if tenant_name:
        named = [c for c in orgs if (c.get("tenantName") or "").casefold() == tenant_name.casefold()]
        if not named:
            raise XeroError(f"no connected organisation is called {tenant_name!r}; connected: "
                            + ", ".join(repr(c.get("tenantName")) for c in orgs))
        return named[0]
    if not orgs:
        raise XeroError("the login connected no Xero organisation (tick one on Xero's consent screen)")
    if len(orgs) > 1:
        raise XeroError("several organisations were connected; run again with --tenant-name, one of: "
                        + ", ".join(repr(c.get("tenantName")) for c in orgs))
    return orgs[0]


class _Catcher(BaseHTTPRequestHandler):
    result: dict = {}
    expected_state = ""
    path_expected = "/callback"

    def do_GET(self):  # noqa: N802 (http.server's naming)
        url = urlparse(self.path)
        if url.path != self.path_expected:
            self.send_response(404)
            self.end_headers()
            return
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if q.get("state") != self.expected_state:
            type(self).result = {"error": "state did not match; ignoring this redirect"}
        elif "error" in q:
            type(self).result = {"error": f"Xero sent back an error: {q['error']}"}
        else:
            type(self).result = {"code": q.get("code", "")}
        ok = "code" in type(self).result
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Connected. You can close this tab." if ok else "Not connected. Check the terminal."
        self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *args):  # keep the code out of any log
        pass


def wait_for_code(redirect_uri: str, state: str, timeout: float = 300, ready=None) -> str:
    url = urlparse(redirect_uri)
    if url.hostname != "localhost":
        raise XeroError("the connect step listens on http://localhost only (Xero does not allow 127.0.0.1)")
    handler = type("Catcher", (_Catcher,), {"result": {}, "expected_state": state, "path_expected": url.path})
    server = HTTPServer(("localhost", url.port or 80), handler)
    server.timeout = timeout
    try:
        if ready:
            ready.set()
        server.handle_request()  # one request, or give up after `timeout`
    finally:
        server.server_close()
    if not handler.result:
        raise XeroError(f"no reply from Xero within {timeout:.0f}s; run --connect again")
    if "error" in handler.result:
        raise XeroError(handler.result["error"])
    return handler.result["code"]


def connect(client, store, redirect_uri: str = DEFAULT_REDIRECT, tenant_name: str | None = None,
            open_browser=None, timeout: float = 300, printer=print) -> dict:
    """Run the whole connect flow. Returns the chosen connection (no secrets in it)."""
    state = secrets.token_urlsafe(24)
    url = authorize_url(store.get("client_id"), redirect_uri, state)
    printer("Opening Xero's login page. If it doesn't open, paste this into the browser:\n" + url)
    ready = threading.Event()
    holder: dict = {}

    def serve():
        try:
            holder["code"] = wait_for_code(redirect_uri, state, timeout, ready)
        except Exception as exc:  # surfaced below
            holder["error"] = exc
            ready.set()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(5)
    if open_browser:
        open_browser(url)
    t.join(timeout + 5)
    if "error" in holder:
        raise holder["error"]
    if "code" not in holder:
        raise XeroError("the connect step timed out")
    tokens = client.exchange_code(holder["code"], redirect_uri)
    chosen = pick_tenant(client.connections(), jwt_claims(tokens["access_token"]).get("authentication_event_id"),
                         tenant_name)
    store.save("tenant_id", chosen["tenantId"])
    client._tenant = chosen["tenantId"]
    return {"tenantName": chosen.get("tenantName"), "tenantId": chosen["tenantId"],
            "scope": tokens.get("scope")}
