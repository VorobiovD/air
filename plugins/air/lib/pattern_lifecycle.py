#!/usr/bin/env python3
"""Deterministic author-pattern lifecycle operations.

Pure-stdlib port of the *mechanical* half of review.md Step 13 sub-steps
2/2.5: strengthening matched patterns and advancing clean counters. The
semantic half (creating new patterns, merging duplicates, capping prose,
archiving narratives) needs judgment and stays with /air:learn sessions.

This module is shared by managed/pattern_writer.py (memory-store writes
after each managed review) and — Phase 2 — the CLI flow. Keeping the
counter mechanics in code instead of LLM bash removes both the fragile
whole-line exact-string replacement and the prompt-injection write path
(review sessions mount the pattern store read-only).

Entry format (one line per pattern, as produced by the learn flows):

    - **<Name>** (<N>x: <refs> | last <S> PRs: <C> clean)[ (declining)]: <prose>
    - **<Name>** (1x: #80 | new): <prose>

Lifecycle rules (mirror review.md Step 13):
    strengthen: count+1, append PR ref, reset counter to "last 0 PRs: 0
                clean", drop "(declining)".
    clean:      seen+1/clean+1 for the author's non-matched active
                patterns; at 5 clean append "(declining)"; at 10 clean
                move the entry under the "(archived)" section marker.
    archived:   never strengthened, never counted (matches review.md
                "archived patterns stay permanently").
"""

import re
from typing import Iterable

# Header of a pattern entry. Prose after the colon may contain anything
# (including parentheses); the match anchors on the FIRST "(...)" group
# after the bold name.
_ENTRY_RE = re.compile(
    r"^- \*\*(?P<name>.+?)\*\* "
    r"\((?P<count>\d+)x: (?P<refs>[^|()]*?) \| "
    r"(?:last (?P<seen>\d+) PRs: (?P<clean>\d+) clean|new)\)"
    r"(?P<tag>(?: \([a-z, -]+\))*)"
    r"(?P<rest>:.*)$"
)

# Inline status tags that FREEZE an entry. Production wikis mark archived
# patterns with a suffix on the entry line itself — e.g.
# `... | last 29 PRs: 29 clean) (archived): ...` or
# `... clean) (declining, archival-eligible): ...` — not only with an
# "(archived)" section heading. Frozen entries are never strengthened and
# never clean-counted: the lifecycle contract says archived stays archived.
_FROZEN_TAG_RE = re.compile(r"\((?:archived|[a-z, ]*archival-eligible)\)")

# Annotations agents attach to findings. Archived matches are reported
# but never strengthen (lifecycle: archived stays archived).
ANNOTATION_RE = re.compile(
    r"\[matches (?P<kind>author|declining|archived) pattern: "
    r"(?P<name>.+?)(?: \(\d+x[^)]*\))?\]"
)

ARCHIVED_HEADING_RE = re.compile(r"^#{2,3} .*\(archived\)\s*$")

# Finding-title line shape in posted reviews: `**3. <title> ...**` — the
# only place the verifier emits author-pattern annotations.
_TITLE_LINE_RE = re.compile(r"^\s*\*\*\d+\.\s")

DECLINE_AT = 5
ARCHIVE_AT = 10


def _norm(name: str) -> str:
    return " ".join(name.casefold().split())


def extract_matched_patterns(review_body: str) -> set[str]:
    """Pattern names the review annotated as matched (author + declining;
    archived annotations are intentionally excluded).

    Injection containment: the review body embeds PR-derived text (quoted
    titles, code, finding prose) that an attacker can influence, so a bare
    body-wide regex would let a crafted PR echo an annotation and spuriously
    strengthen a real pattern. Two bounds: (1) only annotations on finding
    TITLE lines count — the verifier emits them as `**N. <title> [matches
    author pattern: X]**` — quoted attacker text inside finding prose or
    code fences never starts a line with the bold-number prefix; (2) the
    blast radius is inherently limited to strengthening EXISTING entries
    (apply_review only matches names already in the trusted file; creation
    is /air:learn's job). Callers should log each strengthen for audit.
    """
    out = set()
    for line in review_body.split("\n"):
        if not _TITLE_LINE_RE.match(line):
            continue
        for m in ANNOTATION_RE.finditer(line):
            if m.group("kind") in ("author", "declining"):
                out.add(_norm(m.group("name")))
    return out


def apply_review(author_md: str, pr_number: int,
                 matched: Iterable[str]) -> tuple[str, dict]:
    """Apply one review's lifecycle pass to an author's pattern file.

    Returns (updated_md, summary) where summary lists strengthened /
    cleaned / newly-declining / newly-archived pattern names. Lines that
    don't parse as pattern entries pass through untouched. Entries below
    an "(archived)" heading are never modified.
    """
    matched_norm = {_norm(m) for m in matched}
    summary = {"strengthened": [], "cleaned": [], "declining": [], "archived": []}
    out_lines: list[str] = []
    to_archive: list[str] = []
    in_archived = False

    for line in author_md.split("\n"):
        if ARCHIVED_HEADING_RE.match(line):
            in_archived = True
            out_lines.append(line)
            continue
        if line.startswith("#"):
            in_archived = False
            out_lines.append(line)
            continue
        m = _ENTRY_RE.match(line)
        if not m or in_archived:
            out_lines.append(line)
            continue
        tags = m.group("tag") or ""
        if _FROZEN_TAG_RE.search(tags):
            # Inline-archived / archival-eligible entry — pass through
            # untouched regardless of annotations (real-wiki shape; see
            # _FROZEN_TAG_RE).
            out_lines.append(line)
            continue

        name = m.group("name")
        rest = m.group("rest")
        if _norm(name) in matched_norm:
            count = int(m.group("count")) + 1
            refs = f"{m.group('refs').strip()}, #{pr_number}"
            # Strengthening drops status tags ((declining) etc.) by design.
            header = f"- **{name}** ({count}x: {refs} | last 0 PRs: 0 clean)"
            out_lines.append(header + rest)
            summary["strengthened"].append(name)
        else:
            if m.group("seen") is None:  # "| new)" form — starts counting
                seen, clean = 1, 1
            else:
                seen = int(m.group("seen")) + 1
                clean = int(m.group("clean")) + 1
            tag = ""
            if clean >= ARCHIVE_AT:
                summary["archived"].append(name)
                line_new = (f"- **{name}** ({m.group('count')}x: "
                            f"{m.group('refs').strip()} | last {seen} PRs: "
                            f"{clean} clean)" + rest)
                to_archive.append(line_new)
                continue
            if clean >= DECLINE_AT:
                tag = " (declining)"
                if not m.group("tag"):
                    summary["declining"].append(name)
            header = (f"- **{name}** ({m.group('count')}x: "
                      f"{m.group('refs').strip()} | last {seen} PRs: "
                      f"{clean} clean){tag}")
            out_lines.append(header + rest)
            summary["cleaned"].append(name)

    if to_archive:
        marker = None
        for i, line in enumerate(out_lines):
            if ARCHIVED_HEADING_RE.match(line):
                marker = i
                break
        if marker is None:
            out_lines.append("")
            out_lines.append("### (archived)")
            out_lines.extend(to_archive)
        else:
            out_lines[marker + 1:marker + 1] = to_archive
        text = "\n".join(out_lines)
        return text, summary

    return "\n".join(out_lines), summary
