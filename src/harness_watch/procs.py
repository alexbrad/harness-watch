"""Process table snapshot via `ps`, plus helpers to walk the parent chain."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    tty: str | None  # "ttys001", or None when the process has no terminal
    elapsed_s: int
    comm: str


def parse_etime(value: str) -> int:
    """Parse ps etime `[[dd-]hh:]mm:ss` into seconds."""
    days = 0
    if "-" in value:
        d, value = value.split("-", 1)
        days = int(d)
    parts = [int(p) for p in value.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return ((days * 24 + h) * 60 + m) * 60 + s


def parse_ps(output: str) -> dict[int, Proc]:
    procs: dict[int, Proc] = {}
    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        pid, ppid, tty, etime, comm = fields
        try:
            procs[int(pid)] = Proc(
                pid=int(pid),
                ppid=int(ppid),
                tty=None if tty in ("??", "-") else tty,
                elapsed_s=parse_etime(etime),
                comm=comm,
            )
        except ValueError:
            continue
    return procs


def snapshot() -> dict[int, Proc]:
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,tty=,etime=,comm="],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return parse_ps(out)


def host_app(pid: int, procs: dict[int, Proc]) -> str | None:
    """Name of the macOS app the process runs under (e.g. "iTerm", "Terminal"), if any."""
    seen = set()
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        comm = procs[pid].comm
        if ".app/Contents/MacOS/" in comm:
            return comm.split(".app/Contents/MacOS/")[0].rsplit("/", 1)[-1]
        pid = procs[pid].ppid
    return None


def children(pid: int, procs: dict[int, Proc]) -> list[Proc]:
    return [p for p in procs.values() if p.ppid == pid]


def args_of(pids: list[int]) -> dict[int, str]:
    """Full command lines of a few processes (`ps -o comm` stops at the executable)."""
    if not pids:
        return {}
    out = subprocess.run(
        ["ps", "-ww", "-o", "pid=,args=", "-p", ",".join(map(str, pids))],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,  # exits 1 when one of them has just exited
    ).stdout
    found = {}
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():  # ps shows newlines and tabs inside arguments as octal escapes
            found[int(pid)] = args.strip().replace("\\012", "\n").replace("\\011", "\t")
    return found


def tool_command(args: str) -> str | None:
    """The command a Claude Code Bash tool shell runs: it sources a shell snapshot, then runs
    `eval '<command>'`, the command quoted as one shell word. None for any other process."""
    start = args.find(" eval '")
    if "/shell-snapshots/" not in args or start < 0:
        return None
    lexer = shlex.shlex(args[start + len(" eval "):], posix=True)
    lexer.whitespace_split = True
    try:
        return lexer.get_token()
    except ValueError:  # unbalanced quotes: ps cut the line short
        return None


def config_dirs(pids: list[int]) -> dict[int, str | None]:
    """Each process's CLAUDE_CONFIG_DIR, from its environment (`ps -E` appends it to the
    command line; readable for our own processes). None: not set, the default ~/.claude."""
    if not pids:
        return {}
    out = subprocess.run(
        ["ps", "-wwE", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    found = {}
    for line in out.splitlines():
        pid, _, rest = line.strip().partition(" ")
        if pid.isdigit():
            m = re.search(r"(?:^| )CLAUDE_CONFIG_DIR=(\S+)", rest)
            found[int(pid)] = m[1] if m else None
    return found
