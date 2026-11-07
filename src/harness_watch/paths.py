"""Where harness-watch keeps its own files. Standard library only: the status line imports it."""

from pathlib import Path

CACHE = Path.home() / "Library" / "Caches" / "harness-watch"  # all rebuilt if macOS purges it
DATA = Path.home() / "Library" / "Application Support" / "harness-watch"  # kept: the page's key
