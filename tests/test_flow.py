"""Flow: messages between a session and its sub-agents, read where they arrive, joined into lanes."""

import json

from fastapi.testclient import TestClient
from test_explorer import append, call, result

from harness_watch import flow, index, server


def user(text, ts, sidechain=False):
    return {"type": "user", "timestamp": ts, "isSidechain": sidechain, "message": {"role": "user", "content": text}}


def queued(text, ts):
    return {"type": "attachment", "timestamp": ts, "attachment": {"type": "queued_command", "prompt": text}}


def meta(path, **fields):
    path.with_name(f"{path.stem}.meta.json").write_text(json.dumps(fields))


HANDBACK = ('Another Claude session sent a message:\n<agent-message from="arev-1">\n[Subagent hand-back] The text '
            "below is the final report. The report follows:\n  ## REVIEW\n  2 findings.\n</agent-message>\n\nThis came "
            "from another Claude session")


def test_parse():
    teammates = ('<teammate-message teammate_id="team-lead" summary="Go">Do X</teammate-message>\n\n'
                 '<teammate-message teammate_id="rev" color="red">{"type":"idle_notification","from":"rev",'
                 '"idleReason":"available","summary":"[to lead] done","result":"All done."}</teammate-message>')
    assert flow.parse(teammates, in_subagent=False) == [
        {"kind": "message", "peer": "team-lead", "title": "Go", "text": "Do X"},
        {"kind": "idle", "peer": "rev", "title": "[to lead] done", "text": "All done."}]
    assert flow.parse(HANDBACK, in_subagent=False) == [
        {"kind": "handback", "peer": "arev-1", "title": "final report", "text": "## REVIEW\n2 findings."}]
    note = ("<task-notification>\n<task-id>arev-1</task-id>\n<status>completed</status>\n"
            "<summary>Agent \"Review\" finished</summary>\n<result>ok</result>\n</task-notification>")
    assert flow.parse(note, False)[0] == {"kind": "notification", "peer": "arev-1",
                                          "title": 'completed: Agent "Review" finished', "text": "ok"}
    assert flow.parse("<task-notification><task-id>m1</task-id><event>x</event></task-notification>", False) == []
    assert flow.parse("Review the diff", in_subagent=True)[0]["kind"] == "prompt"
    assert flow.parse("Review the diff", in_subagent=False) == []  # a person's prompt to the main session
    for harness in ("[SYSTEM NOTIFICATION - NOT USER INPUT] …", "[handback-send-enforce] …",
                    "Base directory for this skill: /x", "<system-reminder>x</system-reminder>"):
        assert flow.parse(harness, in_subagent=True) == []
    relayed = flow.parse("The coordinator sent a message while you were working: Stop now", True)
    assert relayed == [{"kind": "message", "peer": None, "title": None, "text": "Stop now"}]
    assert flow.result("found 3", {"agentId": "a1", "status": "completed"})["peer"] == "a1"
    assert flow.result("launched", {"agentId": "a1", "status": "async_launched"}) is None


def test_segments_break_after_silence():
    stamps = ["2026-10-30T10:00:00Z", "2026-10-30T10:04:00Z", "2026-10-30T10:30:00Z", None]
    assert flow.segments(stamps) == [["2026-10-30T10:00:00Z", "2026-10-30T10:04:00Z"],
                                     ["2026-10-30T10:30:00Z", "2026-10-30T10:30:00Z"]]


