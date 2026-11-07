"""Tool calls as explorer events: a category, a group within it, a one-line summary, and a capped
copy of the input. Results update the call's status and keep a short preview."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from . import shellparse

CATEGORIES = ("web", "commands", "files", "mcp", "agents", "other")
FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead", "Grep", "Glob", "LS"}
AGENT_TOOLS = {"Agent", "Task"}
NOT_PROGRAMS = {"cd", "export", "set", "unset", "source", ".", "true", ":", "local", "pushd", "popd"}
LOOP_HEADERS = {"for", "select", "case", "function", "done", "fi", "esac", "}", "[[", "["}  # no program in them
KEYWORDS = {"while", "until", "if", "then", "do", "else", "elif", "!", "{"}  # a program may follow
STRING_CAP = 600  # per string in a stored input
DETAIL_CAP = 4000  # whole stored input, as JSON
PREVIEW_CAP = 600  # stored start of a result


def program(command: str) -> str:
    """The program a shell command is about: the first one that isn't cd, export, a wrapper, …
    e.g. "cd repo && git status" -> "git"."""
    try:
        commands = shellparse._commands(shellparse.tokenize(command))
    except Exception:  # noqa: BLE001 - the tokenizer is strict; a stray quote must not lose the event
        commands = [command.split()]
    first = None
    for words in commands:
        words = [w for w in words if w not in shellparse.REDIRECTS]
        while words and words[0] in KEYWORDS:
            words.pop(0)
        if not words or words[0] in LOOP_HEADERS:
            continue
        while words and (shellparse.ASSIGNMENT.match(words[0]) or words[0] in shellparse.WRAPPERS):
            wrapper = words.pop(0)
            while wrapper in shellparse.WRAPPERS and words and (words[0].startswith("-") or words[0].isdigit()):
                words.pop(0)  # its options: env -i, caffeinate -i -t 300
        if not words:
            continue
        name = words[0].strip("'\"").rsplit("/", 1)[-1]
        first = first or name
        if name not in NOT_PROGRAMS:
            return name
    return first or "shell"


def _cap(value):
    if isinstance(value, str):
        return value if len(value) <= STRING_CAP else value[:STRING_CAP] + f"… ({len(value)} chars)"
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_cap(v) for v in value[:20]]
    return value


def _detail(args: dict) -> str:
    text = json.dumps(_cap(args), ensure_ascii=False)
    return text if len(text) <= DETAIL_CAP else json.dumps({"truncated": text[:DETAIL_CAP]}, ensure_ascii=False)


def classify(tool: str, args: dict) -> tuple[str, str, str]:
    """(category, group, summary) of one call."""
    args = args if isinstance(args, dict) else {}
    s = lambda k: str(args.get(k) or "")
    if tool == "WebFetch":
        return "web", urlparse(s("url")).netloc or "unknown", s("url")
    if tool == "WebSearch":
        return "web", "search", s("query")
    if tool == "Bash":
        return "commands", program(s("command")), s("command")[:DETAIL_CAP]
    if tool == "Monitor":
        return "commands", "monitor", s("command")[:DETAIL_CAP]
    if tool in FILE_TOOLS:
        target = s("file_path") or s("notebook_path") or s("path")
        if tool in {"Grep", "Glob"}:
            return "files", tool, f"{s('pattern')}  in {target or '.'}"
        return "files", tool, target
    if tool.startswith("mcp__"):
        server, _, name = tool.removeprefix("mcp__").partition("__")
        brief = next((s(k) for k in ("url", "query", "action", "text", "path", "name") if s(k)), "")
        return "mcp", server, f"{name} {brief}".strip()
    if tool in AGENT_TOOLS:
        return "agents", s("subagent_type") or "general-purpose", s("description") or s("prompt")[:200]
    brief = next((s(k) for k in ("description", "skill", "query", "task_id", "to", "url") if s(k)), "")
    return "other", tool, brief


def use_event(block: dict, ts: str | None, cwd: str | None) -> dict:
    tool = str(block.get("name") or "?")
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    category, group, summary = classify(tool, args)
    return {"id": block.get("id"), "ts": ts, "tool": tool, "category": category, "group": group,
            "summary": summary, "cwd": cwd, "detail": _detail(args)}


def result_event(block: dict, ts: str | None, text: str, tool_result) -> dict:
    """Status of a finished call: ok, error, interrupted or background (still running)."""
    r = tool_result if isinstance(tool_result, dict) else {}
    if block.get("is_error"):
        status = "error"
    elif r.get("interrupted"):
        status = "interrupted"
    elif r.get("backgroundTaskId") or r.get("status") == "async_launched":
        status = "background"
    else:
        status = "ok"
    code = re.match(r"Exit code (\d+)", text or "")
    return {"id": block.get("tool_use_id"), "ts": ts, "status": status,
            "exit_code": int(code[1]) if code else (0 if status == "ok" and "stdout" in r else None),
            "preview": (text or "")[:PREVIEW_CAP], "agent": r.get("agentId")}
