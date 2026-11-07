# harness-watch

A local web page that lists every coding-agent session running on your Mac, live, and lets you dig into each one: what it is doing, what it costs, which files and commands it touched, and how its sub-agents talk to each other.

**Only Claude Code is supported as the agent harness for now.** harness-watch is an independent project, not made or endorsed by Anthropic.

## What it shows

- **Live sessions**: status (busy, idle, or waiting for you, with what for), time since the last reply, directory and git branch, context use, tokens, model, the last prompt, and the sub-agents, background shells and commands running now. Sessions waiting on you sort to the top.
- **Plan usage**: a bar per limit (5-hour and weekly) for each account, with the time to reset.
- **Session detail**: the directories a session moved through, files read and edited, sub-agents and background shells with their output, and every tool call, grouped (web, commands, files, MCP, sub-agents) or as a timeline.
- **Flow**: a lane per agent on a shared time axis, with a line for every message between two agents.
- **All sessions**: every transcript still on disk, searchable, with the same detail view.
- **iTerm2**: clicking a session focuses its tab; **Fork** opens a new tab running `claude --resume <id> --fork-session` in the session's directory.

## Requirements

- macOS, Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Claude Code. harness-watch reads files Claude Code keeps in `~/.claude` (the session registry and transcripts); their format is internal and may change with a Claude Code update, so a part of the page may show "unavailable" until harness-watch catches up.
- iTerm2 for focus and fork (the rest works in any terminal). On the first focus or fork, macOS asks whether harness-watch may control iTerm2.

## Install

```sh
uv tool install git+https://github.com/alexbrad/harness-watch
harness-watch                    # serves http://127.0.0.1:8787
harness-watch open               # opens it, with this Mac's key (once per browser)
```

The page needs a key that only your account can read (`~/Library/Application Support/harness-watch/key`): `harness-watch open` hands it to your browser once, which keeps it for that address, and from then on the plain address works. For another browser, `harness-watch open --print` names a local file to open in it.

To keep it running (started at login, restarted if it stops):

```sh
harness-watch install-agent      # harness-watch uninstall-agent removes it
```

The agent keeps the `HARNESS_WATCH_*` and `CLAUDE_CONFIG_DIR` variables set when you run `install-agent`; run it again after changing them. Log: `~/Library/Logs/harness-watch.log`.

## Plan usage

Claude Code hands its status line command the plan's 5-hour and weekly limits (Pro, Max and Ultra plans, once a session has had a response). `harness-watch-statusline` records them for the page, then runs the status line you already have, if any, so it still shows. In the `settings.json` of each Claude config dir you use (`~/.claude`, plus every `CLAUDE_CONFIG_DIR`):

```json
"statusLine": {"type": "command", "command": "harness-watch-statusline '~/.claude/statusline.sh'"}
```

Leave out the quoted argument if you had no status line. The limits update only while a session runs; a reading older than 10 minutes shows its time.

`HARNESS_WATCH_USAGE_SOURCE=<file.py>` replaces this source with your own: the file's `source()` returns a callable giving the readings (their format is in `src/harness_watch/usage.py`).

## Settings

| Variable | Default | |
|---|---|---|
| `HARNESS_WATCH_PORT` | `8787` | port on 127.0.0.1 |
| `HARNESS_WATCH_HOSTNAME` | none | another name the page may be opened by (below) |
| `HARNESS_WATCH_USAGE_SOURCE` | the status line | file with another plan-usage source |
| `HARNESS_WATCH_DB` | `~/Library/Caches/harness-watch/index.sqlite3` | index of all transcripts, rebuilt if deleted |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code's config dir to read |

Several accounts: sessions started with another `CLAUDE_CONFIG_DIR` (for example `~/.claude-work`) are shown under that account name. The page reads one config dir's sessions; to see the sessions of several in one list, make the others symlinks to the first.

## A name without a port (optional)

To open the page as, say, `http://agents.test` instead of `http://127.0.0.1:8787`: a small relay, run by root, listens on port 80 of a second loopback address (127.0.0.2) and passes everything to harness-watch, so port 80 of `localhost` stays free for other things. It binds the port as root, then runs as `nobody`.

From a clone of this repo:

```sh
git clone https://github.com/alexbrad/harness-watch && cd harness-watch
sudo install -o root -g wheel -m 644 relay/relay.py /Library/PrivilegedHelperTools/harness-watch-relay.py
sudo install -o root -g wheel -m 644 relay/harness-watch.relay.plist /Library/LaunchDaemons/
sudo launchctl bootstrap system /Library/LaunchDaemons/harness-watch.relay.plist
echo "127.0.0.2 agents.test" | sudo tee -a /etc/hosts
HARNESS_WATCH_HOSTNAME=agents.test harness-watch install-agent
harness-watch open --host agents.test
```

After that, `agents.test` opens it (type `http://` the first time, or the browser takes it for a search). Names under `.localhost` don't work: browsers send them all to 127.0.0.1. Undo: `sudo launchctl bootout system/harness-watch.relay`, remove the two installed files and the `/etc/hosts` line, and `sudo ifconfig lo0 -alias 127.0.0.2`.

## Security

- **Other accounts on the Mac can't use it.** Any account can reach 127.0.0.1:8787, so every request for data or an action needs the page's key, kept in a file only you can read. `harness-watch open` checks that the server on the port holds the key before handing it over (another account could hold the port while harness-watch is down), and hands it through a file only you can read, never a command line or an address. The browser keeps it in storage for that exact address and port, which no other site or port can read. Not covered: another account taking the port during a restart while your page is open.
- **Other websites can't use it.** The server answers only requests addressed to its own host names (127.0.0.1, localhost, `HARNESS_WATCH_HOSTNAME`), so a site can't reach it through DNS rebinding; requests that act (focus, fork) must come from the page itself; and no other site may load the page in a frame.
- **What it touches.** The server listens on 127.0.0.1:8787 (the optional relay adds port 80 of 127.0.0.2) and sends nothing anywhere. It reads `~/.claude`, `ps` and the background shells' output files, and writes its cache (`~/Library/Caches/harness-watch`, readable by you only), the key (`~/Library/Application Support/harness-watch`), plus the LaunchAgent and its log when you run `install-agent`. Focus briefly tints the iTerm2 pane.
- **The usage source file** named by `HARNESS_WATCH_USAGE_SOURCE` runs as you, so it is refused unless you own it and its folder and nobody else can write to them.

## Development

```sh
git clone https://github.com/alexbrad/harness-watch && cd harness-watch
uv run harness-watch
uv run pytest
```

Design and decisions: [docs/plan.md](docs/plan.md), [docs/decision-log.md](docs/decision-log.md).

## License

MIT
