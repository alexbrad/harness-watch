"""Plan usage: the status line command records each account's limits, the dashboard shows them."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from harness_watch import server, statusline, usage

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.fromisoformat("2026-11-04T18:00:00+00:00").timestamp()  # before both resets in the fixture
UPDATE = json.loads((FIXTURES / "statusline.json").read_text())


def run_statusline(tmp_path, stdin, *wrapped, config_dir=None):
    env = {"PATH": "/usr/bin:/bin", "HARNESS_WATCH_RATE_LIMITS": str(tmp_path / "limits"), "HOME": str(tmp_path)}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return subprocess.run([sys.executable, "-m", "harness_watch.statusline", *wrapped],
                          input=stdin, capture_output=True, env=env, timeout=30)


def test_status_line_to_page_end_to_end(tmp_path, monkeypatch):
    """Claude Code pipes an update to the command; the page's snapshot then carries the bars."""
    work = tmp_path / ".claude-work"
    work.mkdir()
    (work / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "work@example.com"}}))
    show = f"{sys.executable} -c \"import json,sys; print(json.load(sys.stdin)['model']['display_name'])\""
    r = run_statusline(tmp_path, json.dumps(UPDATE).encode(), show, config_dir=str(work))
    assert r.returncode == 0 and r.stdout.decode().strip() == "Opus 5.5"  # your own status line still shows

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    feed = usage.StatuslineFeed(tmp_path / "limits", clock=lambda: NOW)
    app = server.create_app(port=8787, usage_fn=feed.current)
    [a] = TestClient(app, base_url="http://127.0.0.1:8787").get("/api/sessions").json()["usage"]
    assert (a["account"], a["email"], a["error"]) == ("claude-work", "work@example.com", None)
    assert [(r["label"], r["percent"], r["severity"], r["resets_at"]) for r in a["limits"]] == [
        ("Current session", 23.5, "normal", "2026-11-05T00:00:00+00:00"),
        ("Weekly · all models", 81.2, "warning", "2026-11-10T00:00:00+00:00")]


def test_an_update_without_limits_keeps_the_last_reading(tmp_path):
    run_statusline(tmp_path, json.dumps(UPDATE).encode())
    before = (tmp_path / "limits" / "claude.json").read_text()
    no_limits = {k: v for k, v in UPDATE.items() if k != "rate_limits"}  # API key, Team, or no response yet
    assert run_statusline(tmp_path, json.dumps(no_limits).encode()).returncode == 0
    assert (tmp_path / "limits" / "claude.json").read_text() == before


def test_bad_input_never_fails_the_status_line(tmp_path):
    r = run_statusline(tmp_path, b"not json", "echo still here")
    assert r.returncode == 0 and r.stdout == b"still here\n"
    assert not (tmp_path / "limits").exists()
    assert run_statusline(tmp_path, b"{}").stdout == b""  # nothing wrapped: prints nothing


def test_the_wrapped_status_lines_exit_code_is_kept(tmp_path):
    assert run_statusline(tmp_path, b"{}", "sh", "-c", "echo x; exit 3").returncode == 3


def test_accounts_are_named_after_their_config_dir(tmp_path):
    out = tmp_path / "limits"
    assert statusline.record(UPDATE, None, out, NOW) and statusline.record(UPDATE, "~/.claude-work", out, NOW)
    assert sorted(p.name for p in out.glob("*.json")) == ["claude-work.json", "claude.json"]
    assert [a["account"] for a in usage.StatuslineFeed(out, email_fn=lambda d: None, clock=lambda: NOW).current()] == [
        "claude", "claude-work"]


def test_a_window_past_its_reset_starts_again_at_zero():
    after_5h = datetime.fromisoformat("2026-11-05T00:00:01+00:00").timestamp()
    got = usage.rows(UPDATE["rate_limits"], after_5h)
    assert [(r["percent"], r["resets_at"]) for r in got] == [(0, None), (81.2, "2026-11-10T00:00:00+00:00")]
    assert [usage.severity(p) for p in (0, 74.9, 75, 99, 100)] == ["normal", "normal", "warning", "warning", "critical"]


def test_unreadable_files_are_skipped(tmp_path):
    (tmp_path / "half.json").write_text("{")
    (tmp_path / "odd.json").write_text(json.dumps({"rate_limits": {}}))  # no "at"
    assert usage.StatuslineFeed(tmp_path, email_fn=lambda d: None).current() == []


def test_email_is_read_at_most_once_a_minute(tmp_path):
    statusline.record(UPDATE, None, tmp_path, NOW)
    calls, now = [], [NOW]
    feed = usage.StatuslineFeed(tmp_path, email_fn=lambda d: calls.append(d) or "me@example.com", clock=lambda: now[0])
    feed.current()
    now[0] += 30
    feed.current()
    assert calls == [None]
    now[0] += usage.EMAIL_TTL_S
    feed.current()
    assert calls == [None, None]


def test_another_source_replaces_the_status_lines(tmp_path, monkeypatch):
    plugin = tmp_path / "source.py"
    plugin.write_text("def source():\n    return lambda: [{'account': 'mine', 'limits': []}]\n")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(plugin))
    assert usage.load_source()() == [{"account": "mine", "limits": []}]


def test_a_broken_source_falls_back_to_the_status_lines(tmp_path, monkeypatch, caplog):
    plugin = tmp_path / "source.py"
    plugin.write_text("raise ImportError('nope')\n")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(plugin))
    monkeypatch.setenv("HARNESS_WATCH_RATE_LIMITS", str(tmp_path / "limits"))
    assert usage.load_source()() == []
    assert "failed to load" in caplog.text


