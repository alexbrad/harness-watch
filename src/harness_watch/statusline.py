"""Plan usage from Claude Code's status line, the documented source of the 5-hour and weekly limits.

Claude Code pipes a JSON to the status line command on every update; on Pro, Max and Ultra plans it
holds `rate_limits` once a session has had its first response. `harness-watch-statusline`, set
as that command, merges them into one file per account, which the dashboard reads (usage.py). Then it
runs the status line you had before, given as its argument, so that one still shows.
Standard library only, and only paths.py of the package: it runs on every status line update.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .paths import CACHE

WINDOWS = ("five_hour", "seven_day")


def data_dir() -> Path:
    env = os.environ.get("HARNESS_WATCH_RATE_LIMITS")
    return Path(env) if env else CACHE / "rate-limits"


def account_name(config_dir: str | None) -> str:
    """The account a session logs in with, named after its config dir: the Keychain login is per dir."""
    name = Path(config_dir).expanduser().name.lstrip(".") if config_dir else "claude"
    return name or "claude"


def _order(window) -> tuple:
    """A window's (reset time, share used), 0 for what's missing: which of two readings is newer."""
    w = window if isinstance(window, dict) else {}
    return tuple(v if isinstance(v := w.get(k), (int, float)) else 0 for k in ("resets_at", "used_percentage"))


def merge(old: dict, new: dict) -> dict:
    """Each window from the newer reading. Sessions on one account report snapshots of different
    ages: a later reset is a newer window, and within one window usage only grows."""
    out = {**old, **new}
    for k in WINDOWS:
        if isinstance(old.get(k), dict) and _order(old[k]) > _order(new.get(k)):
            out[k] = old[k]
    return out


def record(data: dict, config_dir: str | None, out: Path, now: float) -> bool:
    """Merge the update's rate limits into <out>/<account>.json; False when it carries none."""
    limits = data.get("rate_limits")
    if not isinstance(limits, dict) or not any(isinstance(limits.get(k), dict) for k in WINDOWS):
        return False
    name = account_name(config_dir)
    mask = os.umask(0o077)  # yours only; restored for the status line it runs after
    try:
        _write(name, config_dir, limits, out, now)
    finally:
        os.umask(mask)
    return True


def _write(name: str, config_dir: str | None, limits: dict, out: Path, now: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.json"
    with open(out / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # sessions on one account update at once
        try:
            old = json.loads(path.read_text()).get("rate_limits")
        except (OSError, ValueError, AttributeError):
            old = None
        merged = merge(old, limits) if isinstance(old, dict) else limits
        tmp = out / f".{name}.{os.getpid()}.tmp"  # then renamed: the dashboard never reads half a file
        tmp.write_text(json.dumps({"account": name, "config_dir": config_dir, "rate_limits": merged, "at": now}))
        tmp.replace(path)


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            record(data, os.environ.get("CLAUDE_CONFIG_DIR"), data_dir(), time.time())
    except (ValueError, OSError):
        pass  # a status line must never fail over the dashboard's file
    if len(sys.argv) > 1:  # your own status line: one shell command string, or a program and its args
        cmd = sys.argv[1] if len(sys.argv) == 2 else sys.argv[1:]
        sys.exit(subprocess.run(cmd, input=raw, shell=len(sys.argv) == 2).returncode)


if __name__ == "__main__":
    main()
