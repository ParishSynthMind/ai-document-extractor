"""Where the Xero credentials live.

Xero rotates refresh tokens: every refresh returns a new refresh token, and the
old one only keeps working for a 30 minute grace period. So the new token is
saved the moment it arrives, before any other call is made.

A store holds four values: client_id, client_secret, tenant_id (which Xero
organisation to post to) and refresh_token. refresh_token and tenant_id are
written back (the second only by `xero-push --connect`). Token values are
never printed or put in an error message.

- KeychainTokenStore: macOS Keychain generic-password items xero-client-id,
  xero-client-secret, xero-tenant-id, xero-refresh-token.
- FileTokenStore: XERO_* environment variables, with written values kept in a
  0600 JSON file (for Linux servers and containers).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from ..quickbooks.tokens import TokenStoreError

KEYS = ("client_id", "client_secret", "tenant_id", "refresh_token")
WRITABLE = ("refresh_token", "tenant_id")
ENV_NAMES = {k: f"XERO_{k.upper()}" for k in KEYS}
SAFE_CHARS = re.compile(r"^[A-Za-z0-9._\-]+$")

__all__ = ["FileTokenStore", "KeychainTokenStore", "MemoryTokenStore", "TokenStore", "TokenStoreError"]


class TokenStore(Protocol):
    def get(self, key: str) -> str: ...

    def save(self, key: str, value: str) -> None: ...


def _check(key: str, value: str) -> None:
    if key not in WRITABLE:
        raise TokenStoreError(f"{key} is never written by this tool")
    # Xero tokens and tenant ids are letters, digits, dots, dashes and underscores.
    # Anything else is refused rather than escaped, so a value can never break out
    # of the Keychain command.
    if not value or not SAFE_CHARS.match(value):
        raise TokenStoreError(f"refusing to store a {key} with unexpected characters")


class KeychainTokenStore:
    def __init__(self, account: str = "neo", prefix: str = "xero-", runner=subprocess.run):
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

    def save(self, key: str, value: str) -> None:
        _check(key, value)
        # `security -i` reads the command from stdin, so the value never appears in
        # the process list the way it would as a command-line argument.
        cmd = f'add-generic-password -U -a "{self.account}" -s "{self.item(key)}" -w "{value}"\n'
        proc = self.run(["security", "-i"], input=cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise TokenStoreError(f"could not write {self.item(key)} to the Keychain")
        if self.get(key) != value:
            raise TokenStoreError(f"the Keychain still holds the old {key} after saving")


class FileTokenStore:
    """Values from XERO_CLIENT_ID, XERO_CLIENT_SECRET, XERO_TENANT_ID, XERO_REFRESH_TOKEN.

    An environment variable can't be rewritten from inside the process, so written
    values go to a JSON file, and once there they win over the environment's.
    """

    def __init__(self, path: str | Path = ".xero_tokens.json", environ=None):
        self.path = Path(path)
        self.environ = os.environ if environ is None else environ

    def _file(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key: str) -> str:
        value = self._file().get(key) or self.environ.get(ENV_NAMES[key], "")
        if not value:
            raise TokenStoreError(f"{ENV_NAMES[key]} is not set and {self.path} has no {key}")
        return value

    def save(self, key: str, value: str) -> None:
        _check(key, value)
        data = self._file()
        data[key] = value
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)  # atomic: a crash mid-write can't leave half a token


class MemoryTokenStore:
    """For tests and one-off scripts."""

    def __init__(self, **values):
        self.values = dict(values)
        self.saved: list[tuple[str, str]] = []

    def get(self, key: str) -> str:
        if not self.values.get(key):
            raise TokenStoreError(f"{key} not set")
        return self.values[key]

    def save(self, key: str, value: str) -> None:
        _check(key, value)
        self.saved.append((key, value))
        self.values[key] = value