def test_an_older_sessions_snapshot_never_replaces_a_newer_reading(tmp_path):
    """Two sessions on one account: each reports the limits it last saw, possibly older ones."""
    fresh = {"five_hour": {"used_percentage": 40, "resets_at": 1793836800},
             "seven_day": {"used_percentage": 81.2, "resets_at": 1794268800}}
    stale = {"five_hour": {"used_percentage": 30, "resets_at": 1793836800},  # same window, before more use
             "seven_day": {"used_percentage": 70, "resets_at": 1794268800}}
    statusline.record({"rate_limits": fresh}, None, tmp_path, NOW)
    statusline.record({"rate_limits": stale}, None, tmp_path, NOW + 1)
    [a] = usage.StatuslineFeed(tmp_path, email_fn=lambda d: None, clock=lambda: NOW).current()
    assert [r["percent"] for r in a["limits"]] == [40, 81.2]

    # The 5-hour window reset and a session reports the new one (no weekly yet): that one replaces it.
    statusline.record({"rate_limits": {"five_hour": {"used_percentage": 2, "resets_at": 1793854800}}}, None, tmp_path, NOW + 2)
    [a] = usage.StatuslineFeed(tmp_path, email_fn=lambda d: None, clock=lambda: NOW).current()
    assert [r["percent"] for r in a["limits"]] == [2, 81.2]


def test_files_of_another_shape_are_skipped_not_a_broken_page(tmp_path):
    good = {"account": "claude", "rate_limits": UPDATE["rate_limits"], "at": NOW}
    for name, data in {"a": {"rate_limits": None, "at": 1}, "b": {**good, "at": 1e20},
                       "c": {**good, "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 1.8e15}}},
                       "d": [1, 2], "claude": good}.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    assert [a["account"] for a in usage.StatuslineFeed(tmp_path, email_fn=lambda d: None, clock=lambda: NOW).current()] == ["claude"]


def test_a_source_may_use_dataclasses(tmp_path, monkeypatch):
    plugin = tmp_path / "source.py"
    plugin.write_text("from __future__ import annotations\nfrom dataclasses import dataclass\n\n"
                      "@dataclass\nclass Feed:\n    name: str\n\n    def current(self) -> list:\n        return [self.name]\n\n"
                      "def source():\n    return Feed('ok').current\n")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(plugin))
    assert usage.load_source()() == ["ok"]


def test_a_source_file_others_can_change_is_refused(tmp_path, monkeypatch, caplog):
    plugin = tmp_path / "source.py"
    plugin.write_text("def source():\n    return lambda: ['ran']\n")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(plugin))
    monkeypatch.setenv("HARNESS_WATCH_RATE_LIMITS", str(tmp_path / "limits"))
    tmp_path.chmod(0o755)
    plugin.chmod(0o664)  # group-writable
    assert usage.load_source()() == [] and "writable by you only" in caplog.text
    plugin.chmod(0o644)
    tmp_path.chmod(0o777)  # a folder others can write: they could swap the file
    assert usage.load_source()() == []
    tmp_path.chmod(0o755)
    assert usage.load_source()() == ["ran"]


def test_the_status_lines_files_are_yours_only(tmp_path):
    out = tmp_path / "limits"
    statusline.record(UPDATE, None, out, NOW)
    assert {oct(p.stat().st_mode & 0o777) for p in [out, *out.iterdir()]} == {"0o700", "0o600"}


def test_a_symlink_is_judged_by_its_target(tmp_path, monkeypatch):
    shared, mine = tmp_path / "shared", tmp_path / "mine"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    mine.mkdir(mode=0o700)
    (shared / "source.py").write_text("def source():\n    return lambda: ['ran']\n")
    (mine / "source.py").symlink_to(shared / "source.py")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(mine / "source.py"))
    monkeypatch.setenv("HARNESS_WATCH_RATE_LIMITS", str(tmp_path / "limits"))
    assert usage.load_source()() == []


def test_a_folder_others_can_rename_anywhere_above_refuses_the_source(tmp_path, monkeypatch):
    open_dir, private = tmp_path / "open", tmp_path / "open" / "private"
    open_dir.mkdir()
    private.mkdir(mode=0o700)
    (private / "source.py").write_text("def source():\n    return lambda: ['ran']\n")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(private / "source.py"))
    monkeypatch.setenv("HARNESS_WATCH_RATE_LIMITS", str(tmp_path / "limits"))
    open_dir.chmod(0o777)  # another user could rename "private" and put their own in its place
    assert usage.load_source()() == []
    open_dir.chmod(0o1777)  # sticky, like /tmp: they can't
    assert usage.load_source()() == ["ran"]
    open_dir.chmod(0o755)


def test_a_source_directly_in_a_shared_sticky_folder_is_refused(tmp_path, monkeypatch):
    shared = tmp_path / "tmp"
    shared.mkdir()
    shared.chmod(0o1777)  # like /tmp: others could plant __pycache__/source.*.pyc beside it
    (shared / "source.py").write_text("def source():\n    return lambda: ['ran']\n")
    monkeypatch.setenv("HARNESS_WATCH_USAGE_SOURCE", str(shared / "source.py"))
    monkeypatch.setenv("HARNESS_WATCH_RATE_LIMITS", str(tmp_path / "limits"))
    assert usage.load_source()() == []
    shared.chmod(0o755)
