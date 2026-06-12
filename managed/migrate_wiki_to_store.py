#!/usr/bin/env python3
"""One-shot, idempotent migration: git wiki -> memory store.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python migrate_wiki_to_store.py owner/repo [--dry-run]

Clones the repo's wiki, splits REVIEW.md into per-author files plus the
shared pattern files, and seeds the per-repo memory store (created if
missing, discovered by name otherwise). Re-running overwrites store
content from the wiki — safe while the wiki is still the source of
truth; once managed runs write to the store, do NOT re-run without
exporting first (the wiki becomes the stale mirror).

Split contract (see managed/memory_store.py module docstring):
    REVIEW.md "### <login>" sections      -> /authors/<login>.md
    REVIEW.md "## Common Findings"        -> /common-findings.md
    REVIEW.md "## Service-Specific..."    -> /service-patterns.md
    REVIEW.md other/reference sections    -> /review-misc.md
    ACCEPTED-PATTERNS.md                  -> /accepted-patterns.md
    SEVERITY-CALIBRATION.md               -> /severity-calibration.md
    GLOSSARY.md                           -> /glossary.md
    PROJECT-PROFILE.md                    -> /project-profile.md
    REVIEW-ARCHIVE.md                     -> /archive/legacy.md
    .air-meta.json                        -> /meta/air-meta.json

Entries are chunked under the 100KB per-memory cap (oversized author
files spill to /archive/<login>-overflow-<n>.md, newest content kept in
the primary file).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import memory_store

MEMORY_CAP = 95_000  # bytes; headroom under the 100KB API cap

WIKI_FILE_MAP = {
    "ACCEPTED-PATTERNS.md": memory_store.ACCEPTED_PATTERNS_PATH,
    "SEVERITY-CALIBRATION.md": memory_store.SEVERITY_CALIBRATION_PATH,
    "GLOSSARY.md": memory_store.GLOSSARY_PATH,
    "PROJECT-PROFILE.md": memory_store.PROJECT_PROFILE_PATH,
    "REVIEW-ARCHIVE.md": f"{memory_store.ARCHIVE_PREFIX}legacy.md",
    ".air-meta.json": memory_store.META_PATH,
}

AUTHOR_HEADING_RE = re.compile(r"^### (?P<login>\S+)(?P<archived> \(archived\))?\s*$")
H2_RE = re.compile(r"^## (?P<title>.+?)\s*$")


def split_review_md(text: str) -> dict[str, str]:
    """Split REVIEW.md into store paths. Author sections (active +
    archived) group per login; known H2 sections map to shared files;
    everything else lands in /review-misc.md so nothing is dropped."""
    out: dict[str, list[str]] = {}
    current_path = "/review-misc.md"
    in_author_block = False

    for line in text.split("\n"):
        h2 = H2_RE.match(line)
        if h2:
            title = h2.group("title").casefold()
            in_author_block = "author patterns" in title
            if "common finding" in title:
                current_path = "/common-findings.md"
            elif "service-specific" in title or "service specific" in title:
                current_path = "/service-patterns.md"
            elif in_author_block:
                current_path = "/review-misc.md"  # heading itself is misc
            else:
                current_path = "/review-misc.md"
            out.setdefault(current_path, []).append(line)
            continue
        author = AUTHOR_HEADING_RE.match(line) if in_author_block else None
        if author:
            current_path = f"{memory_store.AUTHOR_PREFIX}{author.group('login')}.md"
            out.setdefault(current_path, []).append(line)
            continue
        out.setdefault(current_path, []).append(line)

    return {p: "\n".join(lines).strip() + "\n" for p, lines in out.items()
            if "".join(lines).strip()}


def _byte_chunks(lines: list[str], cap: int) -> list[list[str]]:
    """Group lines into chunks each ≤ cap bytes (joined with newlines).

    Byte-bounded, not line-count-bounded: a fixed line count (the old
    `range(…, 800)`) silently produced >100KB memories when lines were
    long — repo-A's 261KB glossary spilled a single 167KB overflow file
    that the API rejects. A single line longer than cap goes in its own
    chunk (still oversized, but unavoidable without splitting mid-line;
    such lines are pathological and flagged by the caller)."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        b = len(line.encode()) + 1  # +1 for the join newline
        if cur and size + b > cap:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += b
    if cur:
        chunks.append(cur)
    return chunks


