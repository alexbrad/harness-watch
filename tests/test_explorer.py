"""Tool-use explorer: calls extracted into the index, grouped, filtered, paged, kept current."""

import json

import pytest
from fastapi.testclient import TestClient

from harness_watch import events, index, server


def test_program_skips_cd_assignments_and_wrappers():
    assert events.program("cd /repo && git status") == "git"
    assert events.program("FOO=1 env -i nohup uv run pytest -q") == "uv"
    assert events.program("caffeinate -i -t 300 python3 x.py") == "python3"
    assert events.program("/opt/homebrew/bin/gh pr view 3 | head") == "gh"
    assert events.program("cd /x") == "cd"
    assert events.program("for f in a b; do wc -l $f; done") == "wc"
    assert events.program("while true; do gh pr checks 3; sleep 30; done") == "gh"
    assert events.program("if [ -f x ]; then cat x; fi") == "cat"
    assert events.program("echo 'unbalanced") == "echo"


def test_classify():
    assert events.classify("WebFetch", {"url": "https://docs.python.org/3/x"}) == ("web", "docs.python.org", "https://docs.python.org/3/x")
    assert events.classify("WebSearch", {"query": "sqlite fts"}) == ("web", "search", "sqlite fts")
    assert events.classify("Grep", {"pattern": "foo", "path": "/w"}) == ("files", "Grep", "foo  in /w")
    assert events.classify("mcp__claude-in-chrome__navigate", {"url": "https://x.org"}) == ("mcp", "claude-in-chrome", "navigate https://x.org")
    assert events.classify("Agent", {"description": "Review diff", "subagent_type": "Explore"}) == ("agents", "Explore", "Review diff")
    assert events.classify("Skill", {"skill": "feature-start"}) == ("other", "Skill", "feature-start")
    long = events.use_event({"id": "t", "name": "Write", "input": {"file_path": "/a", "content": "x" * 50_000}}, "ts", "/w")
    assert len(long["detail"]) < events.DETAIL_CAP + 100


def call(msg_id, use_id, name, ts, sidechain=False, **args):
    return {"type": "assistant", "timestamp": ts, "cwd": "/w", "isSidechain": sidechain,
            "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": {"input_tokens": 1},
                        "content": [{"type": "tool_use", "id": use_id, "name": name, "input": args}]}}


def result(use_id, text, ts, is_error=False, sidechain=False, **tool_result):
    return {"type": "user", "timestamp": ts, "isSidechain": sidechain, "toolUseResult": tool_result,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": use_id, "content": text, "is_error": is_error}]}}


def append(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def projects(tmp_path):
    p = tmp_path / "projects"
    main = p / "-w" / "s1.jsonl"
    append(main,
           call("m1", "u1", "Bash", "2026-10-30T10:00:00Z", command="cd /w && git status"),
           result("u1", "clean", "2026-10-30T10:00:01Z", stdout="clean"),
           call("m2", "u2", "Bash", "2026-10-30T10:01:00Z", command="uv run pytest"),
           result("u2", "Exit code 1\nFAILED", "2026-10-30T10:01:30Z", is_error=True),
           call("m3", "u3", "WebFetch", "2026-10-30T10:02:00Z", url="https://docs.python.org/3/library/re.html"),
           call("m4", "u4", "Agent", "2026-10-30T10:03:00Z", description="Review the diff", subagent_type="Explore"),
           result("u4", "launched", "2026-10-30T10:03:01Z", status="async_launched", agentId="ax"))
    append(p / "-w" / "s1" / "subagents" / "agent-ax.jsonl",
           call("a1", "v1", "Read", "2026-10-30T10:04:00Z", sidechain=True, file_path="/w/app.py"),
           result("v1", "code", "2026-10-30T10:04:01Z", sidechain=True))
    return p


def test_index_groups_and_lists_calls(tmp_path, projects):
    idx = index.SessionIndex(tmp_path / "i.sqlite3", projects)
    idx.scan()
    summary = idx.tool_summary("s1")
    assert summary["total"] == 5
    assert summary["categories"] == {"commands": 2, "web": 1, "agents": 1, "files": 1}
    groups = {(g["category"], g["group"]): (g["count"], g["errors"]) for g in summary["groups"]}
    assert groups[("commands", "git")] == (1, 0) and groups[("commands", "uv")] == (1, 1)
    assert groups[("web", "docs.python.org")] == (1, 0)

    total, items = idx.tool_events("s1")
    assert total == 5
    assert [e["tool"] for e in items] == ["Read", "Agent", "WebFetch", "Bash", "Bash"]  # newest first
    read, agent, fetch, failed, ok = items
    assert read["agent_id"] == "ax" and read["status"] == "ok"  # made by the sub-agent
    assert (agent["status"], agent["agent_ref"]) == ("background", "ax")
    assert fetch["status"] == "pending"  # no result yet
    assert (failed["status"], failed["exit_code"], failed["preview"]) == ("error", 1, "Exit code 1\nFAILED")
    assert (ok["status"], ok["exit_code"]) == ("ok", 0)

    assert [e["summary"] for e in idx.tool_events("s1", category="commands", group="uv")[1]] == ["uv run pytest"]
    assert idx.tool_events("s1", query="PYTHON.org")[0] == 1  # case-folded, matches the URL
    assert idx.tool_summary("s1", query="git")["total"] == 1
    total, page2 = idx.tool_events("s1", offset=3, limit=3)
    assert (total, [e["id"] for e in page2]) == (5, ["u2", "u1"])


def test_refresh_session_reads_new_calls_and_rewrites(tmp_path, projects):
    idx = index.SessionIndex(tmp_path / "i.sqlite3", projects)
    idx.scan()
    main = projects / "-w" / "s1.jsonl"
    append(main, result("u3", "<html>", "2026-10-30T10:05:00Z"),
           call("m5", "u5", "mcp__chrome__navigate", "2026-10-30T10:06:00Z", url="https://x.org"))
    assert idx.refresh_session("s1")
    assert idx.progress["running"] is False and idx.progress["files_total"] == 2  # still the full scan's
    _, items = idx.tool_events("s1")
    assert (items[0]["tool"], items[0]["category"], items[0]["grp"]) == ("mcp__chrome__navigate", "mcp", "chrome")
    assert next(e for e in items if e["id"] == "u3")["status"] == "ok"

    main.write_text(json.dumps(call("n1", "w1", "Bash", "2026-10-30T11:00:00Z", command="ls")) + "\n")  # rewritten, shorter
    idx.refresh_session("s1")
    assert [e["id"] for e in idx.tool_events("s1")[1]] == ["w1", "v1"]  # the old main calls are gone

    (projects / "-w" / "s1" / "subagents" / "agent-ax.jsonl").unlink()
    idx.scan()
    assert [e["id"] for e in idx.tool_events("s1")[1]] == ["w1"]
    assert not idx.refresh_session("../etc")


def test_tools_endpoints(tmp_path, projects, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(projects.parent))
    (projects.parent / "sessions").mkdir()
    c = TestClient(server.create_app(port=8787), base_url="http://127.0.0.1:8787")
    summary = c.get("/api/tools/s1/summary").json()  # indexes the session on demand
    assert summary["total"] == 5
    body = c.get("/api/tools/s1/events?category=commands&limit=1").json()
    assert (body["total"], [e["summary"] for e in body["items"]]) == (2, ["uv run pytest"])
    append(projects / "-w" / "s1.jsonl", call("m9", "u9", "WebSearch", "2026-10-30T12:00:00Z", query="sqlite"))
    assert c.get("/api/tools/s1/summary?q=sqlite").json()["categories"] == {"web": 1}
