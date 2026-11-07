import json

import pytest

from harness_watch import pricing, transcript


def assistant(msg_id, blocks, usage, model="claude-opus-5-5", cwd="/w", ts="2026-10-29T06:00:00Z", **extra):
    return {
        "type": "assistant",
        "cwd": cwd,
        "gitBranch": "main",
        "timestamp": ts,
        "message": {"id": msg_id, "model": model, "content": blocks, "usage": usage},
        **extra,
    }


def user(cwd="/w", ts="2026-10-29T06:00:00Z"):
    return {"type": "user", "cwd": cwd, "timestamp": ts, "message": {"role": "user", "content": "hi"}}


U1 = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200, "output_tokens": 50}
U2 = {"input_tokens": 5, "cache_read_input_tokens": 1200, "cache_creation_input_tokens": 100, "output_tokens": 30}


def write(path, entries, mode="a"):
    with path.open(mode) as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_split_message_counted_once(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [
        user(),
        assistant("m1", [{"type": "thinking"}], U1),
        assistant("m1", [{"type": "text", "text": "x"}], U1),
        assistant("m1", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}}], U1),
    ])
    st = transcript.TranscriptStats(p)
    st.refresh()
    j = st.to_json()
    assert j["requests"] == 1
    assert j["tokens"]["output_tokens"] == 50
    assert j["context_tokens"] == 1260
    assert j["files_edited"] == {"/w/a.py": 1}


def test_cost_matches_price_table(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [], U1), assistant("m2", [], U2)])
    st = transcript.TranscriptStats(p)
    st.refresh()
    price = pricing.PRICES["claude-opus-5-5"]
    expected = sum(
        u["input_tokens"] * price.input
        + u["output_tokens"] * price.output
        + u["cache_read_input_tokens"] * price.cache_read
        + u["cache_creation_input_tokens"] * price.input * 1.25
        for u in (U1, U2)
    ) / 1e6
    assert st.to_json()["cost_usd"] == pytest.approx(expected, abs=1e-4)
    assert st.to_json()["cost_complete"] is True


def test_one_hour_cache_writes_cost_double():
    usage = {"cache_creation_input_tokens": 1_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
    assert pricing.cost_usd(usage, "claude-opus-5-5") == pytest.approx(8.0)


def test_model_lookup_handles_suffixes_and_prefix_collisions():
    assert pricing.lookup("claude-opus-5-5[1m]") is pricing.PRICES["claude-opus-5-5"]
    assert pricing.lookup("claude-opus-5[1m]") is pricing.PRICES["claude-opus-5"]
    assert pricing.lookup("claude-haiku-4-5-20251001") is pricing.PRICES["claude-haiku-4-5"]
    assert pricing.lookup("gpt-x") is None


def test_unknown_model_marks_cost_incomplete(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [], U1, model="claude-future-9")])
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.to_json()["cost_complete"] is False
    assert st.to_json()["context_pct"] is None


def test_cwd_history_and_title(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [
        user(cwd="/a", ts="t1"),
        user(cwd="/a", ts="t2"),
        user(cwd="/b", ts="t3"),
        user(cwd="/a", ts="t4"),
        {"type": "ai-title", "aiTitle": "Fix the thing"},
        {"type": "last-prompt", "lastPrompt": "please fix"},
    ])
    st = transcript.TranscriptStats(p)
    st.refresh()
    j = st.to_json()
    assert [h["cwd"] for h in j["cwd_history"]] == ["/a", "/b", "/a"]
    assert j["cwd_history"][1]["since"] == "t3"
    assert j["cwd"] == "/a"
    assert j["title"] == "Fix the thing"
    assert j["last_prompt"] == "please fix"


def test_a_given_name_wins_over_claude_codes_title(tmp_path):
    """A fork started with --name (or a /rename) writes a custom-title; Claude Code keeps adding
    ai-titles after it, which must not take the name's place."""
    p = tmp_path / "s.jsonl"
    write(p, [{"type": "ai-title", "aiTitle": "Report quote language"},
              {"type": "custom-title", "customTitle": "Report quote (fork)"},
              {"type": "ai-title", "aiTitle": "Report quote language and link"}])
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.to_json()["title"] == "Report quote (fork)"


def test_incremental_reads_and_partial_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [], U1)])
    st = transcript.TranscriptStats(p)
    st.refresh()
    line = json.dumps(assistant("m2", [], U2))
    with p.open("a") as f:
        f.write(line[:20])  # Claude Code mid-write
    st.refresh()
    assert st.to_json()["requests"] == 1
    with p.open("a") as f:
        f.write(line[20:] + "\n")
    st.refresh()
    assert st.to_json()["requests"] == 2
    assert st.to_json()["context_tokens"] == 1335


def test_truncated_file_is_reread(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [], U1), assistant("m2", [], U2)])
    st = transcript.TranscriptStats(p)
    st.refresh()
    write(p, [assistant("m3", [], U1)], mode="w")
    st.refresh()
    assert st.to_json()["requests"] == 1


