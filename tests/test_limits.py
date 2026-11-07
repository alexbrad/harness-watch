"""Usage limits: read from the transcript, applied to every session on the same account."""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from harness_watch import limits, registry, server, transcript

TZ = ZoneInfo("Europe/Bucharest")


def hit_entry(ts: str, text: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "error": "rate_limit", "isApiErrorMessage": True,
            "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]}}


def response(ts: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "message": {"id": ts, "model": "claude-opus-5-5", "content": [],
                                                             "usage": {"input_tokens": 1, "output_tokens": 1}}}


def test_reset_times():
    at = "2026-10-30T09:27:16.728Z"  # 11:27 in Bucharest
    assert limits.resets_at("You've hit your 5-hour limit · resets 1pm (Europe/Bucharest)", at) == "2026-10-30T13:00:00+02:00"
    assert limits.resets_at("You've hit your session limit · resets 4:20am (Europe/Bucharest)", at) == "2026-10-31T04:20:00+02:00"
    assert limits.resets_at("You've hit your weekly limit · resets Nov 2 at 10pm (Europe/Bucharest)", at) == "2026-11-02T22:00:00+02:00"
    assert limits.resets_at("You've hit your session limit · resets soon", at) is None
    assert limits.kind("You've hit your weekly limit · resets 1pm") == "weekly"


def test_active_until_reset_or_a_later_response():
    now = datetime(2026, 10, 30, 10, tzinfo=UTC)
    hit = {"text": "You've hit your session limit · resets 1pm", "at": "2026-10-30T09:00:00Z", "resets": "2026-10-30T13:00:00+02:00"}
    assert limits.active([hit], [None], now)["kind"] == "session"
    assert limits.active([hit], ["2026-10-30T08:59:00Z"], now)  # a response before the hit
    assert limits.active([hit], ["2026-10-30T09:30:00Z"], now) is None  # a response after: lifted
    assert limits.active([hit], [], datetime(2026, 10, 30, 11, 1, tzinfo=UTC)) is None  # past 13:00 local


def test_transcript_records_hit_and_reset(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(hit_entry("2026-10-30T09:00:00Z", "You've hit your session limit · resets 1pm (Europe/Bucharest)")) + "\n")
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.limit["resets"] == "2026-10-30T13:00:00+02:00"
    assert st.last_response_at is None
    with p.open("a") as f:
        f.write(json.dumps({"type": "system", "subtype": "informational", "timestamp": "2026-10-30T11:00:00Z",
                            "content": "Usage limit reset · continuing automatically"}) + "\n")
    st.refresh()
    assert st.limit is None


def test_limit_marks_every_live_session_on_the_account(tmp_path, monkeypatch):
    """Session A hits the limit; B, on the same account, never saw it; C uses another account."""
    (tmp_path / "sessions").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    a, b = subprocess.Popen(sleeper, env=env), subprocess.Popen(sleeper, env=env)
    c = subprocess.Popen(sleeper, env={**env, "CLAUDE_CONFIG_DIR": str(tmp_path / ".claude-work")})
    try:
        now = datetime.now(UTC)
        reset = (now + timedelta(hours=2)).astimezone(TZ)
        text = f"You've hit your session limit · resets {reset.strftime('%-I:%M%p').lower()} (Europe/Bucharest)"
        proj = tmp_path / "projects" / "-w"
        proj.mkdir(parents=True)
        iso = lambda t: t.isoformat().replace("+00:00", "Z")
        for p, entries in ((a, [response(iso(now - timedelta(minutes=5))), hit_entry(iso(now - timedelta(minutes=1)), text)]),
                           (b, [response(iso(now - timedelta(minutes=30)))]),
                           (c, [response(iso(now - timedelta(minutes=30)))])):
            (tmp_path / "sessions" / f"{p.pid}.json").write_text(json.dumps(
                {"pid": p.pid, "sessionId": f"s-{p.pid}", "cwd": "/w", "status": "idle",
                 "statusUpdatedAt": int(time.time() * 1000), "startedAt": int(time.time() * 1000)}))
            (proj / f"s-{p.pid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))

        client = TestClient(server.create_app(port=8787), base_url="http://127.0.0.1:8787")
        sessions = client.get("/api/sessions").json()["sessions"]
        got = {s["pid"]: (s["account"], s["limit"] and s["limit"]["kind"]) for s in sessions}
        assert got == {a.pid: ("claude", "session"), b.pid: ("claude", "session"), c.pid: ("claude-work", None)}
        assert sessions[0]["pid"] == c.pid  # the limited ones sort last
        assert registry.account_name(str(tmp_path / ".claude-work")) == "claude-work"
    finally:
        for p in (a, b, c):
            p.kill()
            p.wait()
