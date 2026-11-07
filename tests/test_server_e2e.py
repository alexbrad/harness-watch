"""End to end: a real process registered in a temp Claude config dir, read through the real
`ps` and served by the HTTP API."""

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from harness_watch import server

PORT = 8787


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def live_proc():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield p
    p.kill()
    p.wait()


def register(home, pid, **fields):
    data = {
        "pid": pid,
        "sessionId": f"sess-{pid}",
        "cwd": "/Users/me/workspace/demo",
        "name": "demo-session",
        "status": "idle",
        "statusUpdatedAt": int(time.time() * 1000) - 90_000,
        "startedAt": int(time.time() * 1000),
        **fields,
    }
    (home / "sessions" / f"{pid}.json").write_text(json.dumps(data))


def client():
    return TestClient(server.create_app(port=PORT), base_url=f"http://127.0.0.1:{PORT}")


def test_lists_live_session_and_drops_dead_one(claude_home, live_proc):
    register(claude_home, live_proc.pid)
    register(claude_home, 999_999, name="dead-session")

    resp = client().get("/api/sessions")

    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert [s["name"] for s in sessions] == ["demo-session"]
    s = sessions[0]
    assert s["is_idle"] is True
    assert s["start_cwd"] == "/Users/me/workspace/demo"
    assert s["in_iterm"] is False  # a background process has no iTerm2 tty


def test_session_disappears_when_process_exits(claude_home, live_proc):
    register(claude_home, live_proc.pid)
    c = client()
    assert len(c.get("/api/sessions").json()["sessions"]) == 1

    live_proc.kill()
    live_proc.wait()

    assert c.get("/api/sessions").json()["sessions"] == []


def test_stats_follow_the_transcript(claude_home, live_proc):
    sid = f"sess-{live_proc.pid}"
    register(claude_home, live_proc.pid)
    proj = claude_home / "projects" / "-Users-me-workspace-demo"
    proj.mkdir(parents=True)
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 0, "output_tokens": 0}

    def append(entry):
        with (proj / f"{sid}.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")

    append({"type": "user", "cwd": "/Users/me/workspace/demo", "timestamp": "t1"})
    append({"type": "assistant", "cwd": "/Users/me/workspace/demo", "gitBranch": "main", "timestamp": "t1",
            "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": usage}})
    c = client()
    st = c.get("/api/sessions").json()["sessions"][0]["stats"]
    assert st["context_tokens"] == 90_010
    assert st["git_branch"] == "main"
    assert st["last_response_at"] == "t1"

    # The session cds elsewhere and does more work: the next poll picks up only the new lines.
    append({"type": "assistant", "cwd": "/Users/me/workspace/demo/sub", "timestamp": "t2",
            "message": {"id": "m2", "model": "claude-opus-5-5", "usage": {**usage, "cache_read_input_tokens": 500_000},
                        "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "/x/y.py"}}]}})
    st = c.get("/api/sessions").json()["sessions"][0]["stats"]
    assert st["requests"] == 2
    assert st["context_pct"] == 50.0
    assert st["cwd"] == "/Users/me/workspace/demo/sub"
    assert len(st["cwd_history"]) == 2
    assert "files_edited" not in st  # the list pushed every second stays small
    assert st["activity_counts"]["files_edited"] == 1
    assert c.get(f"/api/sessions/{live_proc.pid}/detail").json()["files_edited"] == {"/x/y.py": 1}
    assert st["last_response_at"] == "t2"

    # A tool result is not a reply: the "Last reply" column keeps Claude's last response.
    append({"type": "user", "cwd": "/Users/me/workspace/demo/sub", "timestamp": "t3",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "u1", "content": "ok"}]}})
    assert c.get("/api/sessions").json()["sessions"][0]["stats"]["last_response_at"] == "t2"


def test_page_is_served(claude_home):
    resp = client().get("/")
    assert resp.status_code == 200
    assert "Claude sessions" in resp.text
    for path in ("/", "/app.js", "/style.css"):  # else the browser keeps old code across a server update
        assert client().get(path).headers["cache-control"] == "no-cache"


