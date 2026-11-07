"""iTerm2 integration via AppleScript."""

from __future__ import annotations

import json
import logging
import os
import pwd
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import CACHE

log = logging.getLogger(__name__)

_LIST_SCRIPT = """
set sep to character id 9
tell application "iTerm2"
  set out to ""
  repeat with w in windows
    set tabIndex to 0
    repeat with t in tabs of w
      set tabIndex to tabIndex + 1
      repeat with s in sessions of t
        set out to out & (id of w) & sep & tabIndex & sep & (unique id of s) & sep & (tty of s) & sep & (name of s) & linefeed
      end repeat
    end repeat
  end repeat
  return out
end tell
"""


@dataclass(frozen=True)
class ItermSession:
    window_id: int
    tab_index: int
    session_id: str
    tty: str  # "ttys001"
    name: str


APP_BINARY = "iTerm.app/Contents/MacOS/iTerm2"


def is_running(comms) -> bool:
    """Whether iTerm2 appears among the given process command paths."""
    return any(c.endswith(APP_BINARY) for c in comms)


def parse_sessions(output: str) -> dict[str, ItermSession]:
    by_tty: dict[str, ItermSession] = {}
    for line in output.splitlines():
        fields = line.split("\t", 4)
        if len(fields) < 5:
            continue
        window_id, tab_index, session_id, tty, name = fields
        tty = tty.removeprefix("/dev/")
        try:
            by_tty[tty] = ItermSession(int(window_id), int(tab_index), session_id, tty, name)
        except ValueError:
            continue
    return by_tty


OSASCRIPT_TIMEOUT_S = 10.0


class ItermTimeout(Exception):
    pass


def osascript(script: str, *args: str, bounded: bool = True) -> subprocess.CompletedProcess | None:
    """Run AppleScript with `args` as its `on run argv`; None when it hangs. It does while macOS
    asks whether this app may control iTerm2, and the session list would wait with it."""
    try:
        return subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True,
                              timeout=OSASCRIPT_TIMEOUT_S if bounded else None)
    except subprocess.TimeoutExpired:
        log.warning("iTerm2 didn't answer in %.0f s: is macOS asking whether harness-watch may control it?",
                    OSASCRIPT_TIMEOUT_S)
        return None


def list_sessions(running: bool) -> dict[str, ItermSession]:
    """iTerm2 sessions keyed by tty; empty when iTerm2 is not running or scripting fails."""
    # `tell application "iTerm2"` would launch iTerm2, so callers check first.
    if not running:
        return {}
    result = osascript(_LIST_SCRIPT)
    if result is None:
        raise ItermTimeout
    if result.returncode != 0:
        return {}
    return parse_sessions(result.stdout)


class ItermCache:
    """Keeps the tty -> iTerm2 session map between polls.

    The AppleScript lookup takes ~0.6 s, so it re-runs only when a session appears that
    the last lookup didn't cover (new pid or tty), or after `max_age_s` to pick up
    renamed tabs and moved panes.
    """

    def __init__(self, fetch=list_sessions, max_age_s: float = 15.0, clock=time.monotonic, backoff_s: float = 60.0):
        self.backoff_s = backoff_s
        self._fetch = fetch
        self._clock = clock
        self.max_age_s = max_age_s
        self._by_tty: dict[str, ItermSession] = {}
        self._covered: set[tuple[int, str]] = set()
        self._at: float | None = None
        self._quiet_until = 0.0

    def get(self, running: bool, sessions: set[tuple[int, str]]) -> dict[str, ItermSession]:
        """`sessions` is the (pid, tty) pairs of the live Claude Code processes."""
        if not running:
            self._at = None
            self._by_tty = {}
            return {}
        now = self._clock()
        if now < self._quiet_until:
            return self._by_tty
        stale = self._at is None or now - self._at >= self.max_age_s
        if stale or not sessions <= self._covered:
            try:
                self._by_tty = self._fetch(True)
            except ItermTimeout:  # a permission dialog, likely: ask again in a while, not on every poll
                self._quiet_until = self._clock() + self.backoff_s
                return self._by_tty
            self._covered = set(sessions)
            self._at = self._clock()  # after the lookup, which can take seconds
        return self._by_tty


