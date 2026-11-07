import json
import subprocess

from harness_watch import iterm, procs, registry

NOW = 1_800_000_000.0


def proc(pid, ppid=1, tty="ttys001", elapsed=60, comm="claude"):
    return procs.Proc(pid, ppid, tty, elapsed, comm)


def entry(pid, status="busy", since=None, started_s=NOW - 60, **extra):
    return {
        "pid": pid,
        "sessionId": f"sess-{pid}",
        "cwd": f"/Users/me/p{pid}",
        "name": f"name-{pid}",
        "status": status,
        "statusUpdatedAt": since,
        "startedAt": int(started_s * 1000),
        **extra,
    }


def test_parse_etime():
    assert procs.parse_etime("05") == 5
    assert procs.parse_etime("01:05") == 65
    assert procs.parse_etime("02:01:05") == 7265
    assert procs.parse_etime("3-02:01:05") == 3 * 86400 + 7265


def test_parse_ps_keeps_spaces_in_comm_and_drops_no_tty():
    out = (
        "  731     1 ??       43:41 /Applications/iTerm.app/Contents/MacOS/iTerm2\n"
        "  860   731 ??       43:38 /Users/me/Library/Application Support/iTerm2/iTermServer-3.6.10\n"
        "12117   887 ttys000  23:31 claude\n"
        "garbage\n"
    )
    table = procs.parse_ps(out)
    assert table[860].comm.endswith("iTermServer-3.6.10")
    assert table[731].tty is None
    assert table[12117].tty == "ttys000"
    assert table[12117].elapsed_s == 23 * 60 + 31


def test_host_app_walks_parent_chain():
    table = {
        731: proc(731, 1, None, comm="/Applications/iTerm.app/Contents/MacOS/iTerm2"),
        860: proc(860, 731, None, comm="/Users/me/Library/Application Support/iTerm2/iTermServer"),
        887: proc(887, 860, comm="-fish"),
        12117: proc(12117, 887),
        500: proc(500, 1, comm="claude"),
    }
    assert procs.host_app(12117, table) == "iTerm"
    assert procs.host_app(500, table) is None


def test_iterm_parse_sessions():
    out = "32\t1\tUID-A\t/dev/ttys000\t✳ Report quote (claude)\nbad line\n502\t2\tUID-B\t/dev/ttys001\tname\twith tab\n"
    by_tty = iterm.parse_sessions(out)
    assert by_tty["ttys000"] == iterm.ItermSession(32, 1, "UID-A", "ttys000", "✳ Report quote (claude)")
    assert by_tty["ttys001"].name == "name\twith tab"


def test_iterm_is_running():
    assert iterm.is_running(["claude", "/Applications/iTerm.app/Contents/MacOS/iTerm2"])
    assert not iterm.is_running(["/Applications/iTerm.app/Contents/XPCServices/pidinfo.xpc/Contents/MacOS/pidinfo"])


def test_build_sessions_filters_dead_and_reused_pids():
    table = {1: proc(1), 2: proc(2, elapsed=10_000)}
    entries = [entry(1), entry(2), entry(3)]  # 2: pid reused (started long before entry), 3: dead
    sessions = registry.build_sessions(entries, table, {}, NOW)
    assert [s.pid for s in sessions] == [1]


def test_build_sessions_matches_iterm_by_tty():
    table = {1: proc(1, tty="ttys004"), 2: proc(2, tty="ttys009")}
    it = {"ttys004": iterm.ItermSession(7, 3, "UID", "ttys004", "title")}
    by_pid = {s.pid: s for s in registry.build_sessions([entry(1), entry(2)], table, it, NOW)}
    assert by_pid[1].to_json()["in_iterm"] is True
    assert by_pid[1].iterm["tab_index"] == 3
    assert by_pid[2].to_json()["in_iterm"] is False


def test_sort_idle_first_longest_waiting_on_top():
    table = {p: proc(p) for p in range(1, 5)}
    entries = [
        entry(1, "busy", since=400),
        entry(2, "idle", since=300),
        entry(3, "idle", since=100),
        entry(4, "busy", since=900),
    ]
    order = [s.pid for s in registry.build_sessions(entries, table, {}, NOW)]
    assert order == [3, 2, 4, 1]


