import os

import pytest

from harness_watch import shellparse


@pytest.fixture
def root(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    for f in ("src/a.py", "src/b.py", "src/pkg/c.py", "notes.md", "with space.txt", "*.py"):
        (tmp_path / f).write_text("x\n")
    return str(tmp_path)


def parse(cmd, root):
    return shellparse.parse(cmd, root)


def test_reads_lists_and_cd(root):
    act = parse("cd src && sed -n '1,20p' a.py | head -5; grep -rn TODO . 2>/dev/null; ls", root)
    assert act.read == [f"{root}/src/a.py"]
    assert act.listed == [f"{root}/src", f"{root}/src"]


def test_globs_and_braces_expand_like_zsh(root):
    act = parse("head -n 3 src/*.py; wc -l src/{a,b}.py; cat src/**/*.py", root)
    a, b, c = (f"{root}/src/{f}" for f in ("a.py", "b.py", "pkg/c.py"))
    assert act.read == [a, b, a, b, a, b, c]


def test_quoted_globs_and_patterns_are_not_paths(root):
    act = parse("grep -n notes.md notes.md; grep -rn '*.py' src; cat '*.py'", root)
    # grep's first operand is its pattern; a quoted * is literal, so only the file named "*.py" counts.
    assert act.read == [f"{root}/notes.md", f"{root}/*.py"]
    assert act.listed == [f"{root}/src"]


def test_pattern_given_by_option(root):
    act = parse("grep -e x notes.md src/a.py", root)
    assert act.read == [f"{root}/notes.md", f"{root}/src/a.py"]


def test_variables_set_in_the_command_are_followed(root):
    act = parse('F=notes.md; cat "$F"; D=src; ls ${D}; cat "$UNSET_THING" $(echo src/a.py)', root)
    assert act.read == [f"{root}/notes.md"]
    assert act.listed == [f"{root}/src"]


def test_quoting_and_escapes(root):
    act = parse("cat 'with space.txt' with\\ space.txt \"with space.txt\"", root)
    assert act.read == [f"{root}/with space.txt"] * 3


def test_heredoc_body_is_not_parsed_as_commands(root):
    act = parse("cat > out.txt <<'EOF'\ncat notes.md\nls src\nEOF\nhead notes.md", root)
    assert act.written == [f"{root}/out.txt"]
    assert act.read == [f"{root}/notes.md"]
    assert act.listed == []


def test_writes_from_redirects_sed_in_place_tee_cp(root):
    act = parse("echo x > out.txt 2>&1; sed -i '' 's/a/b/' notes.md; echo y | tee -a log.txt; "
                "cat src/a.py >/dev/null; cp src/a.py src/pkg", root)
    assert act.written == [f"{root}/out.txt", f"{root}/notes.md", f"{root}/log.txt", f"{root}/src/pkg/a.py"]
    assert act.read == [f"{root}/src/a.py", f"{root}/src/a.py"]


def test_find_scripts_and_env_prefixes(root):
    act = parse("LC_ALL=C find src -name '*.py'\nsudo wc -l notes.md; python3 -u src/a.py --flag", root)
    assert act.listed == [f"{root}/src"]
    assert act.read == [f"{root}/notes.md", f"{root}/src/a.py"]


def test_comments_and_unmatched_globs(root):
    act = parse("# cat notes.md\ncat nothing*.zz notes.md  # trailing\nhead notes.md", root)
    assert act.read == [f"{root}/notes.md"]  # zsh skips the cat ("no matches found"), runs the head


def test_never_executes_anything(root, tmp_path):
    marker = tmp_path / "ran"
    parse(f"cat $(touch {marker}) `touch {marker}`; touch {marker}2", root)
    assert not marker.exists() and not (tmp_path / "ran2").exists()


def test_bad_input(root):
    assert parse("echo 'unbalanced", root).read == []
    assert parse("ls", None) == shellparse.Activity()
