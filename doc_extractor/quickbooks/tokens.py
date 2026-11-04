"""Where the QuickBooks credentials live.

Intuit rotates refresh tokens: a refresh can hand back a new refresh token, and
from then on only the new one is good. So the new token is saved the moment it
arrives, before any other call is made. Lose it and the next run is locked out
until someone re-authorises the app by hand.

A store holds four values: client_id, client_secret, realm_id and
refresh_token. Only refresh_token is ever written back. Token values are never
printed or put in an error message.

- KeychainTokenStore: macOS Keychain generic-password items (used for the live run).
- FileTokenStore: environment variables, with the rotating refresh token kept in a
  0600 JSON file (for Linux servers, containers, CI-style setups).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

KEYS = ("client_id", "client_secret", "realm_id", "refresh_token")
ENV_NAMES = {k: f"QBO_{k.upper()}" for k in KEYS}
TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._\-]+$")


class TokenStoreError(RuntimeError):
    pass


class TokenStore(Protocol):
    def get(self, key: str) -> str: ...

    def save_refresh_token(self, token: str) -> None: ...


def _check_token(token: str) -> None:
    # Intuit tokens are letters, digits, dots and dashes. Anything else is refused
    # rather than escaped, so a token can never break out of the Keychain command.
    if not token or not TOKEN_CHARS.match(token):
        raise TokenStoreError("refusing to store a refresh token with unexpected characters")


class KeychainTokenStore:
    """Generic-password items named <prefix>client-id, <prefix>refresh-token, and so on."""

    def __init__(self, account: str = "qbo", prefix: str = "intuit-", runner=subprocess.run):
        self.account = account
        self.prefix = prefix
        self.run = runner

    def item(self, key: str) -> str:
        return self.prefix + key.replace("_", "-")

    def get(self, key: str) -> str:
        proc = self.run(["security", "find-generic-password", "-a", self.account, "-s", self.item(key), "-w"],
                        capture_output=True, text=True)
        value = (proc.stdout or "").strip()
        if proc.returncode != 0 or not value:
            raise TokenStoreError(f"Keychain item {self.item(key)!r} (account {self.account!r}) not found")
        return value

    def save_refresh_token(self, token: str) -> None:
        _check_token(token)
        # `security -i` reads the command from stdin, so the token never appears in
        # the process list the way it would as a command-line argument.
        cmd = f'add-generic-password -U -a "{self.account}" -s "{self.item("refresh_token")}" -w "{token}"\n'
        proc = self.run(["security", "-i"], input=cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise TokenStoreError("could not write the new refresh token to the Keychain")
        if self.get("refresh_token") != token:
            raise TokenStoreError("the Keychain still holds the old refresh token after saving")


class FileTokenStore:
    """Values from QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_REALM_ID and QBO_REFRESH_TOKEN.

    The refresh token rotates, and an environment variable can't be rewritten
    from inside the process, so rotated tokens go to a JSON file. Once the file
    exists, its refresh token wins over the environment's (it is the newer one).
    """

    def __init__(self, path: str | Path = ".qbo_tokens.json", environ=None):
        self.path = Path(path)
        self.environ = os.environ if environ is None else environ

    def _file(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {}

    def get(self, key: str) -> str:
        value = self._file().get(key) or self.environ.get(ENV_NAMES[key], "")
        if not value:
            raise TokenStoreError(f"{ENV_NAMES[key]} is not set and {self.path} has no {key}")
        return value

    def save_refresh_token(self, token: str) -> None:
        _check_token(token)
        data = self._file()
        data["refresh_token"] = token
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)  # atomic: a crash mid-write can't leave half a token