def test_sort_waiting_then_idle_and_shell_then_busy_limited_last():
    table = {p: proc(p) for p in range(1, 7)}
    entries = [
        entry(1, "busy", since=400),
        entry(2, "shell", since=100),
        entry(3, "idle", since=300),
        entry(4, "waiting", since=500, waitingFor="input needed"),
        entry(5, "idle", since=50),  # longest idle, but its account is at its limit
        entry(6, "waiting", since=200),
    ]
    sessions = registry.build_sessions(entries, table, {}, NOW)
    for s in sessions:
        s.limit = {"kind": "session"} if s.pid == 5 else None
    order = [s.pid for s in registry.sort_sessions(sessions)]
    assert order == [6, 4, 2, 3, 1, 5]
    by_pid = {s.pid: s for s in sessions}
    assert by_pid[2].attention and not by_pid[1].attention
    assert by_pid[4].waiting_for == "input needed"


def test_read_registry_skips_bad_files(tmp_path):
    (tmp_path / "1.json").write_text(json.dumps(entry(1)))
    (tmp_path / "2.json").write_text("{not json")
    (tmp_path / "3.json").write_text(json.dumps({"pid": "x"}))
    (tmp_path / "1.abc.key").write_text("secret")
    assert [e["pid"] for e in registry.read_registry(tmp_path)] == [1]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_iterm_cache_refetches_only_when_needed():
    calls = []

    def fetch(running):
        calls.append(1)
        return {"ttys001": iterm.ItermSession(1, 1, "U", "ttys001", "n")}

    clock = FakeClock()
    cache = iterm.ItermCache(fetch, max_age_s=15, clock=clock)
    a = {(10, "ttys001")}
    cache.get(True, a)
    cache.get(True, a)
    assert len(calls) == 1  # same sessions, fresh: cached

    cache.get(True, a | {(11, "ttys005")})  # new session (here: not in iTerm2) -> refetch
    cache.get(True, a | {(11, "ttys005")})  # covered now, even though it isn't in iTerm2
    assert len(calls) == 2

    cache.get(True, {(12, "ttys001")})  # new pid on a reused tty -> refetch
    assert len(calls) == 3

    clock.t = 16
    cache.get(True, {(12, "ttys001")})  # stale -> refetch for tab titles
    assert len(calls) == 4

    assert cache.get(False, a) == {}  # iTerm2 quit
    cache.get(True, a)
    assert len(calls) == 5


def test_iterm_cache_waits_a_minute_after_a_lookup_timed_out():
    """A pending "may control iTerm2?" dialog: each lookup waits out its timeout holding the session
    list's lock, so the next one waits a minute instead of the next poll."""
    calls, answer = [], [iterm.ItermTimeout]

    def fetch(running):
        calls.append(1)
        if answer[0] is iterm.ItermTimeout:
            raise iterm.ItermTimeout
        return answer[0]

    clock = FakeClock()
    cache = iterm.ItermCache(fetch, max_age_s=15, clock=clock, backoff_s=60)
    assert cache.get(True, {(10, "ttys001")}) == {}
    clock.t = 30
    cache.get(True, {(11, "ttys002")})  # even a new session waits
    assert len(calls) == 1
    clock.t = 61
    answer[0] = {"ttys001": iterm.ItermSession(1, 1, "U", "ttys001", "n")}  # allowed meanwhile
    assert "ttys001" in cache.get(True, {(10, "ttys001")}) and len(calls) == 2


def test_flash_restores_the_color_the_pane_had_first(tmp_path, monkeypatch):
    """iTerm2 loses a unit per color read/write: re-reading before each flash would darken the pane."""
    calls = []
    color = {"now": "4273, 5020, 6015"}

    def run(cmd, **kw):
        calls.append(cmd[3:])
        if len(cmd) == 4:  # read
            return subprocess.CompletedProcess(cmd, 0, stdout=color["now"])
        color["now"] = f"{int(cmd[4]) - 1}, {int(cmd[5]) - 1}, {int(cmd[6]) - 1}"  # lossy write
        return subprocess.CompletedProcess(cmd, 0, stdout="ok")

    monkeypatch.setattr(iterm, "_COLORS", tmp_path / "colors.json")
    monkeypatch.setattr(iterm.subprocess, "run", run)
    iterm.flash("UID-A")
    iterm.flash("UID-A")
    assert calls == [["UID-A"], ["UID-A", "4273", "5020", "6015"], ["UID-A", "4273", "5020", "6015"]]
