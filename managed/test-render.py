#!/usr/bin/env python3
"""Unit tests for render_store_to_wiki — the deterministic store→wiki mirror.

Pure functions (injected `read`/`all_paths`), so no API key needed — but
importing the module pulls in memory_store (anthropic), so run in the managed
venv:  python managed/test-render.py   (also pytest-compatible).

Covers: the REVIEW.md spine reassembly round-trips through migrate's split
(losslessness), the overflow inverse (reassemble ∘ chunk_oversized == id incl.
numeric -N ordering), shared-file pass-through, the banner, and section order.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import migrate_wiki_to_store as migrate  # noqa: E402
import render_store_to_wiki as r  # noqa: E402


def _norm(s: str) -> str:
    """Compare modulo cosmetic blank-line runs (non-semantic; the split
    normalizes whitespace and the render collapses blank runs)."""
    return re.sub(r"\n{3,}", "\n\n", s.strip())

# A fixture mirroring real store shapes: review-misc is the spine (H1 +
# `## Author Patterns` heading+intro + a trailing `## Pending Drift`), authors
# split out, common/service split out. bob uses the SAFE `#### Archived` (H4)
# form so the strict re-split round-trips (H3 `### (archived)` is covered
# separately below — it's rendered losslessly but re-split would misroute it).
STORE = {
    "/review-misc.md": (
        "# QAI Backend — Code Review Patterns\n\n"
        "## Author Patterns\n\nBehavioral tendencies tracked per author.\n\n"
        "## Pending Drift\n\n- 2026-06-01: something to refresh"
    ),
    "/common-findings.md": "## Common Findings\n\n- **Empty-array guard** (3x: #1, #2, #3 | last 0 PRs: 0 clean): guard implode",
    "/service-patterns.md": "## Service-Specific Patterns\n\n### `app/Services/`\n\n- **N+1 in loops** (2x: #4, #5 | last 1 PRs: 1 clean): batch it",
    "/authors/alice.md": "### alice\n\n- **Stale docs** (4x: #1, #2, #6, #7 | last 0 PRs: 0 clean): comments drift",
    "/authors/bob.md": (
        "### bob\n\n- **Flow gaps** (1x: #3 | new): misses a guard\n\n"
        "#### Archived (10+ clean PRs, surfaced once for reference)\n\n"
        "- **Old habit** (11x | last 11 PRs: 11 clean): fixed long ago"
    ),
    "/glossary.md": "# Glossary\n\n| Term | Definition |\n|---|---|\n| air | reviewer |",
    "/accepted-patterns.md": "# Accepted Patterns\n\n- **logs full object**: behind VPN+IAM (PR #9)",
    "/meta/air-meta.json": '{"reviews_since": 3}',
}


def _reader(store):
    return (lambda p: store.get(p)), set(store)


def test_banner_present():
    read, paths = _reader(STORE)
    review = r.render_review_md(read, paths)
    assert review.startswith(r.MIRROR_BANNER)


def test_section_order():
    read, paths = _reader(STORE)
    review = r.render_review_md(read, paths)
    pos = {s: review.index(s) for s in
           ("## Common Findings", "## Service-Specific Patterns",
            "## Author Patterns", "### alice", "### bob", "## Pending Drift")}
    assert pos["## Common Findings"] < pos["## Service-Specific Patterns"] < pos["## Author Patterns"]
    assert pos["## Author Patterns"] < pos["### alice"] < pos["### bob"]   # authors sorted, after the heading
    assert pos["### bob"] < pos["## Pending Drift"]                        # tail follows authors


def test_render_roundtrips_via_split():
    """Re-splitting the rendered REVIEW.md reproduces the REVIEW-derived store
    memories (modulo migrate's .strip() normalization) — the losslessness
    invariant. Banner stripped first (it's render-only, not stored)."""
    read, paths = _reader(STORE)
    review = r.render_review_md(read, paths)
    body = review[len(r.MIRROR_BANNER):].lstrip("\n")
    resplit = migrate.split_review_md(body)
    for path in ("/review-misc.md", "/common-findings.md",
                 "/service-patterns.md", "/authors/alice.md", "/authors/bob.md"):
        assert path in resplit, f"{path} missing from re-split: {sorted(resplit)}"
        assert _norm(resplit[path]) == _norm(STORE[path]), \
            f"{path} mismatch:\n--got--\n{resplit[path]}\n--want--\n{STORE[path]}"


def test_render_lossless_with_h3_archived():
    """An author file with an H3 `### (archived)` sub-heading is rendered with
    all its content present (re-split would misroute it, but production never
    re-splits — the store is the source of truth)."""
    store = {
        "/review-misc.md": "# Patterns\n\n## Author Patterns\n\nintro",
        "/authors/dave.md": "### dave\n\n- **Live** (1x: #1 | new): x\n\n### (archived)\n\n- **Frozen** (10x | last 10 PRs: 10 clean): y",
    }
    read, paths = _reader(store)
    review = r.render_review_md(read, paths)
    assert "### dave" in review
    assert "**Live**" in review and "**Frozen**" in review
    assert "### (archived)" in review


def test_shared_files_passthrough():
    read, paths = _reader(STORE)
    files = r.render_shared_files(read, paths)
    assert files["GLOSSARY.md"].strip() == STORE["/glossary.md"].strip()
    assert files["ACCEPTED-PATTERNS.md"].strip() == STORE["/accepted-patterns.md"].strip()
    # Counter + REVIEW-HISTORY are never rendered.
    assert "REVIEW-HISTORY.md" not in files
    assert all(".air-meta" not in n for n in files)


def test_reassemble_inverts_chunk_oversized():
    big = "\n".join(f"line {i:05d} " + "x" * 40 for i in range(3000))  # > 95KB
    chunked = migrate.chunk_oversized({"/glossary.md": big})
    assert len(chunked) > 1, "fixture should overflow"
    read, paths = _reader(chunked)
    assert r.reassemble(read, paths, "/glossary.md") == big


def test_render_reassembles_overflowed_author_file():
    """An oversized author file spills to /archive/<login>-overflow-*.md via
    migrate.chunk_oversized; render_review_md must reassemble it (not read raw,
    which would leak the overflow header and drop the spilled patterns)."""
    pats = "\n".join(f"- **Pattern {i:04d}** (1x: #{i} | new): " + "y" * 60
                     for i in range(1500))
    author = f"### bigauthor\n\n{pats}"           # > 95KB
    store = {"/review-misc.md": "# P\n\n## Author Patterns\n\nintro",
             "/authors/bigauthor.md": author}
    chunked = migrate.chunk_oversized(dict(store))
    assert any("overflow" in p for p in chunked), "author file should have overflowed"
    read, paths = _reader(chunked)
    review = r.render_review_md(read, paths)
    assert "older content: see" not in review, "overflow header leaked into REVIEW.md"
    for ln in author.split("\n"):
        if ln.strip():
            assert ln in review, f"author line lost in render: {ln[:50]!r}"


def test_store_to_wiki_is_inverse_of_migrate_map():
    """STORE_TO_WIKI is the literal inverse of migrate.WIKI_FILE_MAP minus the
    counter — guards the derivation against a future map change."""
    for wiki_name, store_path in migrate.WIKI_FILE_MAP.items():
        if store_path == migrate.memory_store.META_PATH:
            assert store_path not in r.STORE_TO_WIKI    # counter never mirrors
        else:
            assert r.STORE_TO_WIKI[store_path] == wiki_name
    assert "REVIEW.md" not in r.STORE_TO_WIKI.values()   # assembled separately


def test_reassemble_passthrough_when_small():
    small = "## Common Findings\n\n- one\n- two"
    read, paths = _reader({"/common-findings.md": small})
    assert r.reassemble(read, paths, "/common-findings.md") == small
    assert r.reassemble(read, paths, "/absent.md") is None


def test_overflow_paths_numeric_sort():
    paths = {f"/archive/glossary-overflow-{n}.md" for n in (1, 2, 10, 11)}
    paths.add("/glossary.md")
    ordered = r._overflow_paths(paths, "glossary")
    assert ordered == [f"/archive/glossary-overflow-{n}.md" for n in (1, 2, 10, 11)]


def test_overflow_paths_malformed_sorts_last():
    """A malformed chunk index must sort LAST (newest), never first — placing
    it first would corrupt the reassembled order."""
    paths = {"/archive/g-overflow-1.md", "/archive/g-overflow-2.md",
             "/archive/g-overflow-bad.md"}
    ordered = r._overflow_paths(paths, "g")
    assert ordered[-1] == "/archive/g-overflow-bad.md"
    assert ordered[:2] == ["/archive/g-overflow-1.md", "/archive/g-overflow-2.md"]


def test_render_and_push_skips_empty_store(monkeypatch=None):
    """Empty store listing → render_and_push returns False WITHOUT rendering or
    pushing (a banner-only render would orphan-rm every shared wiki file)."""
    orig = r._store_reader
    r._store_reader = lambda sid: ((lambda p: None), set())
    try:
        # Returns False at the empty guard, before any clone/push.
        assert r.render_and_push("sid", "owner/repo", "tok") is False
    finally:
        r._store_reader = orig


def test_archive_narratives_folded_into_review_archive():
    """/archive/<name>.md (ad-hoc learn-written narratives) fold into
    REVIEW-ARCHIVE.md; legacy stays; overflow chunks are NOT folded."""
    store = {
        "/review-misc.md": "# P\n\n## Author Patterns\n\nintro",
        "/archive/legacy.md": "# Legacy\n\n- old export prose",
        "/archive/VorobiovD.md": "### VorobiovD (archived)\n\n- capped-out detail",
        "/archive/glossary-overflow-1.md": "OVERFLOW_FRAGMENT_NOT_STANDALONE",
        "/archive/glossary-overflow-bad.md": "MALFORMED_CHUNK_NOT_STANDALONE",
    }
    read, paths = _reader(store)
    ra = r.render_shared_files(read, paths)["REVIEW-ARCHIVE.md"]
    assert "old export prose" in ra            # legacy export preserved
    assert "capped-out detail" in ra           # per-author archive folded in
    assert "OVERFLOW_FRAGMENT_NOT_STANDALONE" not in ra   # numeric chunk excluded
    assert "MALFORMED_CHUNK_NOT_STANDALONE" not in ra     # malformed chunk also excluded


def test_render_files_applies_wiki_cap():
    """render_files MUST run its output through wiki_cap.cap_files — a silent
    import/logic failure there would un-bound the wiki for every store-backed
    repo. Spy cap_files (manual patch, no fixture — works under both runners)
    and assert render_files invokes it AND returns its (capped) output."""
    lib = str(Path(r.__file__).resolve().parent.parent / "plugins" / "air" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import wiki_cap
    orig = wiki_cap.cap_files
    called = {}

    def _spy(files):
        called["yes"] = True
        return {k: v + "\n[CAPPED]" for k, v in files.items()}, ["[cap] spy"]

    wiki_cap.cap_files = _spy
    try:
        out = r.render_files(lambda p: None, [])
    finally:
        wiki_cap.cap_files = orig
    assert called.get("yes"), "render_files did not call wiki_cap.cap_files"
    assert out and all(v.endswith("[CAPPED]") for v in out.values()), \
        "render_files did not return cap_files' output"


_TESTS = [v for k, v in sorted(globals().items())
          if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    sys.exit(1 if failed else 0)
