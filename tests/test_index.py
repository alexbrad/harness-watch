import json
import os
import sqlite3
import time

import pytest

from harness_watch import index, session, transcript

U = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0, "output_tokens": 5}


def entry(msg_id, ts, cwd="/w", blocks=(), sidechain=False):
    return {"type": "assistant", "cwd": cwd, "timestamp": ts, "isSidechain": sidechain,
            "message": {"id": msg_id, "model": "claude-opus-5-5", "content": list(blocks), "usage": U}}


def append(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def projects(tmp_path):
    p = tmp_path / "projects"
    append(p / "-w" / "s1.jsonl", {"type": "ai-title", "aiTitle": "First"}, entry("a", "2026-10-01T10:00:00Z"))
    append(p / "-w" / "s2.jsonl", entry("b", "2026-10-02T10:00:00Z"))
    edit = {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/agent.txt"}}
    append(p / "-w" / "s2" / "subagents" / "agent-x.jsonl", entry("c", "2026-10-02T10:05:00Z", blocks=[edit], sidechain=True))
    return p


def new_index(tmp_path, projects):
    return index.SessionIndex(tmp_path / "db" / "index.sqlite3", projects)


def test_scan_lists_sessions_newest_first_with_subagent_activity(tmp_path, projects):
    idx = new_index(tmp_path, projects)
    assert idx.scan() == 2
    total, items = idx.page(1, 50)
    assert total == 2
    assert [i["session_id"] for i in items] == ["s2", "s1"]
    assert items[1]["title"] == "First"
    s2 = idx.get("s2")
    assert s2["activity"]["files_edited"] == {"/w/agent.txt": 1}
    assert "files_edited" not in s2["stats"]
    assert s2["stats"]["requests"] == 2  # incl. the sub-agent's
    assert s2["stats"]["main"]["requests"] == 1
    assert [a["id"] for a in s2["activity"]["subagents"]] == ["x"]
    assert items[0]["tokens_total"] == s2["stats"]["tokens_total"] == 2 * s2["stats"]["main"]["tokens_total"]
    assert idx.get("nope") is None


def test_restart_rereads_nothing_unchanged(tmp_path, projects):
    new_index(tmp_path, projects).scan()
    again = new_index(tmp_path, projects)  # a server restart
    assert again.scan() == 0
    assert again.progress["files_total"] == 0
    assert again.page(1, 50)[0] == 2


def test_appended_transcript_is_read_from_where_it_stopped(tmp_path, projects):
    idx = new_index(tmp_path, projects)
    idx.scan()
    before = os.path.getsize(projects / "-w" / "s1.jsonl")
    append(projects / "-w" / "s1.jsonl", entry("d", "2026-10-03T10:00:00Z"))
    assert idx.scan() == 1
    assert idx.progress["files_total"] == 1
    assert idx.progress["bytes_done"] == os.path.getsize(projects / "-w" / "s1.jsonl") - before
    assert idx.get("s1")["stats"]["requests"] == 2
    assert idx.page(1, 50)[1][0]["session_id"] == "s1"  # now the most recent


def test_deleted_transcripts_drop_out(tmp_path, projects):
    idx = new_index(tmp_path, projects)
    idx.scan()
    (projects / "-w" / "s1.jsonl").unlink()
    (projects / "-w" / "s2" / "subagents" / "agent-x.jsonl").unlink()
    idx.scan()
    assert [i["session_id"] for i in idx.page(1, 50)[1]] == ["s2"]
    assert idx.get("s2")["activity"]["files_edited"] == {}


def test_pagination(tmp_path, projects):
    for n in range(5):
        append(projects / "-w" / f"p{n}.jsonl", entry(f"m{n}", f"2026-09-0{n + 1}T00:00:00Z"))
    idx = new_index(tmp_path, projects)
    idx.scan()
    total, page2 = idx.page(2, 3)
    assert total == 7
    assert [i["session_id"] for i in page2] == ["p3", "p2", "p1"]  # page 1: s2, s1, p4


def test_schema_change_rebuilds(tmp_path, projects):
    idx = new_index(tmp_path, projects)
    idx.scan()
    with sqlite3.connect(idx.db_path) as db:
        db.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
    rebuilt = new_index(tmp_path, projects)
    assert rebuilt.page(1, 50)[0] == 0
    rebuilt.scan()
    assert rebuilt.progress["files_total"] == 3


def test_live_store_resumes_from_the_index(tmp_path, projects):
    idx = new_index(tmp_path, projects)
    idx.scan()
    store = transcript.TranscriptStore(projects, load_state=idx.load_state)
    reader = store._reader(projects / "-w" / "s1.jsonl", include_sidechain=False)
    assert reader.offset == os.path.getsize(projects / "-w" / "s1.jsonl")  # nothing to re-read
    assert session.session_json(store, "s1")["title"] == "First"


def test_search_by_name_or_any_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/Users/me")
    p = tmp_path / "projects"
    append(p / "-a" / "dash.jsonl", {"type": "ai-title", "aiTitle": "Claude Code session dashboard"},
           entry("1", "2026-10-03T00:00:00Z", cwd="/Users/me/workspace/projects"),
           entry("2", "2026-10-03T01:00:00Z", cwd="/Users/me/workspace/projects/worktrees/dash_v1"))
    append(p / "-b" / "ro.jsonl", {"type": "ai-title", "aiTitle": "Notițe DUPĂ ședință"},
           entry("3", "2026-10-02T00:00:00Z", cwd="/Users/me/workspace/personal/notes"))
    append(p / "-c" / "untitled.jsonl", {"type": "last-prompt", "lastPrompt": "fix the garden sensor box"},
           entry("4", "2026-10-01T00:00:00Z", cwd="/Users/me/workspace/garden-sensors"))
    idx = new_index(tmp_path, p)
    idx.scan()

    def ids(q):
        return [i["session_id"] for i in idx.page(1, 50, q)[1]]

    assert ids("") == ["dash", "ro", "untitled"]
    assert ids("DASHBOARD") == ["dash"]  # title, any case
    assert ids("worktrees") == ["dash"]  # a directory it moved to mid-session
    assert ids("~/workspace/personal") == ["ro"]  # ~ is the home directory
    assert ids("după") == ["ro"]  # non-ASCII case folding
    assert ids("garden box") == ["untitled"]  # untitled: its last prompt is its name; all words must match
    assert ids("garden dashboard") == []
    assert ids("dash_v") == ["dash"] and ids("dash%") == []  # LIKE wildcards are literal
    assert ids("untit") == ["untitled"]  # session id
    assert idx.page(1, 50, "notes")[0] == 1 and idx.count() == 3
