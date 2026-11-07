"""Local SQLite index of every transcript on disk, for the session browser and fast restarts.

Transcripts add up to gigabytes (sub-agents especially), so each file's reader state is saved
with the file's size and mtime. A scan re-reads only files whose size or mtime changed, and
those from where they left off. Rows of files Claude Code has deleted (after its 30-day
cleanup) are dropped, so the index mirrors what is on disk. It is a cache: deleting the
database file only costs one full re-read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import flow, session, transcript
from .paths import CACHE

# Bump when the saved reader state or the parsing changes: the index is rebuilt from scratch.
SCHEMA_VERSION = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_session ON files(session_id);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    first_ts TEXT,
    last_ts TEXT,
    title TEXT,
    last_prompt TEXT,
    start_cwd TEXT,
    cwd TEXT,
    dirs TEXT,  -- every directory the session worked in, one per line: for search
    model TEXT,
    tokens_total INTEGER,  -- incl. sub-agents
    requests INTEGER,
    stats TEXT NOT NULL,
    activity TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_last_ts ON sessions(last_ts);
CREATE TABLE IF NOT EXISTS events (  -- one row per tool call, for the explorer
    path TEXT NOT NULL,  -- the transcript it is in: main or a sub-agent's
    id TEXT NOT NULL,  -- tool_use id
    session_id TEXT NOT NULL,
    agent_id TEXT,  -- the sub-agent that made the call; NULL: the main session
    seq INTEGER,  -- order within the transcript
    ts TEXT,
    tool TEXT,
    category TEXT,  -- web, commands, files, mcp, agents, other
    grp TEXT,  -- within the category: domain, program, tool, MCP server, agent type
    summary TEXT,
    cwd TEXT,
    detail TEXT,  -- the input as JSON, long strings cut
    status TEXT,  -- pending (no result yet), ok, error, interrupted, background
    exit_code INTEGER,
    preview TEXT,  -- start of the result
    result_ts TEXT,
    agent_ref TEXT,  -- for a sub-agent launch: the agent's id
    PRIMARY KEY (path, id)
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, ts);
CREATE TABLE IF NOT EXISTS messages (  -- what an agent was told by another, for the flow
    path TEXT NOT NULL,  -- the receiver's transcript
    n INTEGER NOT NULL,  -- order within it
    session_id TEXT NOT NULL,
    agent_id TEXT,  -- the receiver; NULL: the main session
    ts TEXT,
    kind TEXT,  -- prompt, message, idle, handback, notification, result
    peer TEXT,  -- the sender as written: a name, an agent id; NULL: the parent
    title TEXT,
    text TEXT,
    PRIMARY KEY (path, n)
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, ts);
"""
EVENT_FIELDS = ("agent_id", "ts", "tool", "category", "grp", "summary", "cwd", "detail", "status", "exit_code",
                "preview", "result_ts", "agent_ref", "id")


def default_db_path() -> Path:
    env = os.environ.get("HARNESS_WATCH_DB")
    return Path(env) if env else CACHE / "index.sqlite3"


