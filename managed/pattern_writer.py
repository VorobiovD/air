#!/usr/bin/env python3
"""Apply one review's deterministic pattern updates to the memory store.

Called by review.py after a successful review on a store-backed repo.
Replaces the coordinator's TURN 3 Part B wiki bash for those repos: the
review session mounts the store read-only (PR content is untrusted —
prompt injection must not be able to poison the pattern store), and the
mechanical lifecycle ops (strengthen + clean counters) run here in code
with sha256-preconditioned writes.

Semantic operations (creating patterns, merging, prose caps, archive
narrative moves) remain with /air:learn sessions, which mount read_write.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "plugins" / "air" / "lib"))
import pattern_lifecycle  # noqa: E402

import memory_store  # noqa: E402


def apply_review_to_store(store_id: str, author_login: str, pr_number: int,
                          review_body: str) -> dict | None:
    """Strengthen matched author patterns + advance clean counters.

    Returns the lifecycle summary, or None when the author has no pattern
    file yet (creation is /air:learn's job — nothing mechanical to do).
    """
    matched = pattern_lifecycle.extract_matched_patterns(review_body)
    # ONE listing of /authors/, reused for the case-tolerant path resolution and
    # the zero-author diagnostic below (this runs on every review; update_with
    # necessarily lists the prefix again internally, so don't add a third).
    # Case-tolerant: a migration-era `/authors/vorobiovd.md` must not be
    # orphaned by an exact-case read for login `VorobiovD` (see
    # memory_store.match_author_path).
    try:
        listing = memory_store.list_memories(
            store_id, path_prefix=memory_store.AUTHOR_PREFIX)
    except Exception as e:
        print(f"  [patterns] author listing failed ({type(e).__name__}: {e}) — "
              f"using the canonical path", file=sys.stderr)
        listing = None
    # On a failed listing use the canonical path directly — passing listing=None
    # into resolve_author_path would just make it retry the call that just failed.
    path = (memory_store.author_path(author_login) if listing is None
            else memory_store.resolve_author_path(
                store_id, author_login, listing=listing))
    summary_holder: dict = {}

    def _update(content: str) -> str:
        updated, summary = pattern_lifecycle.apply_review(
            content, pr_number, matched
        )
        summary_holder.update(summary)
        return updated

    # must_exist: author-file creation is /air:learn's job (semantic work);
    # absence here is a normal no-op, not an error.
    written = memory_store.update_with(store_id, path, _update, must_exist=True)
    if written is None:
        # Warn UNCONDITIONALLY, not only when `matched` — an author with no file
        # can never produce a match (matches come from the `[already raised by
        # @author-pattern]` annotations a review only emits when patterns were
        # loaded), so gating the warning on `matched` guaranteed silence exactly
        # when the bootstrap gap was in play: creation is deferred to /air:learn,
        # but learn only curated files that ALREADY existed, so nothing ever
        # created the first one. `learn_headless.seed_missing_author_files` closes
        # that (headless learn); this line stays as the detector if it regresses.
        print(f"  [patterns] no author file at {path} — nothing to strengthen "
              f"({len(matched)} matched annotation(s); creation is /air:learn's "
              f"job — seeded by learn_headless on the messages-api path)",
              file=sys.stderr)
        if listing is not None and len(listing) == 0:
            print(f"  [patterns][warn] {store_id} has ZERO author files — this "
                  f"store was created empty and never seeded, so every review "
                  f"on it is pattern-blind. A headless (messages-api) learn "
                  f"seeds it; on the `full` architecture it needs a manual "
                  f"seed or migrate_wiki_to_store.py.", file=sys.stderr)
        return None
    # Audit line per strengthen — spurious strengthens (e.g. an injected
    # annotation that slipped the title-line anchor) must be traceable.
    for name in summary_holder.get("strengthened", []):
        print(f"  [patterns] strengthened: {name!r} (PR #{pr_number})",
              file=sys.stderr)
    parts = [f"{k}={len(v)}" for k, v in summary_holder.items() if v]
    print(f"  [patterns] {path}: " + (", ".join(parts) if parts else "no-op"),
          file=sys.stderr)
    return summary_holder
