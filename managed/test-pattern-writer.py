"""Offline tests for pattern_writer.apply_review_to_store — the per-review
author-pattern write path.

This module had NO test file, despite deciding WHICH store file every review
reads and writes. Network-free: the memory_store API is faked; the real
`memory_store.author_path` / `match_author_path` are used so the case-tolerant
resolution under test is the production one.
"""

import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import memory_store  # noqa: E402
import pattern_writer  # noqa: E402

# A pattern file in the lifecycle format, with an entry a review can match.
_ALICE = ("# Author Patterns: alice\n"
          "- **Wide except clause** (2x: #1, #2 | last 0 PRs: 0 clean): swallows errors.\n")
# The annotation shape pattern_lifecycle.extract_matched_patterns looks for.
_REVIEW_MATCHING = ("## Code Review\n\n"
                    "**1. Broad exception swallows the failure** "
                    "[matches author pattern: Wide except clause]\n")
_REVIEW_CLEAN = "## Code Review\n\nNo blockers.\n"


class FakeStore:
    def __init__(self, files):
        self.files = dict(files)
        self.writes = []
        self.list_calls = 0
        self.raise_on_list = False

    def list_memories(self, store_id, path_prefix="/"):
        self.list_calls += 1
        if self.raise_on_list:
            raise RuntimeError("store unreachable")
        return {p: {"id": f"mem_{i}", "content_sha256": f"sha_{i}"}
                for i, p in enumerate(self.files) if p.startswith(path_prefix)}

    def update_with(self, store_id, path, fn, default="", must_exist=False):
        self.list_calls += 1        # the real update_with re-lists via read_memory
        current = self.files.get(path)
        if current is None:
            if must_exist:
                return None
            current = default
        new = fn(current)
        self.files[path] = new
        self.writes.append(path)
        return new


def _install(monkeypatch, store):
    monkeypatch.setattr(pattern_writer, "memory_store", types.SimpleNamespace(
        AUTHOR_PREFIX=memory_store.AUTHOR_PREFIX,
        author_path=memory_store.author_path,
        match_author_path=memory_store.match_author_path,
        resolve_author_path=memory_store.resolve_author_path,
        list_memories=store.list_memories,
        update_with=store.update_with,
    ))
    return store


def test_strengthens_the_matched_pattern(monkeypatch):
    store = _install(monkeypatch, FakeStore({"/authors/alice.md": _ALICE}))
    summary = pattern_writer.apply_review_to_store(
        "memstore_x", "alice", 42, _REVIEW_MATCHING)
    assert summary is not None
    assert store.writes == ["/authors/alice.md"]
    assert "3x" in store.files["/authors/alice.md"]      # 2x -> 3x


def test_writes_the_case_variant_file_not_a_second_one(monkeypatch):
    """The repo-C orphan: the store holds `/authors/vorobiovd.md` but the login
    is `VorobiovD`. The write must land on the EXISTING file — creating the
    canonical-case one would split the history in two."""
    store = _install(monkeypatch, FakeStore({"/authors/vorobiovd.md": _ALICE}))
    summary = pattern_writer.apply_review_to_store(
        "memstore_x", "VorobiovD", 7, _REVIEW_MATCHING)
    assert summary is not None
    assert store.writes == ["/authors/vorobiovd.md"]
    assert "/authors/VorobiovD.md" not in store.files


def test_absent_author_file_is_a_noop_not_a_create(monkeypatch):
    """Creation stays learn's job (the review session mounts read-only, and
    deciding a pattern EXISTS is semantic) — this path must never create one."""
    store = _install(monkeypatch, FakeStore({"/authors/alice.md": _ALICE}))
    assert pattern_writer.apply_review_to_store(
        "memstore_x", "carol", 9, _REVIEW_MATCHING) is None
    assert store.writes == []
    assert "/authors/carol.md" not in store.files


def test_zero_author_files_warns_about_the_unseeded_store(monkeypatch, capsys):
    store = _install(monkeypatch, FakeStore({"/meta/air-meta.json": "{}"}))
    assert pattern_writer.apply_review_to_store(
        "memstore_x", "carol", 9, _REVIEW_CLEAN) is None
    err = capsys.readouterr().err
    assert "ZERO author files" in err


def test_populated_store_does_not_claim_it_is_unseeded(monkeypatch, capsys):
    """A new author on a POPULATED store is a normal no-op — emitting the
    'created empty and never seeded' warning there would be a false alarm."""
    store = _install(monkeypatch, FakeStore({"/authors/alice.md": _ALICE}))
    pattern_writer.apply_review_to_store("memstore_x", "carol", 9, _REVIEW_CLEAN)
    err = capsys.readouterr().err
    assert "ZERO author files" not in err
    assert "no author file at /authors/carol.md" in err


def test_absent_file_warns_even_with_no_matched_annotations(monkeypatch, capsys):
    """The warning used to be gated on `matched`, which made it silent exactly
    when the bootstrap gap was live: an author with no file can never produce a
    match, because matches come from annotations a review only emits when
    patterns were loaded."""
    store = _install(monkeypatch, FakeStore({"/authors/alice.md": _ALICE}))
    pattern_writer.apply_review_to_store("memstore_x", "carol", 9, _REVIEW_CLEAN)
    assert "nothing to strengthen" in capsys.readouterr().err


def test_listing_failure_degrades_to_the_canonical_path(monkeypatch, capsys):
    """A store listing failure must not raise out of the review's post-step, and
    must not re-list (resolve_author_path would retry the call that just failed)."""
    store = FakeStore({"/authors/alice.md": _ALICE})
    store.raise_on_list = True
    _install(monkeypatch, store)
    # update_with also lists internally in production; here it returns normally,
    # so the call must complete on the canonical path without raising.
    pattern_writer.apply_review_to_store("memstore_x", "alice", 5, _REVIEW_MATCHING)
    assert "author listing failed" in capsys.readouterr().err
    assert store.writes == ["/authors/alice.md"]


def test_listing_is_not_refetched_per_review(monkeypatch):
    """One /authors/ listing here + one inside update_with. A third (the old
    zero-author diagnostic) ran on the no-file branch — the common case until a
    store is seeded."""
    store = _install(monkeypatch, FakeStore({"/authors/alice.md": _ALICE}))
    pattern_writer.apply_review_to_store("memstore_x", "alice", 5, _REVIEW_MATCHING)
    assert store.list_calls == 2

    store.list_calls = 0
    pattern_writer.apply_review_to_store("memstore_x", "carol", 6, _REVIEW_CLEAN)
    assert store.list_calls == 2      # resolve + update_with, no diagnostic re-list