class SessionIndex:
    def __init__(self, db_path: Path, projects_dir: Path):
        self.db_path = db_path
        self.projects_dir = projects_dir
        self._local = threading.local()
        self._scan_lock = threading.Lock()
        self.progress = {"running": False, "files_done": 0, "files_total": 0,
                         "bytes_done": 0, "bytes_total": 0, "finished_at": None}
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _db(self) -> sqlite3.Connection:
        """One connection per thread: the poller, the scanner and API calls run concurrently."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # SQLite's lower() and LIKE only fold ASCII; search must also match "după" for "DUPĂ".
            conn.create_function("fold", 1, lambda s: s.casefold() if isinstance(s, str) else "", deterministic=True)
        return conn

    def _init_schema(self) -> None:
        db = self._db()
        db.executescript(SCHEMA)
        meta = dict(db.execute("SELECT key, value FROM meta"))
        expected = {"schema": str(SCHEMA_VERSION), "projects_dir": str(self.projects_dir)}
        if any(meta.get(k) != v for k, v in expected.items()):
            with db:
                for table in ("files", "sessions", "events", "messages"):
                    db.execute(f"DROP TABLE IF EXISTS {table}")
                db.executescript(SCHEMA)
                db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", expected.items())

    # Reader state, for the live list's warm start.

    def load_state(self, path: Path) -> dict | None:
        row = self._db().execute("SELECT state FROM files WHERE path = ?", (str(path),)).fetchone()
        return json.loads(row[0]) if row else None

    # Scanning.

    def _discover(self, sid: str = "*") -> dict[str, tuple[str, int, int]]:
        """path -> (session id, size, mtime_ns) for main and sub-agent transcripts (of one session)."""
        found = {}
        for pattern, sid_of in ((f"*/{sid}.jsonl", lambda p: p.stem),
                                (f"*/{sid}/subagents/agent-*.jsonl", lambda p: p.parent.parent.name)):
            for p in self.projects_dir.glob(pattern):
                try:
                    st = p.stat()
                except OSError:
                    continue
                found[str(p)] = (sid_of(p), st.st_size, st.st_mtime_ns)
        return found

    def scan(self) -> int:
        """Bring the index up to date; returns how many sessions changed. One scan at a time."""
        with self._scan_lock:
            return self._scan()

    def refresh_session(self, sid: str) -> bool:
        """Read what one session's transcripts gained since the last scan (a running session's
        latest tool calls). False when a scan is running: the caller shows what is indexed."""
        if not re.fullmatch(r"[\w-]+", sid) or not self._scan_lock.acquire(blocking=False):
            return False
        try:
            self._scan(sid)
            return True
        finally:
            self._scan_lock.release()

    def _scan(self, only: str | None = None) -> int:
        db = self._db()
        found = self._discover(only or "*")
        known = {path: (size, mtime, offset) for path, size, mtime, offset in db.execute(
            "SELECT path, size, mtime_ns, json_extract(state, '$.offset') FROM files"
            + (" WHERE session_id = ?" if only else ""), (only,) if only else ())}
        changed = [p for p, (_, size, mtime) in found.items() if known.get(p, (None, None))[:2] != (size, mtime)]
        removed = [p for p in known if p not in found] if not only else []
        changed.sort(key=lambda p: "/subagents/" in p)  # main transcripts first: the list fills sooner
        progress = self.progress if not only else {}  # a one-session refresh isn't "indexing"
        progress.update(
            running=True, files_done=0, files_total=len(changed), bytes_done=0,
            bytes_total=sum(max(0, found[p][1] - ((known.get(p) or (0, 0, 0))[2] or 0)) for p in changed))
        dirty: set[str] = set()
        try:
            for path in changed:
                sid, size, mtime = found[path]
                state = self.load_state(Path(path))
                stats = (transcript.TranscriptStats.from_state(Path(path), state) if state
                         else transcript.TranscriptStats(Path(path), include_sidechain="/subagents/" in path))
                before = stats.offset
                stats.events = [("restart", {})] if not state else []
                stats.refresh()
                with db:
                    db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?)",
                               (path, sid, size, mtime, json.dumps(stats.to_state())))
                    self._write_events(db, path, sid, stats.events)
                dirty.add(sid)
                progress["files_done"] += 1
                progress["bytes_done"] += max(0, stats.offset - before)
                if "/subagents/" not in path:
                    self._rebuild_session(sid)  # visible right away; sub-agents merge in later
            with db:
                for path in removed:
                    dirty.add(db.execute("SELECT session_id FROM files WHERE path = ?", (path,)).fetchone()[0])
                    db.execute("DELETE FROM files WHERE path = ?", (path,))
                    db.execute("DELETE FROM events WHERE path = ?", (path,))
                    db.execute("DELETE FROM messages WHERE path = ?", (path,))
            for sid in dirty:
                self._rebuild_session(sid)
        finally:
            progress.update(running=False, finished_at=time.time())
        return len(dirty)

    def _write_events(self, db: sqlite3.Connection, path: str, sid: str, evs: list[tuple[str, dict]]) -> None:
        agent = session.agent_id(Path(path)) if "/subagents/" in path else None
        seq = db.execute("SELECT coalesce(max(seq), 0) FROM events WHERE path = ?", (path,)).fetchone()[0]
        n = db.execute("SELECT coalesce(max(n), 0) FROM messages WHERE path = ?", (path,)).fetchone()[0]
        for kind, ev in evs:
            if kind == "restart":
                db.execute("DELETE FROM events WHERE path = ?", (path,))
                db.execute("DELETE FROM messages WHERE path = ?", (path,))
                seq = n = 0
            elif kind == "msg":
                n += 1
                db.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (path, n, sid, agent, ev["ts"], ev["kind"], ev["peer"], ev["title"], ev["text"]))
            elif not ev.get("id"):
                continue
            elif kind == "use":
                seq += 1
                db.execute(
                    "INSERT OR REPLACE INTO events (path, id, session_id, agent_id, seq, ts, tool, category, grp,"
                    " summary, cwd, detail, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (path, ev["id"], sid, agent, seq, ev["ts"], ev["tool"], ev["category"], ev["group"],
                     ev["summary"], ev["cwd"], ev["detail"]))
            else:
                db.execute("UPDATE events SET status = ?, exit_code = ?, preview = ?, result_ts = ?, agent_ref = ?"
                           " WHERE path = ? AND id = ?",
                           (ev["status"], ev["exit_code"], ev["preview"], ev["ts"], ev["agent"], path, ev["id"]))

    def _rebuild_session(self, sid: str) -> None:
        db = self._db()
        rows = db.execute("SELECT path, state FROM files WHERE session_id = ?", (sid,)).fetchall()
        readers = [transcript.TranscriptStats.from_state(Path(p), json.loads(s)) for p, s in rows]
        mains = [r for r in readers if "/subagents/" not in str(r.path)]
        with db:
            if not mains:
                db.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                return
            # The same id in two project dirs (rare): keep the most recently active one.
            main = max(mains, key=lambda r: r.last_activity or "")
            subs = [(sub, transcript.read_meta(sub.path)) for sub in readers
                    if "/subagents/" in str(sub.path) and sub.path.parent.parent.parent == main.path.parent]
            j = session.build(main, subs, live_since=None)
            stats = {k: v for k, v in j.items() if k not in session.DETAIL_FIELDS}
            activity = {k: j[k] for k in session.DETAIL_FIELDS}
            start_cwd = j["cwd_history"][0]["cwd"] if j["cwd_history"] else None
            dirs = "\n".join(dict.fromkeys(h["cwd"] for h in j["cwd_history"]))
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, str(main.path), j["first_activity"], j["last_activity"], j["title"], j["last_prompt"],
                 start_cwd, j["cwd"], dirs, j["model"], j["tokens_total"], j["requests"],
                 json.dumps(stats), json.dumps(activity)))

    # Queries.

    def page(self, page: int, size: int, query: str = "") -> tuple[int, list[dict]]:
        """Newest first. `query`: every word must appear in the title (or, untitled, the last
        prompt), the session id, or one of the directories the session worked in."""
        db = self._db()
        where, params = [], []
        for term in query.split():
            like = _like(term)
            where.append("(fold(coalesce(title, last_prompt)) LIKE ? ESCAPE '\\'"
                         " OR session_id LIKE ? ESCAPE '\\' OR fold(dirs) LIKE ? ESCAPE '\\')")
            params += [like, like, like]
        cond = " WHERE " + " AND ".join(where) if where else ""
        total = db.execute(f"SELECT count(*) FROM sessions{cond}", params).fetchone()[0]
        rows = db.execute(
            "SELECT session_id, title, last_prompt, start_cwd, cwd, first_ts, last_ts, model, tokens_total, requests"
            f" FROM sessions{cond} ORDER BY last_ts DESC LIMIT ? OFFSET ?", [*params, size, (page - 1) * size]).fetchall()
        keys = ("session_id", "title", "last_prompt", "start_cwd", "cwd", "first_ts", "last_ts", "model",
                "tokens_total", "requests")
        return total, [dict(zip(keys, r)) for r in rows]

    def _event_filter(self, sid: str, query: str, category: str | None = None, group: str | None = None,
                      agent: str | None = None):
        where, params = ["session_id = ?"], [sid]
        if agent:  # "main": the main session's own calls
            where.append("agent_id IS NULL" if agent == "main" else "agent_id = ?")
            params += [] if agent == "main" else [agent]
        if category:
            where.append("category = ?")
            params.append(category)
        if group is not None:
            where.append("grp = ?")
            params.append(group)
        for term in query.split():  # every word in the summary, tool or group
            where.append("(fold(summary) LIKE ? ESCAPE '\\' OR fold(tool) LIKE ? ESCAPE '\\' OR fold(grp) LIKE ? ESCAPE '\\')")
            params += [_like(term)] * 3
        return " WHERE " + " AND ".join(where), params

    def tool_summary(self, sid: str, query: str = "", agent: str | None = None) -> dict:
        """Calls per category, and per group within each (most used first)."""
        cond, params = self._event_filter(sid, query, agent=agent)
        db = self._db()
        categories = dict(db.execute(f"SELECT category, count(*) FROM events{cond} GROUP BY category", params))
        groups = [dict(zip(("category", "group", "count", "errors", "last_ts"), row)) for row in db.execute(
            f"SELECT category, grp, count(*), sum(status = 'error'), max(ts) FROM events{cond}"
            " GROUP BY category, grp ORDER BY count(*) DESC, grp", params)]
        return {"total": sum(categories.values()), "categories": categories, "groups": groups}

    def tool_events(self, sid: str, category: str | None = None, group: str | None = None, query: str = "",
                    offset: int = 0, limit: int = 100, agent: str | None = None) -> tuple[int, list[dict]]:
        """Calls newest first, main session and sub-agents together (or one of them: `agent`)."""
        cond, params = self._event_filter(sid, query, category, group, agent)
        db = self._db()
        total = db.execute(f"SELECT count(*) FROM events{cond}", params).fetchone()[0]
        rows = db.execute(f"SELECT {', '.join(EVENT_FIELDS)} FROM events{cond} ORDER BY ts DESC, seq DESC"
                          " LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return total, [dict(zip(EVENT_FIELDS, r)) for r in rows]

    def _flow_rows(self, sid: str, preview: bool):
        text = f"substr(text, 1, {flow.PREVIEW})" if preview else "text"
        keys = ("n", "agent_id", "ts", "kind", "peer", "title", "preview" if preview else "text")
        rows = self._db().execute(
            f"SELECT rowid, agent_id, ts, kind, peer, title, {text} FROM messages WHERE session_id = ? ORDER BY ts, rowid",
            (sid,)).fetchall()
        return [dict(zip(keys, r)) for r in rows]

    def _agents(self, sid: str) -> list[dict]:
        row = self._db().execute("SELECT activity FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        return json.loads(row[0]).get("subagents") or [] if row else []

    def flow(self, sid: str) -> dict:
        """Lanes and messages of one session (see `flow.build`); a message's `n` fetches its text."""
        stamps: dict[str | None, list[str]] = {}
        for agent, ts, result_ts in self._db().execute(
                "SELECT agent_id, ts, result_ts FROM events WHERE session_id = ?", (sid,)):
            stamps.setdefault(agent, []).extend(t for t in (ts, result_ts) if t)
        return flow.build(self._agents(sid), self._flow_rows(sid, preview=True), stamps)

    def flow_messages(self, sid: str, a: str, b: str) -> list[dict]:
        """Every message between lanes `a` and `b`, either way, with its full text."""
        resolve = flow.resolver(self._agents(sid))
        out = []
        for r in self._flow_rows(sid, preview=False):
            to = r["agent_id"] or "main"
            frm = resolve(r["peer"], to)
            if {frm, to} == {a, b} and frm != to:
                out.append({"n": r["n"], "ts": r["ts"], "from": frm, "to": to, "kind": r["kind"],
                            "title": r["title"], "text": r["text"]})
        return out

    def count(self) -> int:
        return self._db().execute("SELECT count(*) FROM sessions").fetchone()[0]

    def get(self, sid: str) -> dict | None:
        row = self._db().execute(
            "SELECT path, start_cwd, stats, activity FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        if row is None:
            return None
        path, start_cwd, stats, activity = row
        return {"session_id": sid, "path": path, "start_cwd": start_cwd,
                "stats": json.loads(stats), "activity": json.loads(activity)}


def _like(term: str) -> str:
    """A LIKE pattern matching `term` anywhere, case-folded, its wildcards literal; `~` is home."""
    if term.startswith("~"):
        term = str(Path.home()) + term[1:]
    return "%" + re.sub(r"([\\%_])", r"\\\1", term.casefold()) + "%"
