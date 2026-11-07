"""Sub-agents and background shells: launch, running state, completion, output, processes."""

import json
import subprocess
import sys
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from harness_watch import procs, server, session, transcript

T0 = "2026-10-30T10:00:00.000Z"
T1 = "2026-10-30T10:01:00.000Z"
T2 = "2026-10-30T10:02:00.000Z"
T3 = "2026-10-30T10:03:00.000Z"
EPOCH_T0 = datetime(2026, 10, 30, 10, tzinfo=UTC).timestamp()
USAGE = {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def write(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def call(msg_id, use_id, name, ts=T0, sidechain=False, **args):
    return {"type": "assistant", "timestamp": ts, "cwd": "/w", "isSidechain": sidechain,
            "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": USAGE,
                        "content": [{"type": "tool_use", "id": use_id, "name": name, "input": args}]}}


def answer(msg_id, ts, sidechain=False):
    return {"type": "assistant", "timestamp": ts, "cwd": "/w", "isSidechain": sidechain,
            "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": USAGE,
                        "content": [{"type": "text", "text": "done"}]}}


def result(use_id, content, ts=T0, sidechain=False, **tool_result):
    return {"type": "user", "timestamp": ts, "isSidechain": sidechain, "toolUseResult": tool_result,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use_id, "content": content}]}}


def notification(task_id, status, summary, ts):
    text = (f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>{status}</status>\n"
            f"<summary>{summary}</summary>\n</task-notification>")
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "content": text}


def launched(task_id, out):
    return f"Command running in background with ID: {task_id}. Output is being written to: {out}. You will be notified."


def read(path, sidechain=False):
    st = transcript.TranscriptStats(path, include_sidechain=sidechain)
    st.refresh()
    return st


def test_background_shell_lifecycle(tmp_path):
    p = tmp_path / "s.jsonl"
    out = "/private/tmp/claude-501/-w/s/tasks/b1.output"
    write(p, call("m1", "u1", "Bash", command="npm test", description="Run tests", run_in_background=True),
          result("u1", launched("b1", out), backgroundTaskId="b1"))
    st = read(p)
    assert st.tasks == {"b1": {"kind": "shell", "command": "npm test", "description": "Run tests",
                               "started": T0, "output_file": out}}
    shell = session.build(st, [], live_since=EPOCH_T0)["shells"][0]
    assert (shell["state"], shell["exit_code"]) == ("running", None)
    assert session.build(st, [], live_since=None)["shells"][0]["state"] == "ended"  # closed session
    assert session.build(st, [], live_since=EPOCH_T0 + 3600)["shells"][0]["state"] == "ended"  # resumed since

    write(p, notification("b1", "failed", 'Background command "Run tests" failed with exit code 2', T1))
    st.refresh()
    shell = session.build(st, [], live_since=EPOCH_T0)["shells"][0]
    assert (shell["state"], shell["exit_code"], shell["ended"]) == ("failed", 2, T1)


def test_taskstop_monitor_and_monitor_events(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p,
          call("m1", "u1", "Monitor", command="tail -f log | grep ERR", description="Watch errors"),
          result("u1", "Monitor started (task m9, expires in 30m)", taskId="m9", timeoutMs=1),
          call("m2", "u2", "Bash", command="sleep 100", run_in_background=True),
          result("u2", launched("b2", "/t/tasks/b2.output"), backgroundTaskId="b2"),
          {"type": "user", "timestamp": T1, "message": {"role": "user", "content":
           '<task-notification>\n<task-id>m9</task-id>\n<summary>Monitor event: "Watch errors"</summary>\n</task-notification>'}},
          call("m3", "u3", "TaskStop", task_id="b2"),
          result("u3", "stopped", ts=T2, message="Successfully stopped task: b2 (sleep 100)", task_id="b2"))
    shells = {s["id"]: s for s in session.build(read(p), [], live_since=EPOCH_T0)["shells"]}
    assert shells["m9"]["kind"] == "monitor"
    assert shells["m9"]["state"] == "running"  # an event is not a completion
    assert (shells["b2"]["state"], shells["b2"]["ended"]) == ("killed", T2)


def agent_file(tmp_path, agent_id="ax", meta=None):
    path = tmp_path / "s" / "subagents" / f"agent-{agent_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name(f"agent-{agent_id}.meta.json").write_text(json.dumps(meta or {
        "agentType": "general-purpose", "description": "Review diff", "spawnDepth": 1, "requestShape": "background"}))
    return path


def test_subagent_running_until_it_answers_or_is_notified(tmp_path):
    main = tmp_path / "s.jsonl"
    write(main, answer("m0", T0))
    path = agent_file(tmp_path)
    write(path, {"type": "user", "timestamp": T0, "isSidechain": True, "message": {"role": "user", "content": "Review it"}},
          call("a1", "t1", "Bash", ts=T1, sidechain=True, command="git diff"))

    def state(live_since=EPOCH_T0):
        sub = read(path, sidechain=True)
        built = session.build(read(main), [(sub, transcript.read_meta(path))], live_since)
        return built["subagents"][0]["state"], built

    assert state()[0] == "running"  # waiting on its tool call
    assert state(None)[0] == "interrupted"  # session closed mid-turn
    write(path, result("t1", "diff", ts=T1, sidechain=True))
    assert state()[0] == "running"  # result in: the model is thinking

    write(path, call("a2", "h1", "SubagentHandback", ts=T2, sidechain=True, report="ok"),
          result("h1", "delivered", ts=T2, sidechain=True))
    s, built = state()
    assert s == "done"
    agent = built["subagents"][0]
    assert (agent["id"], agent["type"], agent["description"], agent["background"]) == ("ax", "general-purpose", "Review diff", True)
    # Its tokens count in the session totals; the main session's own stay apart.
    assert built["requests"] == 3 and built["main"]["requests"] == 1
    assert built["tokens_total"] == 3 * built["main"]["tokens_total"]
    assert built["task_counts"] == {"agents": 1, "agents_running": 0, "shells": 0, "shells_running": 0}

    # Resumed with a message: running again, although an older notice says it finished.
    write(main, notification("ax", "completed", 'Agent "Review diff" finished', T2))
    write(path, {"type": "user", "timestamp": T3, "isSidechain": True, "message": {"role": "user", "content": "One more"}})
    assert state()[0] == "running"