def session(tmp_path):
    """Main launches a background reviewer (which starts a nested checker) and a teammate;
    they talk to each other and to the main session in every way messages travel."""
    p = tmp_path / "projects"
    subs = p / "-w" / "s1" / "subagents"
    append(p / "-w" / "s1.jsonl",
           user("Review my change", "2026-10-30T10:00:00Z"),  # the person: not a flow message
           call("m1", "u1", "Agent", "2026-10-30T10:00:10Z", description="Review", subagent_type="general-purpose"),
           result("u1", "launched", "2026-10-30T10:00:11Z", status="async_launched", agentId="arev-1"),
           call("m2", "u2", "Agent", "2026-10-30T10:00:20Z", description="Docs"),
           result("u2", "spawned", "2026-10-30T10:00:21Z", status="teammate_spawned", agentId="adocs-2"),
           call("m3", "u3", "SendMessage", "2026-10-30T10:02:00Z", to="docs", message="Also check the README"),
           user(HANDBACK, "2026-10-30T10:05:00Z"),
           queued('<teammate-message teammate_id="docs">README fixed</teammate-message>', "2026-10-30T10:06:00Z"),
           user('<teammate-message teammate_id="stranger">hi from elsewhere</teammate-message>', "2026-10-30T10:07:00Z"))
    rev = subs / "agent-arev-1.jsonl"
    append(rev,
           user("Review the diff in /w", "2026-10-30T10:00:12Z", sidechain=True),
           call("r1", "v1", "Read", "2026-10-30T10:00:30Z", sidechain=True, file_path="/w/app.py"),
           result("v1", "code", "2026-10-30T10:00:31Z", sidechain=True),
           call("r2", "v2", "Agent", "2026-10-30T10:01:00Z", sidechain=True, description="Check tests"),
           result("v2", "tests pass", "2026-10-30T10:03:00Z", sidechain=True, status="completed", agentId="achk-3"),
           user('<teammate-message teammate_id="docs">Is app.py done?</teammate-message>', "2026-10-30T10:03:30Z",
                sidechain=True))
    meta(rev, agentType="general-purpose", description="Review", spawnDepth=1)
    docs = subs / "agent-adocs-2.jsonl"
    append(docs,
           user('<teammate-message teammate_id="team-lead" summary="Docs">Fix the docs</teammate-message>',
                "2026-10-30T10:00:22Z", sidechain=True),
           user('<teammate-message teammate_id="team-lead">Also check the README</teammate-message>',
                "2026-10-30T10:02:01Z", sidechain=True),
           call("d1", "w1", "Edit", "2026-10-30T10:02:30Z", sidechain=True, file_path="/w/README.md"))
    meta(docs, agentType="docs", name="docs", spawnDepth=0)
    chk = subs / "agent-achk-3.jsonl"
    append(chk,
           user("Run the tests", "2026-10-30T10:01:01Z", sidechain=True),
           call("c1", "x1", "Bash", "2026-10-30T10:01:30Z", sidechain=True, command="uv run pytest"))
    meta(chk, agentType="general-purpose", description="Check tests", parentAgentId="arev-1", spawnDepth=1)
    return p


def test_flow_lanes_and_messages(tmp_path):
    idx = index.SessionIndex(tmp_path / "i.sqlite3", session(tmp_path))
    idx.scan()
    f = idx.flow("s1")
    assert [(lane["id"], lane["depth"]) for lane in f["lanes"]] == [
        ("main", 0), ("arev-1", 1), ("achk-3", 2), ("adocs-2", 1), ("@stranger", 0)]
    pairs = [(m["from"], m["to"], m["kind"]) for m in f["messages"]]
    assert pairs == [
        ("main", "arev-1", "prompt"), ("main", "adocs-2", "message"), ("arev-1", "achk-3", "prompt"),
        ("main", "adocs-2", "message"), ("achk-3", "arev-1", "result"), ("adocs-2", "arev-1", "message"),
        ("arev-1", "main", "handback"), ("adocs-2", "main", "message"), ("@stranger", "main", "message")]
    rev = f["lanes"][1]
    assert rev["name"] is None and rev["description"] == "Review" and rev["segments"]

    between = idx.flow_messages("s1", "adocs-2", "main")
    assert [(m["from"], m["text"]) for m in between] == [
        ("main", "Fix the docs"), ("main", "Also check the README"), ("adocs-2", "README fixed")]
    assert idx.flow_messages("s1", "main", "arev-1")[1]["text"] == "## REVIEW\n2 findings."


def test_flow_endpoints_and_agent_filter(tmp_path, monkeypatch):
    p = session(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(p.parent))
    (p.parent / "sessions").mkdir()
    c = TestClient(server.create_app(port=8787), base_url="http://127.0.0.1:8787")
    assert len(c.get("/api/flow/s1").json()["messages"]) == 9
    body = c.get("/api/flow/s1/between?a=arev-1&b=achk-3").json()
    assert [m["text"] for m in body["items"]] == ["Run the tests", "tests pass"]

    assert c.get("/api/tools/s1/summary?agent=arev-1").json()["total"] == 2  # Read + Agent
    assert c.get("/api/tools/s1/summary?agent=main").json()["total"] == 3
    items = c.get("/api/tools/s1/events?agent=adocs-2").json()["items"]
    assert [e["tool"] for e in items] == ["Edit"]
