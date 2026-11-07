"""FastAPI app: JSON API, server-sent events, and the static page."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, iterm, key, registry, session, usage
from .paths import CACHE

HOST = "127.0.0.1"
# An extra name the page may be opened by, e.g. through the port-80 relay (relay/relay.py)
HOSTNAME = os.environ.get("HARNESS_WATCH_HOSTNAME") or None
PORT = int(os.environ.get("HARNESS_WATCH_PORT", "8787"))
POLL_S = 1.0
# No other site may frame the page (a click on Fork in an invisible frame would pass the Origin
# check), and the page loads nothing from elsewhere. Inline styles: the bars' widths.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
KEEPALIVE_S = 15.0
RESCAN_S = 30.0  # index rescan interval: only changed transcripts are re-read
STATIC = Path(__file__).parent / "static"
log = logging.getLogger(__name__)


def page_version() -> str:
    """Changes whenever a file of the page does, so an open page knows to reload itself."""
    stats = sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in STATIC.iterdir() if f.is_file())
    return hashlib.sha1(repr(stats).encode()).hexdigest()[:12]


class Static(StaticFiles):
    """The page's files, revalidated on every load (cheap, by ETag) so a reload picks up new code."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class Poller:
    """One polling loop shared by every open page; runs only while a page is connected."""

    def __init__(self, fn, interval: float = POLL_S):
        self.fn = fn
        self.interval = interval
        self.data: str | None = None
        self.version = 0
        self.subscribers = 0
        self._changed = asyncio.Condition()
        self._task: asyncio.Task | None = None

    def subscribe(self) -> None:
        self.subscribers += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def unsubscribe(self) -> None:
        self.subscribers -= 1

    async def _run(self) -> None:
        while self.subscribers > 0:
            try:
                data = json.dumps(await asyncio.to_thread(self.fn))
            except Exception:
                log.exception("poll failed")  # keep serving the last good snapshot
                data = self.data
            if data != self.data:
                self.data = data
                self.version += 1
                async with self._changed:
                    self._changed.notify_all()
            await asyncio.sleep(self.interval)

    async def wait_newer(self, version: int, timeout: float) -> tuple[int, str | None]:
        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(lambda: self.version > version), timeout)
            except TimeoutError:
                pass
        return self.version, self.data


def start_scan(idx, min_age_s: float = RESCAN_S) -> None:
    """Rescan the index in a background thread, unless one is running or ran recently."""
    finished = idx.progress["finished_at"]
    if idx.progress["running"] or (finished and time.time() - finished < min_age_s):
        return

    def run():
        try:
            idx.scan()
        except Exception:
            log.exception("index scan failed")
            idx.progress.update(running=False, finished_at=time.time())

    idx.progress["running"] = True  # claimed before the thread starts, so callers don't start two
    threading.Thread(target=run, name="index-scan", daemon=True).start()