_FOCUS_SCRIPT = """
on run argv
  set target to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is target then
            try
              if miniaturized of w then set miniaturized of w to false
            end try
            select w
            tell t to select
            tell s to select
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""


_PANE_SCRIPT = """
on run argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is item 1 of argv then
            if (count of argv) is 1 then return background color of s
            set original to {(item 2 of argv) as integer, (item 3 of argv) as integer, (item 4 of argv) as integer}
            try
              set background color of s to {30000, 19000, 2000}
              delay 0.25
            end try
            set background color of s to original
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run
"""
# Without arguments beyond the id the script returns the pane's background color; with r g b it
# flashes the pane once and puts that color back, in one run, so the restore can't be skipped halfway.
# Reading and writing a color loses a unit (4273 -> 4272), so the color a pane had the first time
# is remembered and always restored: otherwise every flash would darken it a little.
_COLORS = CACHE / "pane-colors.json"


def _original_color(session_id: str) -> list[int] | None:
    try:
        colors = json.loads(_COLORS.read_text())
    except (OSError, ValueError):
        colors = {}
    if session_id not in colors:
        result = osascript(_PANE_SCRIPT, session_id)
        if result is None:
            return None
        out = result.stdout
        try:
            colors[session_id] = [int(v) for v in out.split(",")]
        except ValueError:
            return None
        if len(colors[session_id]) != 3:
            return None
        colors = dict(list(colors.items())[-500:])  # pane ids are per pane; old ones are dead
        _COLORS.parent.mkdir(parents=True, exist_ok=True)
        _COLORS.write_text(json.dumps(colors))
    return colors[session_id]


def flash(session_id: str) -> None:
    """Flash a pane's background once so the eye finds it (macOS has no "highlight this window")."""
    color = _original_color(session_id)
    if color:
        osascript(_PANE_SCRIPT, session_id, *map(str, color), bounded=False)  # never cut short: it restores the color last


def focus(session_id: str) -> bool:
    """Bring the iTerm2 window/tab/pane holding `session_id` to the front, then flash that pane
    so it is obvious which one it is (macOS has no "highlight this window" of its own)."""
    # The id goes in as an argument, never spliced into the script source.
    result = osascript(_FOCUS_SCRIPT, session_id)
    ok = result is not None and result.returncode == 0 and result.stdout.strip() == "ok"
    if ok:  # in the background: the page gets its answer without waiting for the pulses
        threading.Thread(target=flash, args=(session_id,), daemon=True).start()
    return ok


_FORK_SCRIPT = """
on run argv
  set tabCommand to item 1 of argv
  set windowId to item 2 of argv
  tell application "iTerm2"
    set w to missing value
    if windowId is not "" then
      repeat with x in windows
        if (id of x as text) is windowId then set w to x
      end repeat
    end if
    if w is missing value and (count of windows) > 0 then set w to window 1
    if w is missing value then
      create window with default profile command tabCommand
    else
      tell w to create tab with default profile command tabCommand
      select w
    end if
    activate
  end tell
  return "ok"
end run
"""
# The tab starts with `launch` as its process: nothing is typed, so it can't land in another
# tab that happens to have the keyboard focus.
FORKS = CACHE / "forks"


def is_fish(shell: str) -> bool:
    return Path(shell).name == "fish"


def shell_quote(text: str, fish: bool) -> str:
    """`text` as one shell word. In fish's single quotes backslash and quote are special; in a
    POSIX shell's nothing is, so a quote closes the word, is escaped, and reopens it."""
    if fish:
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return "'" + text.replace("'", "'\\''") + "'"


def fork_script(cwd: str, session_id: str, name: str | None, shell: str) -> str:
    """The script the new tab runs in your login shell, so your functions and aliases apply (a
    `claude` wrapper that picks the account by folder, say), then an ordinary shell in that folder
    once claude exits. fish syntax for fish, POSIX for the rest (zsh, bash)."""
    fish = is_fish(shell)
    named = f" --name {shell_quote(name, fish)}" if name else ""
    then = "; and" if fish else " &&"
    return (f"cd {shell_quote(cwd, fish)}{then} claude --resume {session_id} --fork-session{named}\n"
            f"exec {shell_quote(shell, fish)} -l\n")


FORK_SHELLS = {"fish", "zsh", "bash"}  # the script's syntax and flags; csh, tcsh and others get zsh


def login_shell() -> str:
    shell = pwd.getpwuid(os.getuid()).pw_shell
    return shell if Path(shell or "").name in FORK_SHELLS and os.access(shell, os.X_OK) else "/bin/zsh"


def fork(cwd: str, session_id: str, name: str | None, window_id: int | None = None) -> bool:
    """Open a new iTerm2 tab (in `window_id` when given, else the front window, else a new one)
    running the fork. The script goes in a file, one per session (rewritten on each fork), so the
    tab's command line is a plain path that iTerm2 can't misparse."""
    shell = login_shell()
    fish = is_fish(shell)
    FORKS.mkdir(parents=True, exist_ok=True)
    script = FORKS / f"{session_id}.{'fish' if fish else 'sh'}"
    script.write_text(fork_script(cwd, session_id, name, shell))
    flags = "-l" if fish else "-il"  # zsh and bash read your functions only when interactive; fish always
    launch = f"{shell} {flags} {script}" if " " not in f"{shell}{script}" else f'"{shell}" {flags} "{script}"'
    return open_tab(launch, window_id)


def open_tab(command: str, window_id: int | None = None) -> bool:
    """A new iTerm2 tab whose process is `command`."""
    # The command goes in as an argument, never spliced into the script source.
    result = osascript(_FORK_SCRIPT, command, "" if window_id is None else str(window_id))
    if result is not None and result.returncode != 0:
        log.warning("iTerm2 could not open a tab: %s", result.stderr.strip())
    return result is not None and result.returncode == 0 and result.stdout.strip() == "ok"