def test_sidechain_and_synthetic_entries_ignored(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [
        assistant("m1", [], U1),
        assistant("m2", [], U2, isSidechain=True, cwd="/elsewhere"),
        assistant("m3", [], {"input_tokens": 0, "output_tokens": 0}, model="<synthetic>"),
        "not a dict",
    ])
    with p.open("a") as f:
        f.write("{broken\n")
    st = transcript.TranscriptStats(p)
    st.refresh()
    j = st.to_json()
    assert j["requests"] == 1
    assert j["model"] == "claude-opus-5-5"
    assert [h["cwd"] for h in j["cwd_history"]] == ["/w"]


def test_store_finds_transcript_in_any_project_dir(tmp_path):
    proj = tmp_path / "-Users-me-x"
    proj.mkdir()
    write(proj / "sid.jsonl", [assistant("m1", [], U1)])
    store = transcript.TranscriptStore(tmp_path)
    assert store.get("sid").to_json()["requests"] == 1
    assert store.get("missing") is None
    store.prune(set())
    assert store._stats == {}


def test_cost_parts_split_and_sum(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [], U1), assistant("m2", [], U2)])
    st = transcript.TranscriptStats(p)
    st.refresh()
    j = st.to_json()
    price = pricing.PRICES["claude-opus-5-5"]
    assert j["cost_parts"]["cache_read"] == pytest.approx(2200 * price.cache_read / 1e6, abs=1e-4)
    assert sum(j["cost_parts"].values()) == pytest.approx(j["cost_usd"], abs=1e-3)


def test_branch_head_means_no_repo(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [{**user(), "gitBranch": "HEAD"}])
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.to_json()["git_branch"] is None


def test_relative_paths_resolve_against_cwd(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [{"type": "tool_use", "name": "Read", "input": {"file_path": "a/b.py"}},
                               {"type": "tool_use", "name": "LS", "input": {"path": "docs"}}], U1, cwd="/w")])
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.files_read == {"/w/a/b.py": 1}
    assert st.dirs_listed == {"/w/docs": 1}


def test_gitinfo_reads_worktree_and_detached_heads(tmp_path):
    from harness_watch import gitinfo

    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("0123456789abcdef\n")
    (main / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/feat/x\n")
    wt = tmp_path / "wt"
    (wt / "pkg").mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")

    assert gitinfo.repo_of(str(wt / "pkg" / "new_file.py")) == str(wt)
    assert gitinfo.current_branch(str(wt)) == "feat/x"
    assert gitinfo.current_branch(str(main)) == "detached 0123456"
    assert gitinfo.repo_of(str(tmp_path / "elsewhere.txt")) is None


def bash(cmd):
    return {"type": "tool_use", "name": "Bash", "input": {"command": cmd}}


def test_bash_reads_and_edits_feed_the_trees(tmp_path):
    (tmp_path / "f.txt").write_text("x\n")
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [bash("cat f.txt; ls; echo z > new.txt")], U1, cwd=str(tmp_path))])
    st = transcript.TranscriptStats(p)
    st.refresh()
    j = st.to_json()
    assert j["files_read"] == {f"{tmp_path}/f.txt": 1}
    assert j["dirs_listed"] == {str(tmp_path): 1}
    assert j["files_edited"] == {f"{tmp_path}/new.txt": 1}


def test_edits_in_a_removed_worktree_show_as_removed(tmp_path):
    gone = tmp_path / "repo-feature"  # never created: stands for a worktree deleted after the edits
    p = tmp_path / "s.jsonl"
    write(p, [
        {**assistant("m1", [{"type": "tool_use", "name": "Edit", "input": {"file_path": f"{gone}/src/a.py"}}], U1,
                     cwd=str(gone)), "gitBranch": "feature"},
        assistant("m2", [{"type": "tool_use", "name": "Edit", "input": {"file_path": f"{gone}/b.py"}}], U2, cwd="/w"),
    ])
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.to_json()["repos"] == {str(gone): {"branches": ["feature"], "removed": True}}

    (gone / "src").mkdir(parents=True)  # the directory appears (e.g. created by the tool after the entry)
    (gone / ".git").mkdir()
    (gone / ".git" / "HEAD").write_text("ref: refs/heads/feature\n")
    st.refresh()
    assert st.to_json()["repos"] == {str(gone): {"branches": ["feature"], "removed": False}}


def test_split_message_keeps_the_final_usage(tmp_path):
    """Sub-agent transcripts repeat a message's usage with growing output tokens: the last one counts."""
    p = tmp_path / "s.jsonl"
    write(p, [assistant("m1", [], {**U1, "output_tokens": 3}), assistant("m1", [], U1), assistant("m2", [], U2)])
    st = transcript.TranscriptStats(p)
    st.refresh()
    assert st.to_json()["requests"] == 2
    assert st.to_json()["tokens"]["output_tokens"] == 80


def test_state_round_trip_resumes_reading(tmp_path):
    p = tmp_path / "s.jsonl"
    write(p, [user(ts="t0"), assistant("m1", [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}}], U1)])
    st = transcript.TranscriptStats(p)
    st.refresh()
    saved = json.loads(json.dumps(st.to_state()))
    write(p, [assistant("m1", [], U1), assistant("m2", [], U2)])  # m1 continues across the save
    resumed = transcript.TranscriptStats.from_state(p, saved)
    resumed.refresh()
    straight = transcript.TranscriptStats(p)
    straight.refresh()
    assert resumed.to_json() == straight.to_json()
    assert resumed.to_json()["requests"] == 2
    assert resumed.to_json()["first_activity"] == "t0"