def test_snapshot_carries_the_page_version(claude_home, tmp_path, monkeypatch):
    """An open page compares it with the one it loaded with and reloads when the code changed."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "app.js").write_text("v1")
    monkeypatch.setattr(server, "STATIC", static)
    first = server.page_version()
    assert client().get("/api/sessions").json()["page"] == first
    (static / "app.js").write_text("v2, longer")
    assert server.page_version() != first


def test_rejects_foreign_host_header(claude_home):
    resp = client().get("/api/sessions", headers={"host": "evil.example:8787"})
    assert resp.status_code == 403


def test_serves_its_hostname_also_through_the_port_80_relay(claude_home):
    named = lambda: TestClient(server.create_app(port=PORT, hostname="watch.test"), base_url=f"http://127.0.0.1:{PORT}")  # noqa: E731
    for host in ("watch.test", f"watch.test:{PORT}"):
        assert named().get("/api/sessions", headers={"host": host}).status_code == 200, host
        assert client().get("/api/sessions", headers={"host": host}).status_code == 403, host  # not configured
    c = TestClient(server.create_app(port=PORT, hostname="watch.test"), base_url="http://watch.test")
    assert c.post("/api/sessions/1/focus", headers={"origin": "http://watch.test"}).status_code == 404
    assert c.post("/api/sessions/1/focus", headers={"origin": "http://evil.test"}).status_code == 403
    # Port 80 of 127.0.0.1 is not ours: whatever answers a bare "localhost" isn't harness-watch.
    for host in ("evil.test", "localhost", "127.0.0.1"):
        assert named().get("/", headers={"host": host}).status_code == 403, host


ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}


def fake_session(pid, in_iterm=True):
    from harness_watch import registry

    return registry.Session(
        pid=pid, session_id=f"s-{pid}", name=f"n-{pid}", start_cwd="/x", status="idle",
        status_since_ms=None, started_at_ms=None, version=None, kind=None, tty="ttys001",
        host_app="iTerm" if in_iterm else "Terminal",
        iterm={"window_id": 1, "tab_index": 2, "session_id": f"UID-{pid}", "tty": "ttys001", "name": ""}
        if in_iterm else None,
    )


def focus_client(focused, result=True):
    def focus_fn(uid):
        focused.append(uid)
        return result

    app = server.create_app(
        sessions_fn=lambda: [fake_session(1), fake_session(2, in_iterm=False)],
        focus_fn=focus_fn,
        port=PORT,
    )
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_focus_targets_the_sessions_iterm_uid():
    focused = []
    resp = focus_client(focused).post("/api/sessions/1/focus", headers=ORIGIN)
    assert resp.status_code == 200
    assert focused == ["UID-1"]


def test_focus_errors():
    focused = []
    c = focus_client(focused)
    assert c.post("/api/sessions/2/focus", headers=ORIGIN).status_code == 409  # not in iTerm2
    assert c.post("/api/sessions/3/focus", headers=ORIGIN).status_code == 404  # not running
    assert focus_client([], result=False).post("/api/sessions/1/focus", headers=ORIGIN).status_code == 502
    assert focused == []


def test_focus_rejects_other_origins():
    focused = []
    c = focus_client(focused)
    assert c.post("/api/sessions/1/focus").status_code == 403
    assert c.post("/api/sessions/1/focus", headers={"origin": "https://evil.example"}).status_code == 403
    assert focused == []


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_files_and_branches_include_subagents_and_other_repos(claude_home, live_proc, tmp_path):
    """Main session edits its own repo; a sub-agent edits a second repo and reads files."""
    repo_a, repo_b, plain = tmp_path / "repo-a", tmp_path / "repo-b", tmp_path / "plain"
    for d in (repo_a, repo_b, plain):
        (d / "src").mkdir(parents=True)
    git("init", "-b", "feature-a", cwd=repo_a)
    git("init", "-b", "fix-b", cwd=repo_b)
    (repo_b / "src" / "util.py").write_text("x = 1\n")

    sid = f"sess-{live_proc.pid}"
    register(claude_home, live_proc.pid, cwd=str(repo_a))
    proj = claude_home / "projects" / "-tmp-repo-a"
    (proj / sid / "subagents").mkdir(parents=True)

    def tool(name, **args):
        return {"type": "tool_use", "name": name, "input": args}

    def assistant(msg_id, cwd, branch, *blocks, sidechain=False):
        return {"type": "assistant", "cwd": str(cwd), "gitBranch": branch, "timestamp": "t", "isSidechain": sidechain,
                "message": {"id": msg_id, "model": "claude-opus-5-5", "content": list(blocks), "usage": {}}}

    def write(path, *entries):
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))

    write(proj / f"{sid}.jsonl",
          assistant("m1", repo_a, "feature-a", tool("Edit", file_path=f"{repo_a}/src/app.py")),
          assistant("m2", repo_a, "feature-a", tool("Write", file_path=f"{plain}/notes.md")))
    c = client()
    url = f"/api/sessions/{live_proc.pid}/detail"
    st = c.get(url).json()
    assert st["repos"] == {str(repo_a): {"branches": ["feature-a"], "removed": False}}  # plain/ isn't a repo

    # A sub-agent starts later, works in repo B (cwd elsewhere, so its branch comes from repo B's HEAD).
    write(proj / sid / "subagents" / "agent-x.jsonl",
          assistant("s1", repo_a, "feature-a",
                    tool("Read", file_path=f"{repo_b}/src/util.py"),
                    tool("Grep", pattern="x", path=f"{repo_b}/src/util.py"),
                    tool("Glob", pattern="*.py", path=str(repo_b)),
                    tool("Grep", pattern="y"),
                    tool("Edit", file_path=f"{repo_b}/src/util.py"),
                    tool("Edit", file_path=f"{repo_a}/src/app.py"), sidechain=True))
    st = c.get(url).json()
    assert st["repos"] == {str(repo_a): {"branches": ["feature-a"], "removed": False},
                           str(repo_b): {"branches": ["fix-b"], "removed": False}}
    assert st["files_edited"] == {f"{repo_a}/src/app.py": 2, f"{plain}/notes.md": 1, f"{repo_b}/src/util.py": 1}
    assert st["files_read"] == {f"{repo_b}/src/util.py": 2}  # Read + Grep on a file
    assert st["dirs_listed"] == {str(repo_b): 1, str(repo_a): 1}  # Glob + Grep without path (cwd)
    stats = c.get("/api/sessions").json()["sessions"][0]["stats"]
    assert (stats["requests"], stats["main"]["requests"]) == (3, 2)  # the sub-agent's request counts too
    assert c.get("/api/sessions/999999/detail").status_code == 404


def test_history_lists_closed_and_running_sessions(claude_home, live_proc):
    proj = claude_home / "projects" / "-w"
    proj.mkdir(parents=True)
    usage = {"input_tokens": 1, "output_tokens": 1}
    for sid, ts in ((f"sess-{live_proc.pid}", "2026-10-29T10:00:00Z"), ("closed-1", "2026-10-20T10:00:00Z")):
        (proj / f"{sid}.jsonl").write_text(json.dumps(
            {"type": "assistant", "cwd": "/w", "timestamp": ts,
             "message": {"id": sid, "model": "claude-opus-5-5", "content": [], "usage": usage}}) + "\n")
    register(claude_home, live_proc.pid)
    c = client()

    deadline = time.time() + 10
    while True:  # the first request starts a background scan
        body = c.get("/api/history").json()
        if body["total"] == 2 and not body["indexing"]["running"] or time.time() > deadline:
            break
        time.sleep(0.1)
    assert [(i["session_id"], i["live"]) for i in body["items"]] == [(f"sess-{live_proc.pid}", True), ("closed-1", False)]

    detail = c.get("/api/history/closed-1").json()
    assert detail["live"] is False
    assert detail["stats"]["requests"] == 1
    assert c.get("/api/history/unknown").status_code == 404
    assert c.get("/api/history?page=0&size=1000").json()["size"] == 200
    found = c.get("/api/history?q=closed").json()
    assert ([i["session_id"] for i in found["items"]], found["total"], found["all"]) == (["closed-1"], 1, 2)


def test_account_comes_from_the_processs_config_dir(claude_home, tmp_path):
    from harness_watch import registry

    assert registry.account_name(None) == "claude"
    assert registry.account_name("~/.claude-work") == "claude-work"
    assert registry.account_name("/Users/me/.claude/") == "claude"
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    work = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            env={**env, "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")})
    personal = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env)
    try:
        register(claude_home, work.pid)
        register(claude_home, personal.pid)
        accounts = {s["pid"]: s["account"] for s in client().get("/api/sessions").json()["sessions"]}
        assert accounts == {work.pid: "claude-work", personal.pid: "claude"}
    finally:
        for p in (work, personal):
            p.kill()
            p.wait()


def fork_client(forked, tmp_path, result=True):
    live = fake_session(1)
    live.start_cwd = str(tmp_path / "live dir")
    (tmp_path / "live dir").mkdir(parents=True)
    closed = {"s-old": {"start_cwd": str(tmp_path)}, "s-gone": {"start_cwd": str(tmp_path / "removed-worktree")}}

    class Index:
        def get(self, sid):
            return closed.get(sid)

    def fork_fn(cwd, sid, name, window):
        forked.append((cwd, sid, name, window))
        return result

    app = server.create_app(sessions_fn=lambda: [live], fork_fn=fork_fn, port=PORT, index_fn=Index)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_fork_types_resume_in_the_start_dir_next_to_the_session(tmp_path):
    forked = []
    c = fork_client(forked, tmp_path)
    assert c.post("/api/fork/s-1", headers=ORIGIN, json={"name": " fix\n  it "}).status_code == 200
    assert c.post("/api/fork/s-old", headers=ORIGIN).status_code == 200
    assert forked == [(f"{tmp_path}/live dir", "s-1", "fix it", 1),  # running: next to its own tab
                      (str(tmp_path), "s-old", None, None)]  # closed: the front window


def test_fork_script_runs_the_wrapper_then_a_shell(tmp_path):
    from harness_watch import iterm

    script = iterm.fork_script("/w/it's here", "abc-1", "ShipIn (fork)", "/opt/homebrew/bin/fish")
    assert script == ("cd '/w/it\\'s here'; and claude --resume abc-1 --fork-session --name 'ShipIn (fork)'\n"
                      "exec '/opt/homebrew/bin/fish' -l\n")
    assert "--name" not in iterm.fork_script("/w", "abc-1", None, "/bin/fish")


@pytest.mark.parametrize("shell, used", [("/bin/zsh", "/bin/zsh"), ("/bin/bash", "/bin/bash"), ("/bin/tcsh", "/bin/zsh"),
                                         ("/bin/csh", "/bin/zsh"), ("/no/such/fish", "/bin/zsh"), ("", "/bin/zsh")])
def test_fork_runs_in_the_login_shell_when_it_can(monkeypatch, shell, used):
    from harness_watch import iterm

    monkeypatch.setattr(iterm.pwd, "getpwuid", lambda uid: type("P", (), {"pw_shell": shell})())
    assert iterm.login_shell() == used


@pytest.mark.parametrize("shell", ["/bin/zsh", "/bin/bash", "/opt/homebrew/bin/fish"])
def test_fork_script_quotes_odd_names_in_each_shell(tmp_path, shell):
    """The script's first line run by the shell itself, with a stand-in `claude` that records its
    arguments: a quote in the dir and the name must reach claude intact."""
    from harness_watch import iterm

    if not os.path.exists(shell):
        pytest.skip(f"{shell} not installed")
    start = tmp_path / "it's \"here\" $HOME"
    start.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text('#!/bin/sh\npwd > "$OUT"\nprintf "%s\\n" "$@" >> "$OUT"\n')
    (bin_dir / "claude").chmod(0o755)
    first = iterm.fork_script(str(start), "abc-1", "it's \"a\" fork; rm", shell).splitlines()[0]
    out = tmp_path / "out"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "OUT": str(out), "HOME": str(tmp_path)}
    flags = ["--no-config"] if iterm.is_fish(shell) else ["-f"] if shell.endswith("zsh") else ["--norc"]
    subprocess.run([shell, *flags, "-c", first], env=env, check=True, timeout=30)
    assert out.read_text().splitlines() == [
        os.path.realpath(start), "--resume", "abc-1", "--fork-session", "--name", "it's \"a\" fork; rm"]


def test_fork_errors(tmp_path):
    forked = []
    c = fork_client(forked, tmp_path)
    assert c.post("/api/fork/s-gone", headers=ORIGIN).status_code == 409  # start dir removed
    assert c.post("/api/fork/nope", headers=ORIGIN).status_code == 404
    assert c.post("/api/fork/a;b", headers=ORIGIN).status_code == 400
    assert c.post("/api/fork/s-1").status_code == 403  # no Origin: not our page
    assert c.post("/api/fork/s-1", headers={"origin": "https://evil.example"}).status_code == 403
    assert forked == []
    assert fork_client([], tmp_path / "x", result=False).post("/api/fork/s-1", headers=ORIGIN).status_code == 502


@pytest.mark.skipif(not os.environ.get("ITERM_E2E"), reason="opens a real iTerm2 tab; run with ITERM_E2E=1")
def test_fork_applescript_opens_a_tab_in_iterm2():
    """The real AppleScript against the real iTerm2 (a variable named `launch`, an AppleScript
    command, once failed every fork at run time): a tab whose process exits at once."""
    from harness_watch import iterm

    assert iterm.open_tab("/usr/bin/true")


def test_install_agent_writes_a_launch_agent_that_runs_this_command(tmp_path):
    import plistlib

    from harness_watch import agent

    calls = []

    def launchctl(*args):  # the first bootstrap fails, as right after a bootout
        calls.append(args)
        failed = args[0] == "bootstrap" and [c[0] for c in calls].count("bootstrap") == 1
        return subprocess.CompletedProcess(args, 5 if failed else 0, "", "Input/output error" if failed else "")

    env = {"HARNESS_WATCH_HOSTNAME": "watch.test", "CLAUDE_CONFIG_DIR": "/c", "HOME": "/h", "AWS_SECRET": "x"}
    agent.install("/opt/hw/bin/harness-watch", env, tmp_path, launchctl)
    d = plistlib.loads(agent.plist_path(tmp_path).read_bytes())
    assert d["ProgramArguments"] == ["/opt/hw/bin/harness-watch", "serve"]
    assert d["EnvironmentVariables"] == {"PATH": agent.PATH, "CLAUDE_CONFIG_DIR": "/c", "HARNESS_WATCH_HOSTNAME": "watch.test"}
    assert d["RunAtLoad"] and d["KeepAlive"] and d["StandardOutPath"] == str(tmp_path / "Library/Logs/harness-watch.log")
    assert [c[0] for c in calls] == ["bootout", "bootstrap", "bootstrap"]  # retried once after bootout's I/O error

    assert "removed" in agent.uninstall(tmp_path, launchctl) and not agent.plist_path(tmp_path).exists()
    assert agent.uninstall(tmp_path, launchctl) == "no agent installed"


def test_no_other_site_may_frame_the_page_and_no_api_docs_are_served(claude_home):
    page = client().get("/")
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    for path in ("/docs", "/redoc", "/openapi.json"):  # /docs would load Swagger's scripts from a CDN
        assert client().get(path).status_code == 404, path


def test_fork_refuses_an_id_that_claude_would_read_as_an_option(tmp_path):
    c = fork_client([], tmp_path)
    assert c.post("/api/fork/-p", headers=ORIGIN).status_code == 400


KEY = "s3cret-key"


def keyed_client(**kw):
    return TestClient(server.create_app(port=PORT, page_key=KEY), base_url=f"http://127.0.0.1:{PORT}", **kw)


def test_without_the_key_another_account_gets_no_data_and_no_actions(claude_home):
    """Another account on the Mac reaches 127.0.0.1 too (curl sets any Host and Origin)."""
    other = keyed_client()
    for path in ("/api/sessions", "/api/stream", "/api/history", f"/api/sessions?key=wrong"):
        assert other.get(path).status_code == 401, path
    assert other.get("/api/sessions", headers={"x-harness-watch-key": "guess"}).status_code == 401
    assert other.post("/api/fork/s-1", headers=ORIGIN).status_code == 401
    assert other.get("/").status_code == 200  # the page itself holds no data: it says how to get in
    you = {"x-harness-watch-key": KEY}
    assert other.get("/api/sessions", headers=you).status_code == 200
    assert other.get(f"/api/sessions?key={KEY}").status_code == 200  # how the event stream sends it


def test_the_key_reaches_the_browser_by_a_post_and_lands_in_its_storage(claude_home):
    c = keyed_client()
    assert c.post("/__key", data={"key": "wrong"}, headers={"origin": "null"}).status_code == 401
    got = c.post("/__key", data={"key": KEY}, headers={"origin": "null"})  # a local file's form: Origin "null"
    assert got.status_code == 200 and "localStorage.setItem" in got.text and KEY in got.text
    csp = got.headers["content-security-policy"]
    nonce = csp.split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">' in got.text and "default-src 'none'" in csp
    assert got.headers["cache-control"] == "no-store" and got.headers["x-frame-options"] == "DENY"
    assert "set-cookie" not in got.headers  # cookies would reach every port of 127.0.0.1
    assert c.post("/api/fork/s-1", headers={"origin": "null"}).status_code == 403  # null is only for the key


def test_proof_answers_only_with_the_key(claude_home):
    from harness_watch import key

    assert keyed_client().get("/__proof?n=abc").text == key.proof(KEY, "abc") != key.proof("other", "abc")


def test_open_hands_the_key_over_only_to_a_server_that_proves_it_holds_it(monkeypatch, tmp_path):
    from harness_watch import key

    monkeypatch.setattr(key, "PATH", tmp_path / "data" / "key")
    real = key.load()
    honest = lambda url: key.proof(real, url.split("n=")[1])  # noqa: E731
    squatter = lambda url: "I don't know it"  # noqa: E731
    with pytest.raises(SystemExit, match="doesn't know"):
        server.open_page(None, show=True, fetch=squatter)
    assert not (tmp_path / "data" / "open.html").exists()

    opened = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, check: opened.append(cmd))
    said = server.open_page("watch.test", show=False, fetch=honest)
    page = tmp_path / "data" / "open.html"
    assert opened == [["open", str(page)]] and real not in " ".join(opened[0]) + said  # not in ps, not printed
    assert oct(page.stat().st_mode & 0o777) == "0o600"
    assert 'action="http://watch.test/__key"' in page.read_text() and real in page.read_text()

    def down(url):
        raise OSError("connection refused")
    with pytest.raises(SystemExit, match="isn't answering"):
        server.open_page(None, show=True, fetch=down)


def test_the_key_is_made_once_complete_and_readable_by_you_only(tmp_path):
    from harness_watch import key

    path = tmp_path / "data" / "key"
    first = key.load(path)
    assert len(first) >= 40 and key.load(path) == first
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert [p.name for p in path.parent.iterdir()] == ["key"]  # no temp file left


def test_open_never_follows_a_redirect_to_the_real_server(claude_home):
    """A server at --host could redirect the proof to the real one: _get must not follow it."""
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/__proof")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(OSError, match="302"):
            server._get(f"http://127.0.0.1:{srv.server_port}/__proof?n=x")
    finally:
        srv.shutdown()
