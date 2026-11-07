"""Which git repo a path belongs to and which branch that repo is on, read from `.git` directly."""

from __future__ import annotations

from pathlib import Path

_roots: dict[str, str] = {}  # only hits are cached: a missing dir may be created or cloned later


def repo_root(directory: str) -> str | None:
    """Closest ancestor of `directory` (itself included) holding `.git`: a dir, or a file in worktrees."""
    if directory in _roots:
        return _roots[directory]
    p = Path(directory)
    for d in (p, *p.parents):
        if (d / ".git").exists():
            if len(_roots) < 10_000:
                _roots[directory] = str(d)
            return str(d)
    return None


def repo_of(path: str) -> str | None:
    """Repo of a file or directory path; the file need not exist yet."""
    p = Path(path)
    return repo_root(str(p if p.is_dir() else p.parent))


def missing_root(path: str) -> str:
    """Topmost ancestor of `path` that no longer exists, e.g. a removed worktree's directory."""
    p = Path(path)
    top = p
    for d in p.parents:
        if d.exists():
            return str(top)
        top = d
    return str(top)


def _git_dir(root: str) -> Path | None:
    dot_git = Path(root) / ".git"
    if dot_git.is_dir():
        return dot_git
    try:  # worktree or submodule: ".git" is a file "gitdir: <path>"
        text = dot_git.read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    git_dir = Path(text.split(":", 1)[1].strip())
    return git_dir if git_dir.is_absolute() else Path(root) / git_dir


def current_branch(root: str) -> str | None:
    """Branch checked out in the repo at `root`; `detached <sha>` when HEAD isn't a branch."""
    git_dir = _git_dir(root)
    try:
        head = (git_dir / "HEAD").read_text().strip() if git_dir else ""
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head.removeprefix("ref: refs/heads/")
    return f"detached {head[:7]}" if head else None
