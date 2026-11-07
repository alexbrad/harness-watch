"""Incremental reader for a session transcript (`~/.claude/projects/<dir>/<sessionId>.jsonl`).

Each poll reads only the bytes appended since the last one. The format is internal to
Claude Code: unknown entry types are ignored, malformed lines skipped.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import events, flow, gitinfo, limits, pricing, shellparse

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}  # their `path` defaults to the cwd
ACTIVITY_KEYS = ("files_edited", "files_read", "dirs_listed")
ACTIVITY_FIELDS = (*ACTIVITY_KEYS, "repos")  # the per-file detail, kept out of the per-second push
BACKGROUND_TOOLS = {"Bash": "shell", "Monitor": "monitor"}  # tools whose commands can outlive the call
HANDBACK = "SubagentHandback"  # a sub-agent's last tool call: hands its report to the parent
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
CHUNK = 8 << 20  # read big transcripts in 8 MB pieces


@dataclass
class TranscriptStats:
    path: Path
    include_sidechain: bool = False  # sub-agent transcripts consist of sidechain entries only
    offset: int = 0  # bytes consumed, up to the end of the last complete line
    # Token totals per model (TOKEN_KEYS plus "cache_1h"). One API response is stored as
    # several consecutive entries (thinking, text, tool_use), each carrying the usage so far
    # (output tokens grow): a repeated message id replaces its earlier counts.
    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    requests: int = 0
    _recent: list[list] = field(default_factory=list)  # [message id, model, counts] of the last few
    last_usage: dict | None = None
    model: str | None = None
    git_branch: str | None = None
    cwd_history: list[dict] = field(default_factory=list)  # [{"cwd", "since"}]
    title: str | None = None  # the name you gave it (/rename, --name), else Claude Code's own title
    custom_title: str | None = None
    last_prompt: str | None = None
    files_edited: dict[str, int] = field(default_factory=dict)  # path -> edit count
    files_read: dict[str, int] = field(default_factory=dict)  # path -> read count
    dirs_listed: dict[str, int] = field(default_factory=dict)  # dirs searched or listed
    repos: dict[str, list[str]] = field(default_factory=dict)  # repo root -> branches edited on
    # Edits whose directory doesn't exist (yet, or any more): (path, cwd, branch at edit time).
    _unresolved: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    first_activity: str | None = None  # ISO timestamp of the oldest entry
    last_activity: str | None = None  # ISO timestamp of the newest entry
    # Background shells and monitors started here: id -> {"kind", "command", "description", "started", "output_file"}
    tasks: dict[str, dict] = field(default_factory=dict)
    # Last completion notice per task id (shells, monitors, sub-agents): id -> {"status", "summary", "at"}
    notices: dict[str, dict] = field(default_factory=dict)
    _uses: dict[str, list] = field(default_factory=dict)  # Bash/Monitor call id -> [tool, command, description]
    working: bool = False  # mid-turn: the last entry is a tool call or a result, not a final answer
    # Last usage limit hit: {"text", "at", "resets"}; reset by Claude Code's "Usage limit reset".
    limit: dict | None = None
    last_response_at: str | None = None  # last real model response: the account wasn't limited then
    # Tool calls, results ("use", "result") and messages received ("msg") read by the last refresh,
    # for the index; None: not collected.
    events: list[tuple[str, dict]] | None = field(default=None, repr=False, compare=False)
    _handback: str | None = None  # id of the SubagentHandback call, whose result ends a sub-agent's turn

    def to_state(self) -> dict:
        """Everything needed to resume reading later, as JSON-able data (path excluded)."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("path", "events")}

    @classmethod
    def from_state(cls, path: Path, state: dict) -> TranscriptStats:
        known = {f.name for f in fields(cls)} - {"path", "events"}
        st = cls(path, **{k: v for k, v in state.items() if k in known})
        st._unresolved = [tuple(u) for u in st._unresolved]
        return st

    def refresh(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self.offset:  # rewritten or truncated: start over
            collected = self.events
            self.__init__(self.path, self.include_sidechain)
            if collected is not None:  # the index drops the file's earlier events
                self.events = [*collected, ("restart", {})]
        if size > self.offset:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                rest = b""
                while chunk := f.read(min(CHUNK, size - self.offset - len(rest))):
                    lines = (rest + chunk).split(b"\n")
                    rest = lines.pop()  # incomplete line: finished by the next chunk or a later write
                    self.offset += sum(len(line) + 1 for line in lines)
                    for line in lines:
                        self._apply_line(line)
                    if self.offset + len(rest) >= size:
                        break
        self._resolve_edits()

    def _apply_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if isinstance(entry, dict):
            self._apply(entry)

    def _apply(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "custom-title" and e.get("customTitle"):
            self.custom_title = self.title = e["customTitle"]
        elif kind == "ai-title" and e.get("aiTitle"):
            self.title = self.custom_title or e["aiTitle"]
        elif kind == "last-prompt" and e.get("lastPrompt"):
            self.last_prompt = e["lastPrompt"]
        elif kind == "system" and str(e.get("content", "")).startswith("Usage limit reset"):
            self.limit = None
        elif kind == "queue-operation" and e.get("operation") == "enqueue":
            self._notice(e.get("content"), e.get("timestamp"))  # written when it fires, before it is delivered
        if e.get("isSidechain") and not self.include_sidechain:
            return
        self._ts = e.get("timestamp")
        if e.get("timestamp"):
            self.last_activity = e["timestamp"]
            self.first_activity = self.first_activity or e["timestamp"]
        cwd = e.get("cwd")
        if cwd and (not self.cwd_history or self.cwd_history[-1]["cwd"] != cwd):
            self.cwd_history.append({"cwd": cwd, "since": e.get("timestamp")})
        branch = e.get("gitBranch")
        if branch:
            self.git_branch = None if branch == "HEAD" else branch  # "HEAD": not in a git repo
        if kind == "assistant":
            msg = e.get("message") or {}
            if msg.get("model") == "<synthetic>" and e.get("error") == "rate_limit":
                text = _text(msg.get("content"))
                self.limit = {"text": text, "at": e.get("timestamp"), "resets": limits.resets_at(text, e.get("timestamp"))}
            elif msg.get("model") and not msg["model"].startswith("<"):
                self.last_response_at = e.get("timestamp")
            self._apply_assistant(msg, cwd)
        elif kind == "user":
            self._apply_user(e)
        elif kind == "attachment" and (e.get("attachment") or {}).get("type") == "queued_command":
            self._messages(e["attachment"].get("prompt"))  # delivered mid-turn

    def _apply_assistant(self, msg: dict, cwd: str | None) -> None:
        model = msg.get("model")
        if model and not model.startswith("<"):  # skip "<synthetic>" placeholder messages
            self.model = model
        usage = msg.get("usage")
        if isinstance(usage, dict) and model and not model.startswith("<"):
            self.last_usage = usage
            counts = {k: usage.get(k) or 0 for k in TOKEN_KEYS}
            counts["cache_1h"] = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
            msg_id = msg.get("id")
            previous = next((r for r in self._recent if r[0] == msg_id), None) if msg_id else None
            if previous:  # same response, later entry: swap its counts for the newer ones
                self._add_usage(previous[1], previous[2], -1)
                previous[1], previous[2] = model, counts
            else:
                self.requests += 1
                if msg_id:
                    self._recent = [*self._recent[-31:], [msg_id, model, counts]]
            self._add_usage(model, counts, 1)
        self._apply_tools(msg, cwd)

    def _add_usage(self, model: str, counts: dict[str, int], sign: int) -> None:
        totals = self.usage_by_model.setdefault(model, {})
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + sign * v

    def _apply_tools(self, msg: dict, cwd: str | None) -> None:
        uses = [b for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        for block in uses:
            if self.events is not None:
                self.events.append(("use", events.use_event(block, self._ts, cwd)))
            name, args = block.get("name"), block.get("input") or {}
            self._apply_tool(name, args, cwd)
            if name in BACKGROUND_TOOLS and isinstance(args, dict) and block.get("id"):
                self._uses[block["id"]] = [name, args.get("command"), args.get("description")]
                if len(self._uses) > 64:  # results normally follow at once; drop the oldest unanswered
                    del self._uses[next(iter(self._uses))]
            if name == HANDBACK:
                self._handback = block.get("id")
        # Split entries of one response arrive text first, tool calls after: only a call reopens the turn.
        if uses:
            self.working = any(b.get("name") != HANDBACK for b in uses)
        elif msg.get("content"):
            self.working = False

    def _apply_user(self, e: dict) -> None:
        content = (e.get("message") or {}).get("content")
        ts = e.get("timestamp")
        if isinstance(content, str):
            if content.lstrip().startswith("<task-notification>"):
                self._notice(content, ts)
            self._messages(content)
            self.working = True  # a prompt or notice: the model answers next
            return
        if isinstance(content, list) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            self._messages(_text(content))
        result = e.get("toolUseResult")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            use_id = block.get("tool_use_id")
            if self.events is not None:
                text = _text(block.get("content"))
                self.events.append(("result", events.result_event(block, ts, text, result)))
                answer = flow.result(text, result)
                if answer:
                    self.events.append(("msg", {**answer, "ts": ts}))
            self.working = use_id != self._handback
            use = self._uses.pop(use_id, None)
            if not isinstance(result, dict):
                continue
            task_id = result.get("backgroundTaskId") or (use and use[0] == "Monitor" and result.get("taskId"))
            if task_id and isinstance(task_id, str):
                output = re.search(r"Output is being written to: (\S+?\.output)", _text(block.get("content")))
                self.tasks[task_id] = {
                    "kind": BACKGROUND_TOOLS[use[0]] if use else "shell",
                    "command": use[1] if use else None,
                    "description": use[2] if use else None,
                    "started": ts,
                    "output_file": output[1] if output else None,
                }
            stopped = result.get("task_id")
            if isinstance(stopped, str) and str(result.get("message", "")).startswith("Successfully stopped"):
                self.notices.setdefault(stopped, {"status": "killed", "summary": "Stopped with TaskStop", "at": ts})

    def _messages(self, text) -> None:
        if self.events is not None:
            self.events.extend(("msg", {**m, "ts": self._ts}) for m in flow.parse(text, self.include_sidechain))

    def _notice(self, text, ts: str | None) -> None:
        """A `<task-notification>`: a background shell, monitor or sub-agent stopped. Monitor events,
        which have no status, are not completions."""
        if not isinstance(text, str) or not text.lstrip().startswith("<task-notification>"):
            return
        task_id = re.search(r"<task-id>([^<]+)</task-id>", text)
        status = re.search(r"<status>([^<]+)</status>", text)
        if task_id and status:
            summary = re.search(r"<summary>(.*?)</summary>", text, re.DOTALL)
            self.notices[task_id[1].strip()] = {"status": status[1].strip(), "at": ts,
                                                "summary": summary[1].strip() if summary else None}

    def _apply_tool(self, name: str | None, args: dict, cwd: str | None) -> None:
        if not isinstance(args, dict):
            return
        if name == "Bash" and isinstance(args.get("command"), str):
            act = shellparse.parse(args["command"], cwd)
            for seen, paths in ((self.files_read, act.read), (self.dirs_listed, act.listed), (self.files_edited, act.written)):
                for path in paths:
                    seen[path] = seen.get(path, 0) + 1
            for path in act.written:
                self._record_branch(path, cwd)
        elif name in EDIT_TOOLS or name in READ_TOOLS:
            path = _absolute(args.get("file_path") or args.get("notebook_path"), cwd)
            if not path:
                return
            seen = self.files_edited if name in EDIT_TOOLS else self.files_read
            seen[path] = seen.get(path, 0) + 1
            if name in EDIT_TOOLS:
                self._record_branch(path, cwd)
        elif name in SEARCH_TOOLS:
            path = _absolute(args.get("path") or cwd, cwd)
            if not path:
                return
            seen = self.files_read if name == "Grep" and Path(path).is_file() else self.dirs_listed
            seen[path] = seen.get(path, 0) + 1

    def _record_branch(self, path: str, cwd: str | None) -> None:
        # Resolved after the batch: the tool may not have created the file's directory yet.
        self._unresolved.append((path, cwd, self.git_branch))

    def _resolve_edits(self) -> None:
        pending, self._unresolved = self._unresolved, []
        for path, cwd, branch_then in pending:
            root = gitinfo.repo_of(path)
            if root is None:
                if not os.path.isdir(os.path.dirname(path)):
                    self._unresolved.append((path, cwd, branch_then))  # retried on every refresh
                continue
            # The entry's branch was recorded at edit time, but only for the cwd's repo.
            if branch_then and cwd and gitinfo.repo_root(cwd) == root:
                branch = branch_then
            else:
                branch = gitinfo.current_branch(root)
            branches = self.repos.setdefault(root, [])
            if branch and branch not in branches:
                branches.append(branch)

    def repos_json(self) -> dict[str, dict]:
        """Repo root -> {"branches", "removed"}; a removed repo is guessed from the missing directory."""
        out = {root: {"branches": list(b), "removed": False} for root, b in self.repos.items()}
        for path, cwd, branch_then in self._unresolved:
            entry = out.setdefault(gitinfo.missing_root(path), {"branches": [], "removed": True})
            # Its branch is only known when the session's cwd was inside it at edit time.
            if branch_then and cwd and path.startswith(cwd.rstrip("/") + "/") and branch_then not in entry["branches"]:
                entry["branches"].append(branch_then)
        return out

    @property
    def context_tokens(self) -> int | None:
        """Tokens the next request starts from: the last request's prompt plus its output."""
        u = self.last_usage
        if not u:
            return None
        return sum(u.get(k) or 0 for k in TOKEN_KEYS)

    def to_json(self) -> dict:
        totals = {k: 0 for k in TOKEN_KEYS}
        parts = {k: 0.0 for k in pricing.COST_PARTS}
        cost_known = True
        for model, t in self.usage_by_model.items():
            for k in TOKEN_KEYS:
                totals[k] += t.get(k, 0)
            usage = {**t, "cache_creation": {"ephemeral_1h_input_tokens": t.get("cache_1h", 0)}}
            c = pricing.cost_parts(usage, model)
            if c is None:
                cost_known = False
            else:
                for k, v in c.items():
                    parts[k] += v
        cost = sum(parts.values())
        price = pricing.lookup(self.model)
        context = self.context_tokens
        return {
            "model": self.model,
            "git_branch": self.git_branch,
            "cwd": self.cwd_history[-1]["cwd"] if self.cwd_history else None,
            "cwd_history": self.cwd_history,
            "title": self.title,
            "last_prompt": self.last_prompt,
            "files_edited": self.files_edited,
            "files_read": self.files_read,
            "dirs_listed": self.dirs_listed,
            "repos": self.repos_json(),
            "limit": self.limit,
            "last_response_at": self.last_response_at,
            "first_activity": self.first_activity,
            "last_activity": self.last_activity,
            "requests": self.requests,
            "tokens": totals,
            "tokens_total": sum(totals.values()),
            "cost_usd": round(cost, 4),
            "cost_parts": {k: round(v, 4) for k, v in parts.items()},
            "cost_complete": cost_known,
            "context_tokens": context,
            "context_window": price.context_window if price else None,
            "context_pct": round(100 * context / price.context_window, 1) if price and context else None,
        }


def _text(content) -> str:
    """A tool result's content: a string, or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _absolute(path: str | None, cwd: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    if path.startswith("/") or not cwd:
        return path
    return str(Path(cwd) / path)


def read_meta(path: Path) -> dict:
    """`agent-<id>.meta.json` next to a sub-agent transcript: type, description, depth, parent."""
    try:
        meta = json.loads(path.with_name(f"{path.stem}.meta.json").read_text())
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def merge_activity(into: dict, sub: TranscriptStats) -> None:
    """Add a sub-agent's file activity and branches to a session's `to_json()` dict."""
    for key in ACTIVITY_KEYS:
        merged = into[key] = dict(into[key])
        for path, n in getattr(sub, key).items():
            merged[path] = merged.get(path, 0) + n
    repos = into["repos"] = {k: {**v, "branches": list(v["branches"])} for k, v in into["repos"].items()}
    for root, info in sub.repos_json().items():
        mine = repos.setdefault(root, {"branches": [], "removed": info["removed"]})
        mine["branches"].extend(b for b in info["branches"] if b not in mine["branches"])


class TranscriptStore:
    """Keeps one incremental reader per session, so each poll only reads new lines."""

    def __init__(self, projects_dir: Path, load_state=None):
        self.projects_dir = projects_dir
        self.load_state = load_state  # path -> saved reader state or None: resume instead of re-reading
        self._stats: dict[str, TranscriptStats] = {}
        self._subagents: dict[str, dict[Path, TranscriptStats]] = {}  # session id -> per agent file
        self._meta: dict[Path, dict] = {}

    def find(self, session_id: str) -> Path | None:
        matches = list(self.projects_dir.glob(f"*/{session_id}.jsonl"))
        return max(matches, key=lambda p: p.stat().st_mtime) if matches else None

    def get(self, session_id: str) -> TranscriptStats | None:
        stats = self._stats.get(session_id)
        if stats is None or not stats.path.exists():
            path = self.find(session_id)
            if path is None:
                return None
            stats = self._stats[session_id] = self._reader(path, include_sidechain=False)
        stats.refresh()
        return stats

    def subagents(self, session_id: str, main_path: Path) -> list[TranscriptStats]:
        """Readers for `<sessionId>/subagents/agent-*.jsonl` next to the main transcript."""
        known = self._subagents.setdefault(session_id, {})
        for path in (main_path.parent / session_id / "subagents").glob("agent-*.jsonl"):
            if path not in known:
                known[path] = self._reader(path, include_sidechain=True)
        for stats in known.values():
            stats.refresh()
        return list(known.values())

    def _reader(self, path: Path, include_sidechain: bool) -> TranscriptStats:
        state = None
        if self.load_state:
            try:
                state = self.load_state(path)
            except Exception:  # a broken or locked index must not break the live list
                state = None
        if state:
            return TranscriptStats.from_state(path, state)
        return TranscriptStats(path, include_sidechain=include_sidechain)

    def meta(self, path: Path) -> dict:
        """A sub-agent's meta.json, read once: it is written when the agent starts."""
        if path not in self._meta:
            self._meta[path] = read_meta(path)
        return self._meta[path]

    def prune(self, live_ids: set[str]) -> None:
        for cache in (self._stats, self._subagents):
            for sid in list(cache):
                if sid not in live_ids:
                    del cache[sid]
        kept = {p for agents in self._subagents.values() for p in agents}
        self._meta = {p: m for p, m in self._meta.items() if p in kept}
