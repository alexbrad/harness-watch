"""One session's full picture: its main transcript joined with its sub-agents and background tasks.

A sub-agent runs while its transcript is mid-turn (the last entry is a tool call or a result),
until a completion notice newer than its last entry arrives. A background shell or monitor runs
until its notice arrives. Neither runs in a closed session, or if it started before the current
process did (the session was resumed: tasks of the earlier process died with it).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import transcript

DETAIL_FIELDS = (*transcript.ACTIVITY_FIELDS, "subagents", "shells")  # fetched for the side panel only
TAIL_BYTES = 64 << 10
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]")


def agent_id(path: Path) -> str:
    return path.stem.removeprefix("agent-")


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def _since(iso: str | None, live_since: float | None) -> bool:
    """Whether something stamped `iso` belongs to the current process (started at `live_since`)."""
    if live_since is None:
        return False
    t = _epoch(iso)
    return t is None or t >= live_since - 5  # unknown time: assume current


def _state(notice: dict | None) -> str:
    return {"completed": "done"}.get(notice["status"], notice["status"]) if notice else "done"


def subagent_json(st: transcript.TranscriptStats, meta: dict, notices: dict, live_since: float | None) -> dict:
    aid = agent_id(st.path)
    j = st.to_json()
    notice = notices.get(aid)
    # A notice from before the agent's last entry is from an earlier round: it was resumed since.
    if notice and (not st.last_activity or (notice.get("at") or "") >= st.last_activity):
        state = _state(notice)
    elif st.working:
        state = "running" if _since(st.last_activity, live_since) else "interrupted"
    else:
        state = "done"
    return {
        "id": aid,
        "type": meta.get("agentType"),
        "name": meta.get("name"),
        "description": meta.get("description"),
        "depth": meta.get("spawnDepth") or 1,
        "parent": meta.get("parentAgentId"),
        "background": meta.get("requestShape") == "background",
        "model": j["model"],
        "started": j["first_activity"],
        "last_activity": j["last_activity"],
        "requests": j["requests"],
        "tokens_total": j["tokens_total"],
        "cost_usd": j["cost_usd"],
        "state": state,
    }


def shell_json(task_id: str, task: dict, notices: dict, live_since: float | None) -> dict:
    notice = notices.get(task_id)
    if notice:
        state = _state(notice)
    else:
        state = "running" if _since(task.get("started"), live_since) else "ended"  # ended: how is unknown
    code = re.search(r"exit code (\d+)", (notice or {}).get("summary") or "")
    return {"id": task_id, **task, "state": state, "ended": (notice or {}).get("at"),
            "exit_code": int(code[1]) if code else None, "summary": (notice or {}).get("summary")}


def build(main: transcript.TranscriptStats, subs: list[tuple[transcript.TranscriptStats, dict]],
          live_since: float | None) -> dict:
    """`main.to_json()` with sub-agents' file activity merged in, their tokens and cost added to the
    totals (`main` keeps the main transcript's own), and the sub-agent and shell lists.
    `live_since`: start time of the running process (epoch s), None for a closed session."""
    j = main.to_json()
    notices = dict(main.notices)
    tasks = dict(main.tasks)
    for sub, _ in subs:
        transcript.merge_activity(j, sub)
        notices.update(sub.notices)
        tasks.update(sub.tasks)
    agents = [subagent_json(sub, meta, notices, live_since) for sub, meta in subs]
    agents.sort(key=lambda a: a["started"] or "")
    shells = sorted((shell_json(tid, t, notices, live_since) for tid, t in tasks.items()),
                    key=lambda s: s["started"] or "")

    j["main"] = {"tokens_total": j["tokens_total"], "cost_usd": j["cost_usd"], "requests": j["requests"]}
    for sub, _ in subs:
        s = sub.to_json()
        for k, v in s["tokens"].items():
            j["tokens"][k] += v
        for k, v in s["cost_parts"].items():
            j["cost_parts"][k] = round(j["cost_parts"][k] + v, 4)
        j["requests"] += s["requests"]
        j["cost_complete"] = j["cost_complete"] and s["cost_complete"]
    # Limit hits and responses of sub-agents count too: they use the same account.
    readers = [main, *(sub for sub, _ in subs)]
    hits = [r.limit for r in readers if r.limit and r.limit.get("at")]
    j["limit"] = max(hits, key=lambda h: h["at"]) if hits else None
    j["last_response_at"] = max((r.last_response_at for r in readers if r.last_response_at), default=None)
    j["tokens_total"] = sum(j["tokens"].values())
    j["cost_usd"] = round(sum(j["cost_parts"].values()), 4)
    j["subagents"] = agents
    j["shells"] = shells
    j["task_counts"] = {
        "agents": len(agents), "agents_running": sum(a["state"] == "running" for a in agents),
        "shells": len(shells), "shells_running": sum(s["state"] == "running" for s in shells),
    }
    return j


def session_json(store: transcript.TranscriptStore, session_id: str, live_since: float | None = None) -> dict | None:
    """`build` over the store's incremental readers, for a running session."""
    main = store.get(session_id)
    if main is None:
        return None
    subs = [(sub, store.meta(sub.path)) for sub in store.subagents(session_id, main.path)]
    return build(main, subs, live_since)


def tail(path: str, max_bytes: int = TAIL_BYTES) -> dict:
    """The end of a task's output file as plain text: colours stripped, and of a line rewritten
    with carriage returns (progress bars) only its last version kept."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes)
    except OSError:
        return {"exists": False, "size": 0, "text": "", "truncated": False}
    text = _ANSI.sub("", data.decode("utf-8", errors="replace"))
    truncated = size > max_bytes
    if truncated:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n")]
    return {"exists": True, "size": size, "text": "\n".join(lines), "truncated": truncated}