def create_app(
    sessions_fn=registry.live_sessions,
    focus_fn=iterm.focus,
    fork_fn=iterm.fork,
    port: int = PORT,
    index_fn=registry.session_index,
    live_ids_fn=registry.live_ids,
    usage_fn=lambda: [],
    hostname: str | None = None,
    page_key: str | None = None,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async def rescan_loop():  # keeps the index current, so restarts and the browser stay fast
            while True:
                try:
                    start_scan(await asyncio.to_thread(index_fn))
                except Exception:
                    log.exception("could not open the index")
                await asyncio.sleep(RESCAN_S)

        task = asyncio.create_task(rescan_loop())
        yield
        task.cancel()

    app = FastAPI(title="harness-watch", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"{HOST}:{port}", f"localhost:{port}"}
    if hostname:
        allowed_hosts |= {f"{hostname}:{port}", hostname}  # bare: through the port-80 relay
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    latest = {"at": 0.0, "sessions": []}  # the last poll, reused by the side panel's requests

    def current_sessions() -> list[registry.Session]:
        # The page fetches a session's detail when a push says it changed: answer from that same poll.
        if not poller.subscribers or time.monotonic() - latest["at"] > POLL_S * 2:
            latest.update(at=time.monotonic(), sessions=sessions_fn())
        return latest["sessions"]

    def snapshot_fn() -> dict:
        sessions = sessions_fn()
        latest.update(at=time.monotonic(), sessions=sessions)
        return {"sessions": [s.to_json() for s in sessions], "usage": usage_fn(), "page": page_version()}

    poller = Poller(snapshot_fn)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Blocks DNS-rebinding: a website that points its own hostname at 127.0.0.1
        # would send its hostname here, not ours.
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        # Actions only from our own page: any other site open in the browser could
        # otherwise POST to 127.0.0.1 (its requests carry its own Origin).
        path = request.url.path
        handing_key = page_key and path == "/__key" and request.method == "POST"  # from a local file: Origin "null"
        if request.method != "GET" and not handing_key and request.headers.get("origin") not in allowed_origins:
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        response = await keyed(request, path) if page_key else None
        if response is None:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    async def keyed(request: Request, path: str):
        """Other accounts on this Mac reach 127.0.0.1 too: data and actions need the key. The page's
        own files don't (they hold none), so it can tell you to run `harness-watch open`."""
        if path == "/__proof":  # `harness-watch open` asks before handing the key to whoever holds the port
            return PlainTextResponse(key.proof(page_key, request.query_params.get("n", "")[:256]))
        if path == "/__key" and request.method == "POST":
            form = urllib.parse.parse_qs((await request.body()).decode("utf-8", "replace"))
            if key.matches((form.get(key.PARAM) or [None])[0], page_key):
                page, csp = key.keeper(page_key)
                return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})
            return PlainTextResponse("wrong key\n", status_code=401)
        given = request.headers.get(key.HEADER) or request.query_params.get(key.PARAM)
        if path.startswith("/api/") and not key.matches(given, page_key):
            return JSONResponse({"error": "needs this Mac's key: run `harness-watch open`"}, status_code=401)
        return None

    def find_session(pid: int) -> registry.Session:
        for s in current_sessions():
            if s.pid == pid:
                return s
        raise HTTPException(404, "session not running")

    @app.get("/api/sessions")
    async def sessions():
        return await asyncio.to_thread(snapshot_fn)

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events():
            poller.subscribe()
            try:
                seen = 0
                while not await request.is_disconnected():
                    version, data = await poller.wait_newer(seen, KEEPALIVE_S)
                    if version > seen and data is not None:
                        seen = version
                        yield f"data: {data}\n\n"
                    else:
                        yield ": keepalive\n\n"
            finally:
                poller.unsubscribe()

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/sessions/{pid}/detail")
    async def detail(pid: int):
        return (await asyncio.to_thread(find_session, pid)).detail_json()

    def find_shell(session_id: str, task_id: str) -> dict | None:
        live = next((s for s in current_sessions() if s.session_id == session_id), None)
        if live:
            shells = (live.stats or {}).get("shells", [])
        else:
            shells = ((index_fn().get(session_id) or {}).get("activity") or {}).get("shells", [])
        return next((sh for sh in shells if sh["id"] == task_id), None)

    @app.get("/api/output")
    async def output(session_id: str, task_id: str):
        """The end of a background shell's output file. Only files the session's transcript names
        as a task's output are read."""
        shell = await asyncio.to_thread(find_shell, session_id, task_id)
        if shell is None:
            raise HTTPException(404, "no such background shell")
        path = shell.get("output_file")
        pattern = r"/\S+/tasks/" + re.escape(task_id) + r"\.output"
        if not path or not re.fullmatch(pattern, path) or os.path.normpath(path) != path:
            raise HTTPException(404, "no output file recorded")
        return {"state": shell["state"], **await asyncio.to_thread(session.tail, path)}

    @app.get("/api/history")
    async def history(page: int = 1, size: int = 50, q: str = ""):
        page, size = max(1, page), min(max(1, size), 200)
        idx = await asyncio.to_thread(index_fn)
        start_scan(idx, min_age_s=5)  # picks up sessions closed since the last scan
        total, items = await asyncio.to_thread(idx.page, page, size, q[:200])
        live = await asyncio.to_thread(live_ids_fn)
        for item in items:
            item["live"] = item["session_id"] in live
        return {"total": total, "all": await asyncio.to_thread(idx.count), "page": page, "size": size,
                "query": q, "indexing": idx.progress, "items": items}

    @app.get("/api/history/{session_id}")
    async def history_session(session_id: str):
        idx = await asyncio.to_thread(index_fn)
        data = await asyncio.to_thread(idx.get, session_id)
        if data is None:
            raise HTTPException(404, "no transcript for this session")
        data["live"] = session_id in await asyncio.to_thread(live_ids_fn)
        return data

    def fresh_index(session_id: str):
        idx = index_fn()
        idx.refresh_session(session_id)  # a running session's newest calls; skipped while a scan runs
        return idx

    @app.get("/api/tools/{session_id}/summary")
    async def tool_summary(session_id: str, q: str = "", agent: str | None = None):
        idx = await asyncio.to_thread(fresh_index, session_id)
        return await asyncio.to_thread(idx.tool_summary, session_id, q[:200], agent)

    @app.get("/api/tools/{session_id}/events")
    async def tool_events(session_id: str, category: str | None = None, group: str | None = None, q: str = "",
                          offset: int = 0, limit: int = 100, agent: str | None = None):
        idx = await asyncio.to_thread(fresh_index, session_id)
        total, items = await asyncio.to_thread(
            idx.tool_events, session_id, category, group, q[:200], max(0, offset), min(max(1, limit), 500), agent)
        return {"total": total, "offset": offset, "items": items}

    @app.get("/api/flow/{session_id}")
    async def flow(session_id: str):
        idx = await asyncio.to_thread(fresh_index, session_id)
        return await asyncio.to_thread(idx.flow, session_id)

    @app.get("/api/flow/{session_id}/between")
    async def flow_between(session_id: str, a: str, b: str):
        idx = await asyncio.to_thread(fresh_index, session_id)
        return {"items": await asyncio.to_thread(idx.flow_messages, session_id, a, b)}

    @app.post("/api/sessions/{pid}/focus")
    async def focus(pid: int):
        session = await asyncio.to_thread(find_session, pid)
        if session.iterm is None:
            raise HTTPException(409, "session is not running in iTerm2")
        if not await asyncio.to_thread(focus_fn, session.iterm["session_id"]):
            raise HTTPException(502, "iTerm2 could not focus the session")
        return {"ok": True}

    @app.post("/api/fork/{session_id}")
    async def fork(session_id: str, request: Request):
        """Opens a new iTerm2 tab running `claude --resume <id> --fork-session --name <name>` from
        the session's start directory (where Claude Code keeps its transcript), next to the
        session's own tab."""
        if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z-]{0,63}", session_id):  # never an option to claude
            raise HTTPException(400, "not a session id")
        body = await request.json() if await request.body() else {}
        name = " ".join(str((body or {}).get("name") or "").split())[:120] or None  # one line
        live = next((s for s in await asyncio.to_thread(current_sessions) if s.session_id == session_id), None)
        if live:
            cwd, window = live.start_cwd, (live.iterm or {}).get("window_id")
        else:
            data = await asyncio.to_thread(lambda: index_fn().get(session_id))
            if data is None:
                raise HTTPException(404, "no transcript for this session")
            cwd, window = data.get("start_cwd"), None
        if not cwd or not os.path.isdir(cwd):
            raise HTTPException(409, f"the session's start directory no longer exists: {cwd or '?'}")
        if not await asyncio.to_thread(fork_fn, cwd, session_id, name, window):
            raise HTTPException(502, "iTerm2 could not open a tab")
        return {"ok": True}

    app.mount("/", Static(directory=STATIC, html=True), name="static")
    return app


