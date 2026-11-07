"""Which files a Bash tool command read, listed or wrote, worked out without running anything.

Sessions read most files with shell commands (`grep`, `sed -n`, `head`, `ls`), not the Read
tool. The command is tokenized like a shell would (quotes, escapes, heredocs, `cd`), and words
are expanded in-process: `~`, variables assigned earlier in the same command, `{a,b}` braces and
globs (via `glob`, which only lists directories). Nothing is executed. What can't be known
without running something is skipped, not guessed: `$(...)`, unknown variables, `xargs` input,
files opened by scripts. Paths are checked against the filesystem when the transcript is read,
which for a live session is within a second of the command.
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field

READ_CMDS = {
    "cat", "head", "tail", "less", "more", "sed", "awk", "grep", "egrep", "fgrep", "rg", "wc", "nl",
    "diff", "cmp", "jq", "sort", "uniq", "cut", "file", "stat", "bat", "xxd", "od", "strings", "md5",
    "shasum", "source", ".",
}
PATTERN_FIRST = {"grep", "egrep", "fgrep", "rg", "sed", "awk"}  # first operand is the pattern/script
PATTERN_OPTS = {"-e", "-f", "--regexp", "--file", "--expression"}  # ...unless given with these
RECURSIVE_SEARCH = {"grep", "egrep", "fgrep", "rg"}
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "fish", "perl", "ruby", "deno", "bun"}
LIST_CMDS = {"ls", "find", "tree", "du", "fd"}
WRAPPERS = {"sudo", "time", "nohup", "command", "builtin", "exec", "env", "caffeinate"}
OPERATORS = (";;", "&&", "||", "|&", ">>", "<<<", "<<-", "<<", ">&", "<&", "&>>", "&>", ">|", ";", "&", "|", "(", ")", ">", "<")
SEPARATORS = {";", "&&", "||", "|", "|&", "&", "(", ")", ";;", "\n"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<", ">&", "<&", "<<<"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass
class Activity:
    read: list[str] = field(default_factory=list)
    listed: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)


class Unknown(Exception):
    """A word whose value depends on running something (or on state we don't have)."""


class NoMatch(Unknown):
    """A glob matching nothing: zsh reports "no matches found" and skips that command."""


def tokenize(command: str) -> list[str]:
    """Raw words (quotes kept) and operators; heredoc bodies and comments are dropped."""
    tokens: list[str] = []
    heredocs: list[str] = []  # delimiters whose bodies start after the current line
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c == "\n":
            tokens.append("\n")
            i += 1
            for delim in heredocs:  # skip each body up to its delimiter line
                while i < n:
                    end = command.find("\n", i)
                    line = command[i:] if end < 0 else command[i:end]
                    i = n if end < 0 else end + 1
                    if line.strip() == delim:
                        break
            heredocs = []
            continue
        if c in " \t":
            i += 1
            continue
        if c == "#" and (not tokens or tokens[-1] in SEPARATORS or command[i - 1] in " \t"):
            while i < n and command[i] != "\n":
                i += 1
            continue
        op = next((o for o in OPERATORS if command.startswith(o, i)), None)
        if op:
            tokens.append(op)
            i += len(op)
            continue
        start = i
        i = _word_end(command, i)
        word = command[start:i]
        if word.isdigit() and i < n and command[i] in "<>":
            continue  # fd number of "2>file"
        if len(tokens) and tokens[-1] in {"<<", "<<-"}:
            heredocs.append(_unquote_simple(word))
        tokens.append(word)
    return tokens


def _word_end(s: str, i: int) -> int:
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t\n;&|()<>":
            return i
        if c == "\\":
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
        elif c == "`":
            j = s.find("`", i + 1)
            i = n if j < 0 else j + 1
        elif s.startswith("$(", i):
            depth, i = 1, i + 2
            while i < n and depth:
                depth += {"(": 1, ")": -1}.get(s[i], 0)
                i += 1
        else:
            i += 1
    return i


def _unquote_simple(word: str) -> str:
    return word.replace("'", "").replace('"', "").replace("\\", "")


def _braces(word: str) -> list[str]:
    """Bash brace expansion for unquoted words: a{b,c}d, {1..3}."""
    m = re.search(r"\{([^{}]*)\}", word)
    if not m or any(q in word for q in "'\""):
        return [word]
    body = m.group(1)
    rng = re.fullmatch(r"(-?\d+)\.\.(-?\d+)", body)
    if rng:
        a, b = int(rng.group(1)), int(rng.group(2))
        items = [str(x) for x in (range(a, b + 1) if a <= b else range(a, b - 1, -1))]
    elif "," in body:
        items = body.split(",")
    else:
        return [word]
    return [w for item in items for w in _braces(word[: m.start()] + item + word[m.end():])]


def expand(word: str, env: dict[str, str], cwd: str) -> list[str]:
    """Expand one raw word into the arguments the shell would pass. Raises Unknown."""
    out = []
    for w in _braces(word):
        text, pattern, has_glob = _expand_quotes(w, env)
        if text.startswith("~"):
            home = os.path.expanduser("~")
            if text == "~" or text.startswith("~/"):
                text, pattern = home + text[1:], glob.escape(home) + pattern[1:]
        if has_glob:
            base = pattern if os.path.isabs(text) else os.path.join(glob.escape(cwd), pattern)
            matches = _glob(base)
            if not matches:
                raise NoMatch(word)
            out.extend(m if os.path.isabs(text) else os.path.relpath(m, cwd) for m in matches)
        else:
            out.append(text)
    return out


GLOB_LIMIT, GLOB_SECONDS = 5000, 0.25


def _glob(pattern: str) -> list[str]:
    """Matches of a pattern; Unknown when it's too big to expand quickly (e.g. `~/**/*`)."""
    deadline = time.monotonic() + GLOB_SECONDS
    matches = []
    for m in glob.iglob(pattern, recursive=True):
        matches.append(m)
        if len(matches) > GLOB_LIMIT or time.monotonic() > deadline:
            raise Unknown(pattern)
    return sorted(matches)


def _expand_quotes(w: str, env: dict[str, str]) -> tuple[str, str, bool]:
    """(literal text, glob pattern with quoted parts escaped, whether an unquoted glob char appears)."""
    text, pattern, has_glob = [], [], False
    i, n, in_double = 0, len(w), False

    def lit(s: str) -> None:
        text.append(s)
        pattern.append(glob.escape(s))

    while i < n:
        c = w[i]
        if c == "'" and not in_double:
            j = w.find("'", i + 1)
            lit(w[i + 1 : j if j >= 0 else n])
            i = n if j < 0 else j + 1
        elif c == '"':
            in_double = not in_double
            i += 1
        elif c == "\\" and i + 1 < n:
            lit(w[i + 1])
            i += 2
        elif c == "`" or w.startswith("$(", i):
            raise Unknown(w)
        elif c == "$":
            m = re.match(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", w[i:])
            if not m:
                raise Unknown(w)  # $1, $?, ${x:-y}, ...
            name = m.group(1) or m.group(2)
            if name not in env:
                raise Unknown(w)
            lit(env[name])
            i += m.end()
        elif c in "*?[" and not in_double:
            text.append(c)
            pattern.append(c)
            has_glob = True
            i += 1
        else:
            lit(c)
            i += 1
    return "".join(text), "".join(pattern), has_glob


def _commands(tokens: list[str]) -> list[list[str]]:
    commands, current = [], []
    for t in tokens:
        if t in SEPARATORS:
            if current:
                commands.append(current)
            current = []
        else:
            current.append(t)
    if current:
        commands.append(current)
    return commands


def parse(command: str, cwd: str | None) -> Activity:
    out = Activity()
    if not command or not cwd:
        return out
    env = {"HOME": os.path.expanduser("~")}
    for words in _commands(tokenize(command)):
        cwd = _run_one(words, cwd, env, out)
    return out


def _run_one(raw: list[str], cwd: str, env: dict[str, str], out: Activity) -> str:
    """Account for one simple command; returns the cwd after it."""
    words: list[str] = []
    i = 0
    while i < len(raw):
        w = raw[i]
        if w in REDIRECTS or w in {"<<", "<<-"}:
            target = raw[i + 1] if i + 1 < len(raw) else ""
            i += 2
            if w in {"<<", "<<-", "<<<", ">&", "<&"}:
                continue
            for path in _paths(target, cwd, env):
                if not path.startswith("/dev/"):
                    (out.read if w == "<" else out.written).append(path)
            continue
        words.append(w)
        i += 1
    # Leading VAR=value: a bare assignment sets it for later commands; before a command, only for it.
    while words and ASSIGNMENT.match(words[0]):
        name, _, value = words.pop(0).partition("=")
        try:
            expanded = expand(value, env, cwd)
            if len(expanded) == 1:
                env[name] = expanded[0]
        except Unknown:
            env.pop(name, None)
    while words and words[0] in WRAPPERS:
        words = words[1:]
    if not words:
        return cwd
    if words[0] == "export":
        for w in words[1:]:
            if ASSIGNMENT.match(w):
                _run_one([w], cwd, env, out)
        return cwd
    name = os.path.basename(_unquote_simple(words[0]))
    args: list[str] = []
    for w in words[1:]:
        try:
            args.extend(expand(w, env, cwd))
        except NoMatch:
            return cwd  # the command never ran
        except Unknown:
            args.append("\0")  # keeps positions, never a path
    if name == "cd":
        target = args[0] if args else env["HOME"]
        path = _abs(target, cwd)
        return path if path and os.path.isdir(path) else cwd
    if name in READ_CMDS:
        _reads(name, args, cwd, out)
    elif name in LIST_CMDS:
        _lists(name, args, cwd, out)
    elif name in INTERPRETERS:
        script = next((a for a in args if not a.startswith("-")), None)
        path = _abs(script, cwd) if script else None
        if path and os.path.isfile(path):
            out.read.append(path)
    elif name in {"touch", "tee"}:
        out.written.extend(p for a in args if not a.startswith("-") and (p := _abs(a, cwd)))
    elif name in {"cp", "mv"}:
        operands = [a for a in args if not a.startswith("-")]
        if len(operands) >= 2:
            dest = _abs(operands[-1], cwd)
            for src in operands[:-1]:
                s = _abs(src, cwd)
                if not s or not dest:
                    continue
                if name == "cp" and os.path.isfile(s):
                    out.read.append(s)
                out.written.append(os.path.join(dest, os.path.basename(s)) if os.path.isdir(dest) else dest)
    return cwd


def _reads(name: str, args: list[str], cwd: str, out: Activity) -> None:
    in_place = name == "sed" and any(a == "-i" or (a.startswith("-i") and not a.startswith("--")) for a in args)
    skip_pattern = name in PATTERN_FIRST and not any(a in PATTERN_OPTS for a in args)
    operands = 0
    after_dashdash = False
    for a in args:
        if not after_dashdash and a == "--":
            after_dashdash = True
            continue
        if a.startswith("-") and not after_dashdash:
            continue
        if skip_pattern:
            skip_pattern = False
            continue
        operands += 1
        path = _abs(a, cwd)
        if path is None:
            continue
        if os.path.isfile(path):
            (out.written if in_place else out.read).append(path)
        elif os.path.isdir(path) and name in RECURSIVE_SEARCH:
            out.listed.append(path)
    if name == "rg" and operands == 0:
        out.listed.append(cwd)  # rg with only a pattern searches the cwd


def _lists(name: str, args: list[str], cwd: str, out: Activity) -> None:
    paths = []
    for a in args:
        if a.startswith("-") or a in {"!", "("}:
            if name == "find":
                break  # predicates follow the start paths
            continue
        path = _abs(a, cwd)
        if path and os.path.isdir(path):
            paths.append(path)
        elif path and os.path.isfile(path) and name == "ls":
            pass  # `ls file` shows metadata, reads nothing
    if not paths and not any(not a.startswith("-") for a in args):
        paths = [cwd]
    out.listed.extend(paths)


def _paths(word: str, cwd: str, env: dict[str, str]) -> list[str]:
    try:
        return [p for w in expand(word, env, cwd) if (p := _abs(w, cwd))]
    except Unknown:
        return []


def _abs(word: str | None, cwd: str) -> str | None:
    if not word or "\0" in word:
        return None
    return os.path.normpath(word if os.path.isabs(word) else os.path.join(cwd, word))
