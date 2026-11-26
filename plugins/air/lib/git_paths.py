"""Canonical decoding of the file paths git prints in diff output.

One implementation, so every consumer (today: the headless tool sandbox's output
guard) agrees on what path a diff header names. Handles:

  * quoted paths with C-style escapes (git quotes any path with a control char,
    a double quote, a backslash, or — by default — a non-ASCII byte, using octal
    escapes for the raw UTF-8 bytes: `"a/caf\\303\\251/x"`);
  * paths containing spaces (an unquoted `diff --git a/my dir/x b/my dir/x`);
  * renames / copies (`rename from|to`, `copy from|to`, and a header whose two
    sides differ);
  * combined-diff headers (`diff --cc <path>` / `diff --combined <path>`);
  * `--- a/x` / `+++ b/x` lines (with git's trailing tab for spaced paths) and
    `/dev/null`;
  * `Binary files a/x and b/y differ`.

`iter_diff_paths` is deliberately liberal: it strips ANSI colour escapes and any
leading `--graph` decoration before matching, and for an AMBIGUOUS header (spaces
plus a rename) it yields every plausible reading — a caller screening for
sensitive paths wants over-matching, never under-matching.

stdlib-only (the lib/ rule).
"""
import re
from typing import Iterator

_ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]")
# `git log --graph` prefixes every line with `| `, `* `, `|/`, `|\`, ... Strip a
# leading run of those decoration characters and spaces.
_GRAPH_PREFIX_RE = re.compile(r"^[|*\\/ ]+")

_SIMPLE_ESCAPES = {
    "a": 0x07, "b": 0x08, "f": 0x0C, "n": 0x0A, "r": 0x0D, "t": 0x09, "v": 0x0B,
    "\\": 0x5C, '"': 0x22,
}


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI escape sequences (colour codes git adds under --color)."""
    return _ANSI_RE.sub("", text)


def _read_quoted(s: str, start: int) -> tuple[str, int] | None:
    """Decode the C-style quoted string beginning at s[start] == '"'. Returns
    (decoded, index_after_closing_quote) or None if unterminated."""
    assert s[start] == '"'
    out = bytearray()
    i = start + 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            return out.decode("utf-8", errors="replace"), i + 1
        if c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in "01234567":
                j = i + 1
                digits = ""
                while j < n and len(digits) < 3 and s[j] in "01234567":
                    digits += s[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            if nxt in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[nxt])
                i += 2
                continue
            # Unknown escape: keep the character itself (never drop data).
            out.extend(nxt.encode("utf-8"))
            i += 2
            continue
        out.extend(c.encode("utf-8"))
        i += 1
    return None


def unquote_path(s: str) -> str:
    """Decode a git path token. A token wrapped in double quotes is C-unquoted
    (octal escapes → UTF-8); anything else is returned unchanged."""
    s = s.strip("\r\n")
    if len(s) >= 2 and s[0] == '"':
        got = _read_quoted(s, 0)
        if got is not None:
            return got[0]
    return s


def strip_prefix(path: str) -> str:
    """Drop the leading `a/` / `b/` side prefix git adds to diff paths."""
    if path[:2] in ("a/", "b/"):
        return path[2:]
    return path


def _candidates_from_rest(rest: str) -> list[str]:
    """The two path tokens of a `diff --git` header (everything after
    `diff --git `), as bare paths (prefix stripped). Unambiguous in the common
    cases; for unquoted spaced paths with differing sides every plausible split
    is returned."""
    rest = rest.rstrip("\r\n")
    # First side quoted: `"a/..." <second>` (second quoted or plain).
    if rest.startswith('"'):
        got = _read_quoted(rest, 0)
        if got is None:
            return [strip_prefix(rest)]
        first, end = got
        tail = rest[end:].lstrip(" ")
        out = [strip_prefix(first)]
        if tail:
            out.append(strip_prefix(unquote_path(tail)))
        return out
    # First side plain, second quoted: `a/x "b/..."`. A plain path never holds a
    # double quote (git would have quoted it), so the first ` "` is the boundary.
    q = rest.find(' "')
    if q != -1:
        return [strip_prefix(rest[:q]), strip_prefix(unquote_path(rest[q + 1:]))]
    # Both plain. Equal sides (the non-rename case): `a/P b/P` → P, found by length
    # so a space inside P cannot confuse the split.
    if len(rest) % 2 == 1:
        half = (len(rest) - 1) // 2
        left, sep, right = rest[:half], rest[half], rest[half + 1:]
        if sep == " " and left.startswith("a/") and right.startswith("b/") \
                and left[2:] == right[2:]:
            return [left[2:]]
    # Differing sides with spaces (a rename): every ` b/` boundary is a candidate.
    out: list[str] = []
    for m in re.finditer(r" b/", rest):
        a_side, b_side = rest[:m.start()], rest[m.start() + 1:]
        if a_side.startswith("a/"):
            out.extend([a_side[2:], b_side[2:]])
    return out or [strip_prefix(rest)]


_RENAME_RE = re.compile(r"^(?:rename|copy) (?:from|to) (.*)$")
_BINARY_RE = re.compile(r"^Binary files (.*) and (.*) differ\s*$")


def parse_diff_header(line: str) -> list[str]:
    """Paths named by ONE header-like line of git diff output, decoded and with
    the `a/`/`b/` prefix removed. Returns [] for a line that is not a path-bearing
    header (or only names /dev/null)."""
    line = line.rstrip("\r\n")

    if line.startswith("diff --git "):
        paths = _candidates_from_rest(line[len("diff --git "):])
    elif line.startswith("diff --cc ") or line.startswith("diff --combined "):
        rest = line.split(" ", 2)[2]
        paths = [unquote_path(rest)]
    elif line.startswith("--- ") or line.startswith("+++ "):
        # Trailing tab: git appends it to ---/+++ paths containing spaces.
        rest = line[4:].split("\t", 1)[0]
        paths = [strip_prefix(unquote_path(rest))]
    else:
        m = _RENAME_RE.match(line)
        if m:
            paths = [unquote_path(m.group(1))]
        else:
            b = _BINARY_RE.match(line)
            if not b:
                return []
            paths = [strip_prefix(unquote_path(b.group(1))), strip_prefix(unquote_path(b.group(2)))]

    return [p for p in paths if p and p != "/dev/null"]


def iter_diff_paths(text: str) -> Iterator[str]:
    """Every path named by any header-like line in `text` (git diff / log -p /
    show output). Tolerant of ANSI colour and `--graph` line decoration."""
    for raw in strip_ansi(text).splitlines():
        stripped = _GRAPH_PREFIX_RE.sub("", raw)
        yield from parse_diff_header(raw)
        if stripped != raw:
            yield from parse_diff_header(stripped)


def any_path(text: str, predicate) -> str | None:
    """First path in `text`'s diff headers for which predicate(path) is true, else None."""
    for p in iter_diff_paths(text):
        if predicate(p):
            return p
    return None
