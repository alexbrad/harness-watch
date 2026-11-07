# harness-watch: plan

A local web page (http://127.0.0.1:8787, or a name of your own through the relay) that lists every live Claude Code session on this Mac and lets you explore each one. Clicking a session focuses its iTerm2 tab. Why it is built this way: [decision-log.md](decision-log.md).

## What it shows

### Live session list
One row per running `claude` process. Sessions close → row disappears.
- Name, start dir, current dir (highlighted when it differs from the start dir), git branch, model.
- Status: **idle** sessions sort to the top, bold with a clear marker and an "idle for N min" timer; busy sessions below.
- Context size (tokens and % of the window), total tokens, estimated cost.
- Running / total sub-agents; running background shells.
- Last user prompt (truncated).
- Warning badge when the session is not running in iTerm2 (focus unavailable).
- Plan usage: per account, a bar for each limit (current 5-hour session, weekly, weekly per model) with % used and time to reset, as `/usage` shows them (step 8).

### Session detail (click a row = focus iTerm2 + open detail)
- Overview: the list fields, plus cwd history (every directory the session moved through, with times) and files edited.
- Sub-agents: type, description, running/finished, tokens; each opens its own tool history.
- Shells: background shells with live output tails; current foreground child processes.
- Tool-use explorer, two views over the same events (main session + sub-agents):
  - Grouped: Web (URLs grouped by domain, searches), Commands (Bash, with cwd and exit status), Files (read / edited / written, as a directory tree with counts), MCP calls, Sub-agent launches, Other.
  - Timeline: chronological, filterable by category and text.
- Flow: a lane per agent (sub-agents under the agent that started them) on a shared time axis, bars for when each worked, a line for every message between two agents. Clicking a line lists every message between that pair; clicking a lane limits the tool-use explorer to that agent. The time axis zooms (pinch, ⌘/Ctrl + scroll, −/Fit/+ buttons, or a drag across a stretch); lane names stay in view while it scrolls sideways.
- Fork button: asks for a name, then opens a new iTerm2 tab running `claude --resume <id> --fork-session --name <name>` from the session's start dir.

### Session browser
All transcripts still on disk (Claude Code keeps 30 days). Filter by dir, date and name, plus text search. Opens the same detail view read-only (no live parts). Fork available.

## Data sources
- `~/.claude/sessions/<pid>.json`: live registry (sessionId, cwd, name, status, statusUpdatedAt), checked against `ps`.
- `ps`: TTY, child processes.
- `~/.claude/projects/<dir>/<sessionId>.jsonl`: transcript (usage, model, cwd, gitBranch, tool_use / tool_result).
- `~/.claude/projects/<dir>/<sessionId>/subagents/agent-*.{jsonl,meta.json}`: sub-agents.
- `/private/tmp/claude-501/<dir>/<sessionId>/tasks/*.output`: background shell output.
- iTerm2 AppleScript: tty → window/tab/session, for focus and fork.
- Plan usage (step 8): the `rate_limits` Claude Code pipes to the status line command, which `harness-watch-statusline` merges into `~/Library/Caches/harness-watch/rate-limits/<account>.json`. `HARNESS_WATCH_USAGE_SOURCE` can name another source, such as one reading Claude Code's usage endpoint with the Keychain login (kept out of this repo: see the decision log, 2026-11-04).

All readers sit behind one module and fail soft: an unknown format shows "unavailable", never crashes the page.

## Stack
Python 3.13, uv, FastAPI + uvicorn; pushes updates to the page over server-sent events. Frontend: one static HTML page with vanilla JS, no build step. pytest with fixture transcripts. Bound to 127.0.0.1 only; action endpoints check `Origin`.

## Steps
Each step is its own commit; steps needing a look in the browser stop for sign-off.
1. Skeleton + live list: project setup, registry reader, `ps` liveness and TTY, list page with idle-first sorting and the non-iTerm warning. *(manual check)*
2. Focus: click → AppleScript selects the matching iTerm2 window/tab/session. *(manual check)*
3. Transcript stats: incremental tail reader; tokens, context %, cost, model, branch, cwd history, last prompt, files edited. *(manual check)*
4. Sub-agents and shells (done 2026-10-30): sub-agent list with tokens and cost, added to the session totals; background shells and monitors with live output tail; commands running now and other child processes. *(manual check)*
5. Tool-use explorer (done 2026-10-30): every tool call of the session and its sub-agents, stored in the index while it reads transcripts; Grouped (by category, then domain / program / tool / MCP server / agent type) and Timeline views, category chips, text filter, paging. *(manual check)*
5b. Flow (done 2026-11-02): messages between the session and its sub-agents (prompts, SendMessage, idle notices, hand-backs, results, finish notices) read where they arrive and stored in the index; lanes with activity spans, quiet stretches cut out of the axis; per-pair message list; the explorer filters by agent. *(manual check)*
6. Fork (done 2026-11-02): a Fork button on live and closed sessions asks for the fork's name (prefilled "<title> (fork)"), then opens a new iTerm2 tab (next to the session's own tab when it runs in iTerm2) whose process is a script in your login shell (fish, zsh or bash; other shells get zsh): `cd <start dir>; and claude --resume <id> --fork-session --name <name>` (`&&` outside fish), then a login shell in that dir. Through your shell, so a `claude` wrapper (the account per folder) applies; a removed start dir is reported instead. *(manual check)*
7. Session browser (done before 4–6, 2026-10-30): local SQLite index of every transcript on disk, re-reading only files whose size or mtime changed (and from where it stopped); "All sessions" view, newest first, paginated; the same side panel for closed sessions. The live list resumes from the index too, so restarts are fast. Filters and search later. *(manual check)*
8. Plan usage (2026-11-03): bars under the header for every logged-in account (default dir and each `~/.claude-*`), read every 5 minutes and right after a window resets; an expired login is never refreshed, only reported. *(manual check)* Since 2026-11-04 from the status line by default, the Keychain reading moved to a private source file loaded through `HARNESS_WATCH_USAGE_SOURCE`.
9. Always on (2026-11-04): `harness-watch install-agent` writes a LaunchAgent that runs the server at login and restarts it if it stops; optionally a root LaunchDaemon `harness-watch.relay` adds 127.0.0.2 to loopback and relays its port 80 to it (`relay/relay.py`; a pf forward until 2026-11-04, see the decision log), so a name in `/etc/hosts` (`HARNESS_WATCH_HOSTNAME`) opens the page without a port.
