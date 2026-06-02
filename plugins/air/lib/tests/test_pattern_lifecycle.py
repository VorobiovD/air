"""Unit tests for pattern_lifecycle.py — strengthen/clean/decline/archive."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
sys.path.insert(0, str(LIB))

import pattern_lifecycle as pl  # noqa: E402


SAMPLE = """### somedev

- **Stale doc refs** (3x: #1, #2, #3 | last 2 PRs: 2 clean): Counts drift. Tendency: sweep mirrors (e.g. CLAUDE.md).
- **Flow gaps** (1x: #4 | new): Misses N+1 path.
- **Old habit** (2x: #5, #6 | last 9 PRs: 9 clean): Nearly archived.
- **Fading** (1x: #7 | last 4 PRs: 4 clean): About to decline.
"""

ARCHIVED_SAMPLE = """### somedev

- **Active** (1x: #1 | last 0 PRs: 0 clean): Live.

### somedev (archived)

- **Gone** (1x: #2 | last 12 PRs: 12 clean): Archived long ago.
"""


def test_strengthen_resets_counter_and_appends_ref():
    out, summary = pl.apply_review(SAMPLE, 80, {"Stale doc refs"})
    assert "- **Stale doc refs** (4x: #1, #2, #3, #80 | last 0 PRs: 0 clean):" in out
    assert summary["strengthened"] == ["Stale doc refs"]
    # prose preserved, including its parenthetical
    assert "(e.g. CLAUDE.md)" in out


def test_strengthen_is_case_and_space_insensitive():
    out, _ = pl.apply_review(SAMPLE, 81, {"  stale  DOC refs "})
    assert "(4x: #1, #2, #3, #81 | last 0 PRs: 0 clean)" in out


def test_clean_pass_increments_non_matched():
    out, summary = pl.apply_review(SAMPLE, 80, {"Stale doc refs"})
    assert "- **Flow gaps** (1x: #4 | last 1 PRs: 1 clean):" in out
    assert "Flow gaps" in summary["cleaned"]


def test_decline_tag_added_at_threshold():
    out, summary = pl.apply_review(SAMPLE, 80, set())
    assert "- **Fading** (1x: #7 | last 5 PRs: 5 clean) (declining):" in out
    assert "Fading" in summary["declining"]


def test_strengthen_removes_declining_tag():
    declined, _ = pl.apply_review(SAMPLE, 80, set())
    assert "(declining)" in declined
    out, _ = pl.apply_review(declined, 81, {"Fading"})
    fading_line = [l for l in out.split("\n") if "**Fading**" in l][0]
    assert "(declining)" not in fading_line
    assert "| last 0 PRs: 0 clean)" in fading_line


def test_archive_at_threshold_moves_entry():
    out, summary = pl.apply_review(SAMPLE, 80, set())
    assert "Old habit" in summary["archived"]
    # entry moved under an (archived) heading with its final counters
    archived_idx = out.index("(archived)")
    assert out.index("**Old habit**") > archived_idx
    assert "last 10 PRs: 10 clean" in out


def test_archived_section_never_touched():
    out, summary = pl.apply_review(ARCHIVED_SAMPLE, 99, {"Gone"})
    assert "- **Gone** (1x: #2 | last 12 PRs: 12 clean):" in out
    assert summary["strengthened"] == []
    assert "- **Active** (1x: #1 | last 1 PRs: 1 clean):" in out


def test_non_entry_lines_pass_through():
    md = "### somedev\n\nfree prose line\n- not a pattern bullet\n"
    out, _ = pl.apply_review(md, 80, set())
    assert out == md


def test_extract_matched_patterns():
    body = (
        "**1. X** [matches author pattern: Stale doc refs (3x)]\n"
        "**2. Y** [matches declining pattern: Fading]\n"
        "**3. Z** [matches archived pattern: Gone (1x)]\n"
    )
    got = pl.extract_matched_patterns(body)
    assert got == {"stale doc refs", "fading"}  # archived excluded


def test_extract_ignores_annotations_outside_title_lines():
    # Injection containment: attacker-quoted annotation text in finding
    # prose / code fences must not count — only finding-title lines do.
    body = (
        "**1. Real finding** [matches author pattern: Stale doc refs]\n"
        "The diff contained the literal text\n"
        "[matches author pattern: Flow gaps] inside a quoted snippet.\n"
        "```\n[matches author pattern: Old habit]\n```\n"
    )
    got = pl.extract_matched_patterns(body)
    assert got == {"stale doc refs"}


INLINE_ARCHIVED = """### somedev

- **Live one** (2x: #1, #2 | last 1 PRs: 1 clean): Active entry.
- **Frozen** (1x: #3 | last 29 PRs: 29 clean) (archived): Real-wiki inline form.
- **Nearly frozen** (4x: #4 | last 7 PRs: 7 clean) (declining, archival-eligible): Tagged by learn.
"""


def test_inline_archived_entries_are_frozen():
    out, summary = pl.apply_review(INLINE_ARCHIVED, 90, {"Frozen", "Nearly frozen"})
    assert "- **Frozen** (1x: #3 | last 29 PRs: 29 clean) (archived):" in out
    assert "- **Nearly frozen** (4x: #4 | last 7 PRs: 7 clean) (declining, archival-eligible):" in out
    assert summary["strengthened"] == []
    assert "- **Live one** (2x: #1, #2 | last 2 PRs: 2 clean):" in out


def test_decorative_status_tags_parse():
    md = ("### somedev\n\n- **Hot** (5x: #1 | last 0 PRs: 0 clean) "
          "(active, strengthening): Recurring.\n")
    out, summary = pl.apply_review(md, 91, {"Hot"})
    assert "- **Hot** (6x: #1, #91 | last 0 PRs: 0 clean):" in out
    assert summary["strengthened"] == ["Hot"]
