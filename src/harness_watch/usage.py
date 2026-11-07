"""Plan usage per Claude account: the 5-hour and weekly limits, as bars under the header.

Read from the files the status line command writes (statusline.py). HARNESS_WATCH_USAGE_SOURCE
may name a Python file instead whose `source()` returns another feed: a callable that returns the
readings below. A reading: {account, email, plan, limits: [{label, percent, severity, resets_at}],
updated_at, error}; `resets_at` and `updated_at` are ISO times.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
import types
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import statusline

LABELS = {"five_hour": "Current session", "seven_day": "Weekly · all models"}
WARNING_PCT, CRITICAL_PCT = 75, 100
EMAIL_TTL_S = 60.0  # the feed is read every second; Claude Code's global config can be large
log = logging.getLogger(__name__)


def email(config_dir: Path | None, home: Path | None = None) -> str | None:
    """The account's address, from Claude Code's global config for that dir."""
    path = (config_dir or home or Path.home()) / ".claude.json"
    try:
        return (json.loads(path.read_text()).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, ValueError, AttributeError):
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def severity(percent: float) -> str:
    return "critical" if percent >= CRITICAL_PCT else "warning" if percent >= WARNING_PCT else "normal"


def rows(rate_limits: dict, now: float) -> list[dict]:
    """The status line's windows as rows. A window whose reset time has passed since starts again
    at 0%, its next reset unknown until a session reports it."""
    out = []
    for key, label in LABELS.items():
        w = rate_limits.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
            continue
        percent, resets = w["used_percentage"], w.get("resets_at")
        resets = resets if isinstance(resets, (int, float)) else None
        if resets is not None and resets <= now:
            percent, resets = 0, None
        out.append({"label": label, "percent": percent, "severity": severity(percent),
                    "resets_at": _iso(resets) if resets is not None else None})
    return out


class StatuslineFeed:
    """The readings in the status line's files: one per account that has had a session since."""

    def __init__(self, directory: Path | None = None, email_fn=email, clock=time.time):
        self.directory = directory or statusline.data_dir()
        self.email_fn, self.clock = email_fn, clock
        self._emails: dict[str | None, tuple[str | None, float]] = {}

    def _email(self, config_dir: str | None, now: float) -> str | None:
        cached = self._emails.get(config_dir)
        if not cached or cached[1] <= now:
            cached = (self.email_fn(Path(config_dir).expanduser() if config_dir else None), now + EMAIL_TTL_S)
            self._emails[config_dir] = cached
        return cached[0]

    def current(self) -> list[dict]:
        now = self.clock()
        out = []
        for path in sorted(self.directory.glob("*.json"), key=lambda p: p.stem):  # "claude" first
            try:  # a file of any other shape is skipped: it must not break the page's snapshot
                data = json.loads(path.read_text())
                limits, updated = rows(data["rate_limits"], now), _iso(float(data["at"]))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
                continue
            config_dir = data.get("config_dir") if isinstance(data.get("config_dir"), str) else None
            out.append({"account": str(data.get("account") or path.stem), "email": self._email(config_dir, now),
                        "plan": None, "limits": limits, "updated_at": updated, "error": None})
        return out


def check_private(path: Path) -> None:
    """Refuse a source file another user could change, or swap by renaming a folder above it: it
    runs as you. Every folder up to / must be yours or root's, and writable by others only when
    sticky (/tmp: there nobody may rename what isn't theirs), except the file's own folder.
    Not checked: ACLs that grant others write access (macOS keeps them out of st_mode)."""
    for p in (path, *path.parents):
        st = p.stat()
        sticky = p not in (path, path.parent) and st.st_mode & stat.S_ISVTX
        if st.st_uid not in (os.getuid(), 0) or (st.st_mode & 0o022 and not sticky):
            raise PermissionError(f"{p} must be yours (or root's) and writable by you only")


def load_source() -> Callable[[], list[dict]]:
    """The feed: the status line's, or the one HARNESS_WATCH_USAGE_SOURCE names."""
    path = os.environ.get("HARNESS_WATCH_USAGE_SOURCE")
    if path:
        try:
            real = Path(path).resolve()  # a symlink's own folder says nothing about its target's
            check_private(real)
            module = sys.modules["usage_source"] = types.ModuleType("usage_source")  # as an import does
            module.__file__ = str(real)
            # Compiled here, never from a __pycache__ beside it, which others might control.
            exec(compile(real.read_bytes(), str(real), "exec", dont_inherit=True), module.__dict__)  # its own __future__ flags
            return module.source()
        except Exception:
            sys.modules.pop("usage_source", None)
            log.exception("usage source %s failed to load; using the status line's", path)
    return StatuslineFeed().current