def test_killed_subagent_stops_running(tmp_path):
    main = tmp_path / "s.jsonl"
    path = agent_file(tmp_path)
    write(path, call("a1", "t1", "Bash", ts=T1, sidechain=True, command="sleep 1000"))
    write(main, answer("m0", T0), notification("ax", "killed", "Agent stopped", T2))
    sub = read(path, sidechain=True)
    assert session.build(read(main), [(sub, {})], EPOCH_T0)["subagents"][0]["state"] == "killed"


def test_tool_command_unquotes_both_styles():
    wrap = "/bin/zsh -c source /u/.claude/shell-snapshots/s.sh 2>/dev/null || true && eval {} < /dev/null && pwd -P >| /tmp/c"
    assert procs.tool_command(wrap.format("'echo '\\''a b'\\'''")) == "echo 'a b'"
    assert procs.tool_command(wrap.format("'grep '\"'\"'x\\ty'\"'\"' f'")) == "grep 'x\\ty' f"
    assert procs.tool_command(wrap.format("'cut off")) is None
    assert procs.tool_command("node /opt/mcp/server.js eval 'x'") is None


def test_tail_strips_colours_and_progress_rewrites(tmp_path):
    out = tmp_path / "b.output"
    out.write_bytes(b"\x1b[32mok\x1b[0m line\r\ndownload 10%\rdownload 50%\rdownload 100%\nend\n")
    assert session.tail(str(out))["text"] == "ok line\ndownload 100%\nend\n"
    out.write_bytes(b"x" * 100 + b"\nlast line\n")
    t = session.tail(str(out), max_bytes=20)
    assert (t["text"], t["truncated"], t["size"]) == ("last line\n", True, 111)
    assert session.tail(str(tmp_path / "gone"))["exists"] is False


# End to end: a stand-in "claude" process with a Bash tool shell child, its transcript, the API.

@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_claude(tmp_path):
    """A process whose child is shaped like Claude Code's Bash tool shell running a two-line command."""
    wrapper = (f"source {tmp_path}/shell-snapshots/snap.sh 2>/dev/null || true && "
               "eval 'sleep 30\ntrue' < /dev/null && pwd -P >| /dev/null")
    code = f"import subprocess, time; subprocess.Popen(['/bin/zsh', '-c', {wrapper!r}]); time.sleep(60)"
    p = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.time() + 5
    while not [c for c in procs.children(p.pid, procs.snapshot())] and time.time() < deadline:
        time.sleep(0.05)
    yield p
    for child in procs.children(p.pid, procs.snapshot()):
        subprocess.run(["kill", str(child.pid)], check=False)
    p.kill()
    p.wait()


def test_shells_and_processes_end_to_end(claude_home, fake_claude, tmp_path):
    pid = fake_claude.pid
    sid = f"sess-{pid}"
    started_ms = int(time.time() * 1000) - 1000
    (claude_home / "sessions" / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "cwd": "/w", "name": "demo", "status": "busy", "startedAt": started_ms}))
    out = tmp_path / "tmp" / sid / "tasks" / "b7.output"
    out.parent.mkdir(parents=True)
    out.write_text("\x1b[1mstarting\x1b[0m\n")
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    transcript_path = claude_home / "projects" / "-w" / f"{sid}.jsonl"
    write(transcript_path,
          call("m1", "u1", "Bash", ts=now, command="sleep 30\ntrue", description="Wait", run_in_background=True),
          result("u1", launched("b7", out), ts=now, backgroundTaskId="b7"),
          call("m2", "u2", "Bash", ts=now, command="ls", run_in_background=True),
          result("u2", launched("b8", "/etc/passwd"), ts=now, backgroundTaskId="b8"))
    c = TestClient(server.create_app(port=8787), base_url="http://127.0.0.1:8787")

    s = c.get("/api/sessions").json()["sessions"][0]
    assert s["stats"]["task_counts"]["shells_running"] == 2
    assert "shells" not in s["stats"]  # the per-second push stays small
    [proc] = s["processes"]
    assert (proc["command"], proc["tool"], proc["shell_id"]) == ("sleep 30\ntrue", True, "b7")  # linked to its shell
    shells = {sh["id"]: sh for sh in c.get(f"/api/sessions/{pid}/detail").json()["shells"]}
    assert shells["b7"]["pid"] == proc["pid"]

    body = c.get(f"/api/output?session_id={sid}&task_id=b7").json()
    assert (body["state"], body["text"]) == ("running", "starting\n")
    assert c.get(f"/api/output?session_id={sid}&task_id=b8").status_code == 404  # not a task output path
    assert c.get(f"/api/output?session_id={sid}&task_id=nope").status_code == 404

    write(transcript_path, notification("b7", "completed", 'Background command "Wait" completed (exit code 0)', now))
    shells = {sh["id"]: sh for sh in c.get(f"/api/sessions/{pid}/detail").json()["shells"]}
    assert (shells["b7"]["state"], shells["b7"]["exit_code"]) == ("done", 0)
