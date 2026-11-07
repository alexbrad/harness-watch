"""Messages between a session and its sub-agents, read on the receiving side.

Everything an agent is told reaches its transcript as a user entry: a sub-agent's first prompt
and later messages from its parent, `<teammate-message>`s (SendMessage between agents),
`<agent-message>` hand-backs (a sub-agent's final report), `<task-notification>`s (a background
agent stopped), and, for a foreground agent, the Agent call's result. The sender is recorded
as written (a name, an agent id, or None for the parent) and resolved when the flow is built,
once every agent's name is known.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

TEXT_CAP = 20_000  # stored per message
PREVIEW = 160
GAP_S = 5 * 60  # an agent quiet this long shows as a break in its bar
ASYNC_STATUSES = {"async_launched", "teammate_spawned"}  # launch receipts, not results
LEAD = "team-lead"  # what agents call the main session
# Text Claude Code itself adds to a sub-agent's transcript: notices, interruptions, skill bodies.
HARNESS = ("<", "[", "Base directory for this skill:")
RELAYED = "The coordinator sent a message while you were working:"  # from the parent, mid-turn

_TEAMMATE = re.compile(r"<teammate-message\s+([^>]*)>(.*?)</teammate-message>", re.DOTALL)
_AGENT = re.compile(r'<agent-message\s+from="([^"]+)"[^>]*>(.*?)(?:</agent-message>|\Z)', re.DOTALL)
_ATTR = re.compile(r'(\w+)="([^"]*)"')


def _tag(text: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return m[1].strip() if m else None


def _cap(text: str) -> str:
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + f"\n… ({len(text)} chars)"


def _teammate(attrs: str, body: str) -> dict:
    a = dict(_ATTR.findall(attrs))
    body = body.strip()
    kind, title = "message", a.get("summary")
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("type"):
            kind = "idle" if data["type"] == "idle_notification" else str(data["type"])
            title = data.get("summary") or title or (data.get("idleReason") if kind == "idle" else None)
            body = next((str(data[k]) for k in ("result", "message", "content", "reason") if data.get(k)), body)
    return {"kind": kind, "peer": a.get("teammate_id"), "title": title, "text": _cap(body)}


def _handback(sender: str, body: str) -> dict:
    _, sep, report = body.partition("The report follows:\n")
    lines = (report if sep else body).splitlines()  # the harness indents the report by two spaces
    return {"kind": "handback", "peer": sender, "title": "final report",
            "text": _cap("\n".join(line.removeprefix("  ") for line in lines).strip())}


def parse(text, in_subagent: bool) -> list[dict]:
    """Messages in one delivered user entry: [{"kind", "peer", "title", "text"}]. `peer` None:
    from the parent (a sub-agent's prompt). A person's prompt to the main session is not one."""
    if not isinstance(text, str) or not text.strip():
        return []
    found = [_teammate(attrs, body) for attrs, body in _TEAMMATE.findall(text)]
    found += [_handback(sender, body) for sender, body in _AGENT.findall(text)]
    if found:
        return found
    stripped = text.lstrip()
    if stripped.startswith("<task-notification>"):
        task, status = _tag(text, "task-id"), _tag(text, "status")
        if not task or not status:
            return []  # a monitor event
        result = _tag(text, "result") or ""
        return [{"kind": "notification", "peer": task, "title": f"{status}: {_tag(text, 'summary') or ''}".strip(),
                 "text": _cap(result)}]
    if not in_subagent or stripped.startswith(HARNESS):
        return []
    if stripped.startswith(RELAYED):
        return [{"kind": "message", "peer": None, "title": None, "text": _cap(stripped[len(RELAYED):].strip())}]
    return [{"kind": "prompt", "peer": None, "title": None, "text": _cap(stripped.rstrip())}]


def result(text: str, tool_result) -> dict | None:
    """A foreground agent's answer, as the Agent call's result."""
    r = tool_result if isinstance(tool_result, dict) else {}
    agent = r.get("agentId")
    if not isinstance(agent, str) or r.get("status") in ASYNC_STATUSES or not text:
        return None
    return {"kind": "result", "peer": agent, "title": "result", "text": _cap(text)}


# Building the flow of one session.

def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else None
    except ValueError:
        return None


def resolver(agents: list[dict]):
    """A function from a message's raw sender (a name, an agent id, None) and its receiver
    to a lane id: "main", an agent id, or "@<name>" for someone outside this session."""
    ids = {a["id"] for a in agents}
    names = {}
    for a in agents:
        for n in (a.get("name"), a.get("type")):
            if n:
                names.setdefault(n, a["id"])
    parents = {a["id"]: a.get("parent") if a.get("parent") in ids else "main" for a in agents}

    def resolve(peer: str | None, receiver: str) -> str:
        if peer is None:
            return parents.get(receiver, "main")
        if peer in ids:
            return peer
        if peer in (LEAD, "main"):
            return "main"
        return names.get(peer) or f"@{peer}"
    return resolve


def segments(stamps: list[str], gap_s: float = GAP_S) -> list[list[str]]:
    """Activity spans from timestamps: a new span starts after `gap_s` of silence."""
    out: list[list] = []
    last = None
    for ts in sorted(s for s in stamps if s):
        t = _epoch(ts)
        if t is None:
            continue
        if out and last is not None and t - last <= gap_s:
            out[-1][1] = ts
        else:
            out.append([ts, ts])
        last = t
    return out


def build(agents: list[dict], rows: list[dict], stamps: dict[str | None, list[str]]) -> dict:
    """The flow: one lane per agent (main first, children under their parent), its activity
    spans, and the messages between lanes.
    `rows`: stored messages {"n", "agent_id" (receiver, None: main), "ts", "kind", "peer", "title", "preview"}.
    `stamps`: agent id (None: main) -> timestamps of its tool calls and results."""
    resolve = resolver(agents)
    messages = []
    for r in rows:
        to = r["agent_id"] or "main"
        frm = resolve(r["peer"], to)
        if r["kind"] == "notification" and frm.startswith("@"):
            continue  # a background shell's or monitor's notice
        if frm == to:
            continue
        messages.append({"n": r["n"], "ts": r["ts"], "from": frm, "to": to, "kind": r["kind"],
                         "title": r["title"], "preview": r["preview"]})
    by_lane: dict[str, list[str]] = {}
    for m in messages:
        for lane in (m["from"], m["to"]):
            by_lane.setdefault(lane, []).append(m["ts"])
    children: dict[str, list[dict]] = {}
    ids = {a["id"] for a in agents}
    for a in sorted(agents, key=lambda a: a.get("started") or ""):
        children.setdefault(a.get("parent") if a.get("parent") in ids else "main", []).append(a)
    lanes = []

    def add(lane_id: str, agent: dict | None, depth: int):
        own = list(stamps.get(None if lane_id == "main" else lane_id, []))
        if agent:
            own += [agent.get("started"), agent.get("last_activity")]
        own += by_lane.get(lane_id, [])
        lanes.append({"id": lane_id, "depth": depth, "segments": segments([s for s in own if s]),
                      **({k: agent.get(k) for k in ("name", "type", "description", "state", "started",
                                                        "last_activity", "background")} if agent else {"name": "main session"})})
        for child in children.get(lane_id, []):
            add(child["id"], child, depth + 1)

    add("main", None, 0)
    for peer in sorted({lane for m in messages for lane in (m["from"], m["to"]) if lane.startswith("@")}):
        lanes.append({"id": peer, "depth": 0, "name": f"{peer[1:]} (outside this session)", "external": True,
                      "segments": segments(by_lane.get(peer, []))})
    return {"lanes": lanes, "messages": messages}
