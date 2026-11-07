"""`harness-watch install-agent`: a LaunchAgent that starts the server at login and restarts it if
it stops. It runs the same `harness-watch` command that installed it, with the HARNESS_WATCH_*
and CLAUDE_CONFIG_DIR variables set at install time; install again after changing them.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

LABEL = "harness-watch"
# launchd has no shell PATH: git, ps, osascript and security, plus Homebrew's tools
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KEPT = ("CLAUDE_CONFIG_DIR",)
PREFIX = "HARNESS_WATCH_"


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def definition(command: str, environ: dict[str, str], home: Path) -> dict:
    env = {"PATH": PATH, **{k: v for k, v in sorted(environ.items()) if k.startswith(PREFIX) or k in KEPT}}
    log = str(home / "Library" / "Logs" / f"{LABEL}.log")
    return {"Label": LABEL, "ProgramArguments": [command, "serve"], "EnvironmentVariables": env,
            "RunAtLoad": True, "KeepAlive": True, "StandardOutPath": log, "StandardErrorPath": log}


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(command: str | None = None, environ=None, home: Path | None = None, launchctl=_launchctl) -> str:
    home = home or Path.home()
    command = command or os.path.abspath(sys.argv[0])
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(definition(command, dict(os.environ if environ is None else environ), home)))
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")  # an older definition: launchd keeps the one it loaded
    for _ in range(10):  # right after a bootout, bootstrap can fail for a moment ("Input/output error")
        r = launchctl("bootstrap", domain, str(path))
        if r.returncode == 0:
            return f"installed {path}; it runs `{command} serve` now and at every login. Open the page: harness-watch open"
        time.sleep(0.5)
    raise SystemExit(f"launchctl bootstrap failed: {r.stderr.strip()}")


def uninstall(home: Path | None = None, launchctl=_launchctl) -> str:
    path = plist_path(home)
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if path.exists():
        path.unlink()
        return f"stopped and removed {path}"
    return "no agent installed"