def serve() -> None:
    os.umask(0o077)  # the index copies every transcript: readable by you only, as ~/.claude is
    CACHE.mkdir(parents=True, exist_ok=True)
    CACHE.chmod(0o700)  # also when an older version made it readable to all
    extra = f" and http://{HOSTNAME}" if HOSTNAME else ""
    print(f"harness-watch on http://{HOST}:{PORT}{extra}; open it with `harness-watch open`", flush=True)
    app = create_app(usage_fn=usage.load_source(), hostname=HOSTNAME, page_key=key.load())
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def open_page(host: str | None, show: bool, fetch=None) -> str:
    """Hand the key to your browser for http://<host>/: only once the server there proves it holds
    the key, and through a file only you can read, never a command line or an address."""
    base = f"http://{host or f'{HOST}:{PORT}'}"
    page_key = key.load()
    nonce = key.proof(page_key, str(time.time_ns()))[:32]  # any fresh value
    try:
        answer = (fetch or _get)(f"{base}/__proof?n={nonce}")
    except OSError as e:
        raise SystemExit(f"harness-watch isn't answering on {base} ({e}): start it, or run `harness-watch install-agent`")
    if answer.strip() != key.proof(page_key, nonce):
        raise SystemExit(f"whatever answers on {base} doesn't know this Mac's key: not handing it over")
    opener = key.PATH.with_name("open.html")
    fd = os.open(opener, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.opener(f"{base}/__key", page_key))
    if show:
        return f"open this file in the browser to use: {opener}"
    subprocess.run(["open", str(opener)], check=True)
    return f"opened {base}/ with this Mac's key; the browser keeps it"


def _get(url: str) -> str:
    """GET without following redirects: a server at `url` could otherwise relay the proof from the
    real one and receive the key."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.netloc, timeout=5)
    try:
        conn.request("GET", f"{parts.path}?{parts.query}")
        resp = conn.getresponse()
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status}")
        return resp.read().decode()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness-watch", description="Dashboard of your live Claude Code sessions.")
    parser.add_argument("command", nargs="?", choices=["serve", "open", "install-agent", "uninstall-agent"],
                        default="serve", help="serve (default); open: the page, with its key, in your browser; "
                        "install-agent: run at login, restarted if it stops; uninstall-agent")
    parser.add_argument("--host", help="open: the name to open it by (default 127.0.0.1:<port>)")
    parser.add_argument("--print", action="store_true", help="open: print the file to open in another browser")
    args = parser.parse_args()
    if args.command == "serve":
        serve()
    elif args.command == "open":
        print(open_page(args.host or HOSTNAME, args.print))
    else:
        print(agent.install() if args.command == "install-agent" else agent.uninstall())
