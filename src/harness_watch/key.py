"""The page's key: only who can read it can use the page, so other accounts on the Mac can't.

The key file is readable by you only. `harness-watch open` first checks that the server answering
on the port knows the key (another account could hold the port while harness-watch is down), then
opens a local file, readable by you only, that posts the key to the server: never in a command
line (`ps` shows those to every account) or an address (the browser's history keeps those). The
server answers with a page that stores the key in the browser's storage for that origin (scheme,
host and port: unlike a cookie, no other port on 127.0.0.1 ever sees it) and the page sends it with
every request.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from pathlib import Path

from .paths import DATA

PATH = DATA / "key"
HEADER = "x-harness-watch-key"
PARAM = "key"  # for the event stream: EventSource can't send headers
STORAGE = "harness-watch-key"  # the browser's localStorage item


def load(path: Path | None = None) -> str:
    """The key; made on first use. Never empty: an empty key would let everyone in."""
    path = path or PATH
    for _ in range(50):
        try:
            key = path.read_text().strip()
        except FileNotFoundError:
            key = _create(path)
        if key:
            return key
        time.sleep(0.05)  # an older version's half-written file; _create never leaves one
    raise RuntimeError(f"{path} is empty: delete it and start harness-watch again")


def _create(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_urlsafe(32)
    tmp = path.with_name(f".key.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    try:
        os.link(tmp, path)  # appears complete, or not at all; fails if another process made one first
    except FileExistsError:
        key = path.read_text().strip()
    finally:
        tmp.unlink()
    return key


def matches(given: str | None, key: str) -> bool:
    return given is not None and hmac.compare_digest(given.encode(), key.encode())


def proof(key: str, nonce: str) -> str:
    """What only a server holding the key can answer to `nonce`."""
    return hmac.new(key.encode(), nonce.encode(), hashlib.sha256).hexdigest()


def opener(url: str, key: str) -> str:
    """The local file `harness-watch open` opens: it posts the key to `url`."""
    return (f'<!doctype html><meta charset="utf-8"><title>harness-watch</title>'
            f'<body onload="document.forms[0].submit()"><form method="post" action="{html.escape(url)}">'
            f'<input type="hidden" name="{PARAM}" value="{html.escape(key)}"></form>')


def keeper(key: str) -> tuple[str, str]:
    """The server's answer to a right key: (page, its Content-Security-Policy). The page stores the
    key for this origin, then goes to the dashboard. Its one inline script runs by nonce."""
    nonce = secrets.token_urlsafe(16)
    page = (f'<!doctype html><meta charset="utf-8"><title>harness-watch</title><script nonce="{nonce}">'
            f'try {{ localStorage.setItem({STORAGE!r}, {key!r}); }} catch (e) {{}}'
            f'location.replace("/");</script>')
    return page, f"default-src 'none'; script-src 'nonce-{nonce}'; frame-ancestors 'none'; base-uri 'none'"
