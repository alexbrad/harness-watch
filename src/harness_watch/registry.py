"""Live Claude Code sessions, from `~/.claude/sessions/<pid>.json` checked against `ps`.

The registry format is internal to Claude Code and may change: every field is read
defensively, and a file that can't be parsed is skipped rather than failing the list.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import index, iterm, limits, procs, session, transcript
from .statusline import account_name

# How far a registry's startedAt may drift from the process start before we treat the
# pid as reused by another process.
_START_TOLERANCE_S = 120


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


@dataclass
class Session:
    pid: int
    session_id: str
    name: str
    start_cwd: str
    status: str  # "idle", "busy", or whatever Claude Code reports
    status_since_ms: int | None
    started_at_ms: int | None
    version: str | None
    kind: str | None
    tty: str | None
    host_app: str | None
    iterm: dict | None  # ItermSession as dict when found in iTerm2
    account: str | None = None  # "claude" (default config dir) or e.g. "claude-work" (~/.claude-work)
    limit: dict | None = None  # the account's usage limit in force: {"kind", "text", "at", "resets"}
    waiting_for: str | None = None  # with status "waiting": e.g. "input needed"
    stats: dict | None = None  # session.build(), when the transcript is found
    # Child processes: Bash tool commands running now (foreground, or a background shell's) and
    # others, such as MCP servers. [{"pid", "elapsed_s", "command", "tool", "shell_id"}]
    processes: list[dict] | None = None

    @property
    def is_idle(self) -> bool:
        return self.status == "idle"

    @property
    def attention(self) -> bool:
        """Waiting on you: idle, waiting (a question or permission), or in shell mode."""
        return self.status in ATTENTION

    def to_json(self) -> dict:
        """Pushed to every page each second, so the side panel's lists are left out: see detail_json."""
        data = asdict(self)
        data["is_idle"] = self.is_idle
        data["attention"] = self.attention
        data["in_iterm"] = self.iterm is not None
        if self.stats:
            data["stats"] = {k: v for k, v in self.stats.items() if k not in session.DETAIL_FIELDS}
            data["stats"]["activity_counts"] = {k: len(self.stats[k]) for k in transcript.ACTIVITY_FIELDS}
            # Changes whenever a file is read or edited again, a branch is added, or a sub-agent or
            # shell changes: the page refetches the detail.
            files = sum(sum(self.stats[k].values()) for k in transcript.ACTIVITY_KEYS)
            branches = sum(len(r["branches"]) + 1 for r in self.stats["repos"].values())
            tasks = json.dumps([self.stats["subagents"], self.stats["shells"]], sort_keys=True)
            data["stats"]["detail_version"] = f"{files}.{branches}.{hashlib.md5(tasks.encode()).hexdigest()[:12]}"
        return data

    def detail_json(self) -> dict:
        """Files, repos, sub-agents and shells (incl. sub-agents' activity), for the side panel."""
        return {k: (self.stats or {}).get(k, {} if k in transcript.ACTIVITY_FIELDS else []) for k in session.DETAIL_FIELDS}


ATTENTION = ("waiting", "idle", "shell")  # also the order they sort in


def read_registry(sessions_dir: Path) -> list[dict]:
    entries = []
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("pid"), int) and data.get("sessionId"):
            entries.append(data)
    return entries


def _is_live(entry: dict, table: dict[int, procs.Proc], now_s: float) -> bool:
    proc = table.get(entry["pid"])
    if proc is None:
        return False
    started_ms = entry.get("startedAt")
    if isinstance(started_ms, (int, float)):
        proc_start_s = now_s - proc.elapsed_s
        if abs(proc_start_s - started_ms / 1000) > _START_TOLERANCE_S:
            return False
    return True


def build_sessions(
    entries: list[dict],
    table: dict[int, procs.Proc],
    iterm_by_tty: dict[str, iterm.ItermSession],
    now_s: float,
) -> list[Session]:
    sessions = []
    for e in entries:
        if not _is_live(e, table, now_s):
            continue
        proc = table[e["pid"]]
        it = iterm_by_tty.get(proc.tty) if proc.tty else None
        sessions.append(
            Session(
                pid=e["pid"],
                session_id=str(e["sessionId"]),
                name=str(e.get("name") or e["sessionId"][:8]),
                start_cwd=str(e.get("cwd") or ""),
                status=str(e.get("status") or "unknown"),
                status_since_ms=e.get("statusUpdatedAt") or e.get("updatedAt"),
                started_at_ms=e.get("startedAt"),
                version=e.get("version"),
                kind=e.get("kind"),
                waiting_for=e.get("waitingFor") if isinstance(e.get("waitingFor"), str) else None,
                tty=proc.tty,
                host_app=procs.host_app(proc.pid, table),
                iterm=asdict(it) if it else None,
            )
        )
    return sort_sessions(sessions)


