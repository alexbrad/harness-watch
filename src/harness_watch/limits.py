"""Usage limits. A hit is per Claude account: every session logged in with it is blocked until
the reset, also those that haven't sent a request since.

Claude Code records a hit in the transcript as a synthetic assistant message, e.g.
"You've hit your session limit · resets 1:40pm (Europe/Bucharest)" (also "weekly limit",
"5-hour limit", "resets Oct 6 at 10pm").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_RESETS = re.compile(
    r"resets (?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2}) at )?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\s*(?:\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def kind(text: str) -> str:
    """"session", "weekly", "5-hour", …: the words before "limit"."""
    m = re.search(r"hit your (.+?) limit", text or "")
    return m[1] if m else "usage"


def resets_at(text: str, at: str | None) -> str | None:
    """The reset time in the message, as an ISO timestamp: the first such clock time after the
    message was written (or that date, when one is given). None when it can't be read."""
    m = _RESETS.search(text or "")
    if not m or not at:
        return None
    try:
        tz = ZoneInfo(m["tz"].strip()) if m["tz"] else None
        written = datetime.fromisoformat(at).astimezone(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m["hour"]) % 12 + (12 if m["ampm"].lower() == "pm" else 0)
    reset = written.replace(hour=hour, minute=int(m["minute"] or 0), second=0, microsecond=0)
    if m["month"]:
        reset = reset.replace(month=_MONTHS.index(m["month"].lower()[:3]) + 1, day=int(m["day"]))
        if reset < written:
            reset = reset.replace(year=reset.year + 1)
    elif reset <= written:
        reset += timedelta(days=1)
    return reset.isoformat()


def active(hits: list[dict], last_responses: list[str], now: datetime) -> dict | None:
    """The account's limit in force now: its latest hit, unless the reset time has passed or a
    session got a real response after it."""
    hits = [h for h in hits if h and h.get("at")]
    if not hits:
        return None
    hit = max(hits, key=lambda h: datetime.fromisoformat(h["at"]))
    at = datetime.fromisoformat(hit["at"])
    if hit.get("resets") and datetime.fromisoformat(hit["resets"]) <= now:
        return None
    if any(r and datetime.fromisoformat(r) > at for r in last_responses):
        return None
    return {**hit, "kind": kind(hit["text"])}