def chunk_oversized(seed: dict[str, str]) -> dict[str, str]:
    """Spill content over the per-memory cap into /archive overflow files,
    keeping the newest lines in the primary path (entries append over
    time, so the tail is newest). Both the primary `keep` slice and every
    overflow file are byte-bounded under MEMORY_CAP so each lands under
    the 100KB API write cap."""
    out: dict[str, str] = {}
    for path, content in seed.items():
        data = content.encode()
        if len(data) <= MEMORY_CAP:
            out[path] = content
            continue
        lines = content.split("\n")
        header = (f"<!-- older content: see "
                  f"{memory_store.ARCHIVE_PREFIX}{Path(path).stem}-overflow-*.md -->")
        # Reserve room for the header line so primary stays under cap.
        keep_cap = MEMORY_CAP - len(header.encode()) - 1
        keep: list[str] = []
        size = 0
        for line in reversed(lines):
            size += len(line.encode()) + 1
            if keep and size > keep_cap:
                break
            keep.append(line)
        keep.reverse()
        overflow = lines[: len(lines) - len(keep)]
        stem = Path(path).stem
        chunks = _byte_chunks(overflow, MEMORY_CAP)
        for n, chunk in enumerate(chunks, 1):
            ofile = f"{memory_store.ARCHIVE_PREFIX}{stem}-overflow-{n}.md"
            out[ofile] = "\n".join(chunk) + "\n"
            if len("\n".join(chunk).encode()) > MEMORY_CAP:
                print(f"  [warn] {ofile} still over cap — a single line "
                      f"exceeds {MEMORY_CAP} bytes (pathological entry; "
                      f"clean the source wiki)", file=sys.stderr)
        out[path] = header + "\n" + "\n".join(keep)
        if len(out[path].encode()) > MEMORY_CAP:
            print(f"  [warn] {path} still over cap after keep-trim — a single "
                  f"line exceeds {MEMORY_CAP} bytes (pathological entry; clean "
                  f"the source wiki)", file=sys.stderr)
        print(f"  [chunk] {path} exceeded cap — {len(chunks)} overflow file(s)",
              file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the split without writing to the store")
    args = ap.parse_args()

    seed: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        wiki = Path(tmp) / "wiki"
        # Private wikis need a token — same x-access-token URL shape as
        # every other wiki access in the codebase. Falls back to an
        # unauthenticated clone (public wikis / ambient credential helper).
        token = os.environ.get("AIR_BOT_TOKEN") or os.environ.get("GH_TOKEN", "")
        host = (f"https://x-access-token:{token}@github.com"
                if token else "https://github.com")
        url = f"{host}/{args.repo}.wiki.git"
        r = subprocess.run(["git", "clone", "--depth", "1", url, str(wiki)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            # Redact the token-bearing URL from anything we echo.
            err = r.stderr.strip()[:300].replace(token, "***") if token \
                else r.stderr.strip()[:300]
            print(f"Error: wiki clone failed for {args.repo}: {err}\n"
                  f"Private wiki? Set AIR_BOT_TOKEN or GH_TOKEN.",
                  file=sys.stderr)
            return 1

        review = wiki / "REVIEW.md"
        if review.is_file():
            seed.update(split_review_md(review.read_text()))
        for fname, mpath in WIKI_FILE_MAP.items():
            f = wiki / fname
            if f.is_file():
                seed[mpath] = f.read_text()

    if not seed:
        print("Nothing to migrate — wiki is empty.", file=sys.stderr)
        return 1

    seed = chunk_oversized(seed)

    print(f"Split for {args.repo}:")
    for path in sorted(seed):
        print(f"  {path:45s} {len(seed[path].encode()):>8,} bytes")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    store_id = memory_store.find_or_create_store(args.repo)
    print(f"\nStore: {store_id} ({memory_store.store_name(args.repo)})")
    for path, content in sorted(seed.items()):
        memory_store.write_memory(store_id, path, content)
        print(f"  seeded {path}")
    print(f"\nDone — {len(seed)} memories. Managed runs on {args.repo} will "
          f"now mount the store; the git wiki becomes the exported mirror.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
