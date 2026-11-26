"""Unit tests for git_paths — the canonical decoder of paths in git diff headers."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import git_paths  # noqa: E402
from git_paths import iter_diff_paths, parse_diff_header, unquote_path  # noqa: E402


@pytest.mark.parametrize("line,expected", [
    ("diff --git a/.env b/.env", [".env"]),
    ("diff --git a/src/app.py b/src/app.py", ["src/app.py"]),
    # spaces in the folder name (unquoted)
    ("diff --git a/my dir/.env b/my dir/.env", ["my dir/.env"]),
    # quoted Unicode path with octal escapes
    ('diff --git "a/caf\\303\\251/.env" "b/caf\\303\\251/.env"', ["café/.env", "café/.env"]),
    # rename: two different sides, one quoted
    ('diff --git a/old.txt "b/n\\303\\251w.txt"', ["old.txt", "néw.txt"]),
    # rename with spaces on both sides
    ("diff --git a/x y/a.txt b/z w/b.txt", ["x y/a.txt", "z w/b.txt"]),
    # combined-merge headers carry a single, unprefixed path
    ("diff --cc .env", [".env"]),
    ("diff --combined my dir/.env", ["my dir/.env"]),
    ('diff --cc "caf\\303\\251/.env"', ["café/.env"]),
    ("rename from old/.env", ["old/.env"]),
    ("rename to new/.env", ["new/.env"]),
    ("copy from a b/.env", ["a b/.env"]),
    ("copy to c/.env", ["c/.env"]),
    ("--- a/.env", [".env"]),
    ("+++ b/.env", [".env"]),
    ("--- a/my dir/.env\t", ["my dir/.env"]),
    ('+++ "b/caf\\303\\251/.env"', ["café/.env"]),
    ("--- /dev/null", []),
    ("+++ /dev/null", []),
    ("Binary files a/x.bin and b/y.bin differ", ["x.bin", "y.bin"]),
    ("Binary files /dev/null and b/y.bin differ", ["y.bin"]),
])
def test_parse_diff_header(line, expected):
    assert parse_diff_header(line) == expected


@pytest.mark.parametrize("line", [
    "", "index 83db48f..bf2a3a1 100644", "@@ -1,3 +1,4 @@", "+TOKEN=1", " context line",
    "    diff --git is mentioned in a commit message body",
    "similarity index 90%", "new file mode 100644",
])
def test_parse_diff_header_ignores_non_headers(line):
    assert parse_diff_header(line) == []


def test_unquote_path_escapes():
    assert unquote_path('"a\\tb"') == "a\tb"
    assert unquote_path('"q\\"uote"') == 'q"uote'
    assert unquote_path('"back\\\\slash"') == "back\\slash"
    assert unquote_path('"\\346\\227\\245\\346\\234\\254"') == "日本"
    assert unquote_path("plain/path") == "plain/path"
    assert unquote_path('"unterminated') == '"unterminated'


def test_iter_diff_paths_handles_ansi_and_graph_decoration():
    text = (
        "\x1b[1mdiff --git a/.env b/.env\x1b[m\n"
        "\x1b[1m--- a/.env\x1b[m\n"
        "| * diff --git a/my dir/.env b/my dir/.env\n"
        "|/  +++ b/graph/.env\n"
        "plain content line\n"
    )
    found = list(iter_diff_paths(text))
    assert ".env" in found
    assert "my dir/.env" in found
    assert "graph/.env" in found


def test_iter_diff_paths_full_diff_sample():
    text = (
        "diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/old.cfg b/new.cfg\nsimilarity index 90%\nrename from old.cfg\nrename to new.cfg\n"
        "diff --cc merged.txt\n--- a/merged.txt\n+++ b/merged.txt\n"
    )
    found = set(iter_diff_paths(text))
    assert found == {"src/a.py", "old.cfg", "new.cfg", "merged.txt"}


def test_any_path_predicate():
    text = "diff --git a/ok.py b/ok.py\ndiff --git a/my dir/.env b/my dir/.env\n"
    assert git_paths.any_path(text, lambda p: p.endswith(".env")) == "my dir/.env"
    assert git_paths.any_path(text, lambda p: p.endswith(".pem")) is None