def sort_sessions(sessions: list[Session]) -> list[Session]:
    """Waiting first, then idle and shell, longest-waiting on top; then busy and the rest, most
    recently active on top; sessions blocked by their account's usage limit last."""

    def key(s: Session):
        since = s.status_since_ms or 0
        if s.limit:
            return (3, 0, -since)
        if s.attention:
            return (0, s.status != "waiting", since)
        return (1 if s.status == "busy" else 2, 0, -since)

    return sorted(sessions, key=key)


_stores: dict[Path, transcript.TranscriptStore] = {}


_indexes: dict[tuple[Path, Path], index.SessionIndex] = {}


def session_index() -> index.SessionIndex:
    key = (index.default_db_path(), claude_home() / "projects")
    if key not in _indexes:
        _indexes[key] = index.SessionIndex(*key)
    return _indexes[key]


def _store() -> transcript.TranscriptStore:
    projects = claude_home() / "projects"
    if projects not in _stores:
        _stores[projects] = transcript.TranscriptStore(projects, load_state=session_index().load_state)
    return _stores[projects]


def live_ids() -> set[str]:
    """Ids of the sessions running now (registry + ps), without transcript stats or iTerm2."""
    table = procs.snapshot()
    return {e.get("sessionId") for e in read_registry(claude_home() / "sessions") if _is_live(e, table, time.time())}


def attach_stats(sessions: list[Session], store: transcript.TranscriptStore) -> list[Session]:
    for s in sessions:
        # Tasks begun before this process started belong to an earlier run of a resumed session.
        s.stats = session.session_json(store, s.session_id, live_since=(s.started_at_ms or 0) / 1000)
    store.prune({s.session_id for s in sessions})
    return sessions


_accounts: dict[tuple[int, int | None], str] = {}  # (pid, startedAt) -> account; a process's env is fixed


def attach_accounts(sessions: list[Session]) -> None:
    missing = [s.pid for s in sessions if (s.pid, s.started_at_ms) not in _accounts]
    for pid, config_dir in procs.config_dirs(missing).items():
        s = next(s for s in sessions if s.pid == pid)
        _accounts[(pid, s.started_at_ms)] = account_name(config_dir)
    for s in sessions:
        s.account = _accounts.get((s.pid, s.started_at_ms))
    live = {(s.pid, s.started_at_ms) for s in sessions}
    for key in [k for k in _accounts if k not in live]:
        del _accounts[key]


def attach_limits(sessions: list[Session], now: datetime | None = None) -> None:
    """A limit hit in one session blocks every session on the same account."""
    now = now or datetime.now(UTC)
    by_account: dict[str | None, list[Session]] = {}
    for s in sessions:
        by_account.setdefault(s.account, []).append(s)
    for group in by_account.values():
        stats = [s.stats or {} for s in group]
        limit = limits.active([st.get("limit") for st in stats], [st.get("last_response_at") for st in stats], now)
        for s in group:
            s.limit = limit


def attach_processes(sessions: list[Session], table: dict[int, procs.Proc]) -> None:
    """Each session's child processes; a Bash tool shell running a background shell's command is
    linked to it (pid and run time go on the shell)."""
    kids = {s.pid: procs.children(s.pid, table) for s in sessions}
    args = procs.args_of([p.pid for ps in kids.values() for p in ps])
    for s in sessions:
        running = [sh for sh in (s.stats or {}).get("shells", []) if sh["state"] == "running"]
        s.processes = []
        for p in sorted(kids[s.pid], key=lambda p: p.elapsed_s):
            if p.pid not in args:
                continue  # exited between the two ps calls
            command = procs.tool_command(args[p.pid])
            shell = next((sh for sh in running if command and sh["command"] == command and "pid" not in sh), None)
            if shell:
                shell.update(pid=p.pid, elapsed_s=p.elapsed_s)
            s.processes.append({"pid": p.pid, "elapsed_s": p.elapsed_s, "command": command or args[p.pid],
                                "tool": command is not None, "shell_id": shell["id"] if shell else None})


_iterm_cache = iterm.ItermCache()
# The poller and direct API calls can run at the same time; the caches aren't thread-safe.
_lock = threading.Lock()


def live_sessions() -> list[Session]:
    with _lock:
        table = procs.snapshot()
        entries = read_registry(claude_home() / "sessions")
        live = {(e["pid"], table[e["pid"]].tty) for e in entries if e["pid"] in table and table[e["pid"]].tty}
        by_tty = _iterm_cache.get(iterm.is_running(p.comm for p in table.values()), live)
        sessions = attach_stats(build_sessions(entries, table, by_tty, time.time()), _store())
        attach_processes(sessions, table)
        attach_accounts(sessions)
        attach_limits(sessions)
        return sort_sessions(sessions)
