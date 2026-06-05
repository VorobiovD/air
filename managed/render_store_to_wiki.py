#!/usr/bin/env python3
"""Deterministic store→wiki mirror render — the inverse of migrate_wiki_to_store.

The per-repo memory store is the source of truth; the git wiki is an EXPORTED
MIRROR for humans (GitHub wiki UI) and CLI reads. This module reads the store
and renders the legacy wiki file shapes, then pushes them — no AI session.

It replaces the in-session render the `air-learner` agent used to do
(learn-orchestrator Step 6) and runs throttled per-review (≤1×/hour, gated by
meta.py `mirror-due`) plus authoritatively after each learn curation.

REVIEW.md reassembly (the load-bearing part): `migrate.split_review_md` carves
`## Common Findings` → /common-findings.md, `## Service-Specific Patterns` →
/service-patterns.md, and each `### <login>` block → /authors/<login>.md, while
EVERYTHING ELSE — the `# <title>` H1, the `## Author Patterns` heading + intro,
`## Compliance / Reference`, `## Pending Drift`, … — lands in /review-misc.md.
So /review-misc.md is the structural SPINE: we walk it, inject Common Findings +
Service-Specific just before its first `## ` heading (their canonical slot,
after the H1 and before the first surviving section), and inject the sorted
per-author blocks right after the `## Author Patterns` heading + its intro.

NOT in the store (skipped here): REVIEW-HISTORY.md (a PR-comments regen owned by
the AI learn) and .air-meta.json (the counter). Managed-only: the CLI has no
store render (see plugins/air/commands/learn.md).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import memory_store
import migrate_wiki_to_store as migrate  # WIKI_FILE_MAP — we render its inverse

# Share the stdlib wiki helpers (clone/commit/push) — same sys.path idiom as
# review.py:60 / learn.py:182. Needed only for the push; the render is pure.
_LIB_DIR = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))
import wiki_git  # noqa: E402  (relies on the sys.path tweak above)

MIRROR_BANNER = (
    "> **Mirror** — source of truth is the air pattern memory store; edits "
    "here are overwritten. Update via /air:learn."
)

REVIEW_MISC_PATH = "/review-misc.md"
_OVERFLOW_HEADER_RE = re.compile(r"^<!-- older content: see .*-overflow-\*\.md -->\s*$")

# store path -> wiki filename. Derived as the literal inverse of
# migrate.WIKI_FILE_MAP MINUS the counter (.air-meta.json never mirrors to the
# wiki in store mode) — so adding a file to the migrate map automatically
# round-trips here, no parallel list to drift. REVIEW.md isn't in WIKI_FILE_MAP
# (it's split into many memories); render_review_md reassembles it separately.
STORE_TO_WIKI = {
    store_path: wiki_name
    for wiki_name, store_path in migrate.WIKI_FILE_MAP.items()
    if store_path != memory_store.META_PATH
}


def _is_author_patterns_h2(line: str) -> bool:
    """Match the `## Author Patterns` heading the same way migrate's split
    does (`"author patterns" in title.casefold()`), so the inject point is
    exactly the split's author-section boundary."""
    return line.startswith("## ") and "author patterns" in line.casefold()


def _overflow_paths(all_paths, stem: str) -> list[str]:
    """`/archive/<stem>-overflow-<n>.md` paths sorted by n NUMERICALLY
    (so -10 follows -2, not lexicographic)."""
    pref = f"{memory_store.ARCHIVE_PREFIX}{stem}-overflow-"
    found = []
    for p in all_paths:
        if p.startswith(pref) and p.endswith(".md"):
            try:
                found.append((int(p[len(pref):-3]), p))
            except ValueError:
                found.append((0, p))
    return [p for _, p in sorted(found)]


def reassemble(read, all_paths, primary_path: str) -> str | None:
    """Restore a logical file from primary + overflow chunks — the inverse of
    migrate.chunk_oversized. `read(path) -> str | None`. Returns the content,
    or None if the primary memory is absent. When primary's first line is the
    `<!-- older content: … -->` header, prepend the overflow chunks (oldest
    first) and drop that header line."""
    primary = read(primary_path)
    if primary is None:
        return None
    plines = primary.split("\n")
    if not (plines and _OVERFLOW_HEADER_RE.match(plines[0])):
        return primary
    stem = PurePosixPath(primary_path).stem
    lines: list[str] = []
    for op in _overflow_paths(all_paths, stem):
        chunk = read(op)
        if chunk is None:
            continue
        clines = chunk.split("\n")
        if clines and clines[-1] == "":   # drop the trailing "" from the `+ "\n"`
            clines = clines[:-1]
        lines.extend(clines)
    lines.extend(plines[1:])              # keep lines (drop the header line)
    return "\n".join(lines)


def render_review_md(read, all_paths) -> str:
    """Reassemble REVIEW.md from the store. Drives off /review-misc.md as the
    spine (see module docstring); injects Common Findings + Service-Specific
    before the first `## ` heading and the sorted per-author blocks after the
    `## Author Patterns` heading + intro."""
    common = reassemble(read, all_paths, memory_store.COMMON_FINDINGS_PATH)
    service = reassemble(read, all_paths, memory_store.SERVICE_PATTERNS_PATH)
    misc = reassemble(read, all_paths, REVIEW_MISC_PATH) or ""

    authors = []
    for p in sorted(
        (p for p in all_paths
         if p.startswith(memory_store.AUTHOR_PREFIX) and p.endswith(".md")),
        key=str.casefold,
    ):
        # reassemble (not bare read): an oversized author file spills to
        # /archive/<login>-overflow-*.md via migrate.chunk_oversized, same as
        # the shared files — read raw and we'd leak the overflow header line
        # and drop the spilled patterns.
        c = reassemble(read, all_paths, p)
        if c is not None and c.strip():
            authors.append(c.rstrip("\n"))
    author_block = "\n\n".join(authors)
    inject_cs = "\n\n".join(b.rstrip("\n") for b in (common, service) if b and b.strip())

    out: list[str] = [MIRROR_BANNER, ""]
    misc_lines = misc.split("\n")
    has_author_h2 = any(_is_author_patterns_h2(l) for l in misc_lines)
    inserted_cs = False
    i, n = 0, len(misc_lines)
    while i < n:
        line = misc_lines[i]
        # Common + Service go just before the first `## ` heading (the H1 and
        # any preamble precede it; the first surviving `## ` is Author Patterns
        # or Compliance/… — both of which followed Common+Service originally).
        if not inserted_cs and line.startswith("## "):
            if inject_cs:
                out += [inject_cs, ""]
            inserted_cs = True
        out.append(line)
        if _is_author_patterns_h2(line):
            # Pass through the section intro (until the next ##/### or EOF),
            # then inject the author blocks.
            j = i + 1
            while j < n and not (misc_lines[j].startswith("## ")
                                 or misc_lines[j].startswith("### ")):
                out.append(misc_lines[j])
                j += 1
            if author_block:
                out += ["", author_block]
            i = j
            continue
        i += 1

    if not inserted_cs and inject_cs:        # misc had no `## ` heading at all
        out += ["", inject_cs]
    if author_block and not has_author_h2:   # authors exist but no heading in misc
        out += ["", "## Author Patterns", "", author_block]

    # Collapse blank-line runs (the spine + injected blocks can abut with a
    # double blank). Cosmetic — blank count is non-semantic and migrate's split
    # normalizes it anyway — but keeps the mirror tidy and the round-trip clean.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return text.rstrip("\n") + "\n"


def render_shared_files(read, all_paths) -> dict[str, str]:
    """Pass-through whole-file memories → their wiki filenames (overflow
    reassembled). Absent memories are skipped (no empty files)."""
    out: dict[str, str] = {}
    for store_path, wiki_name in STORE_TO_WIKI.items():
        content = reassemble(read, all_paths, store_path)
        if content and content.strip():
            out[wiki_name] = content if content.endswith("\n") else content + "\n"
    return out


def render_files(read, all_paths) -> dict[str, str]:
    """Pure render: {wiki_filename: content}. Injected `read`/`all_paths` keep
    this API-free for unit tests."""
    files = {"REVIEW.md": render_review_md(read, all_paths)}
    files.update(render_shared_files(read, all_paths))
    return files


def _store_reader(store_id: str):
    """(read_fn, all_paths) backed by the live store."""
    all_paths = set(memory_store.list_memories(store_id, "/"))

    def read(path: str) -> str | None:
        r = memory_store.read_memory(store_id, path)
        return r[0] if r else None

    return read, all_paths


def render_all(store_id: str) -> dict[str, str]:
    """{wiki_filename: content} rendered from the live store."""
    read, all_paths = _store_reader(store_id)
    return render_files(read, all_paths)


def render_and_push(store_id: str, repo: str, token: str,
                    dry_run: bool = False) -> bool:
    """Render the store and push the mirror to the repo's git wiki. Best-effort
    — returns True on success (or dry-run), False on any failure. Never raises
    to the caller's hot path (review/learn wrap it, but we also swallow here)."""
    try:
        files = render_all(store_id)
    except Exception as e:  # noqa: BLE001
        print(f"  [mirror] render failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False
    if not files:
        print("  [mirror] empty store — nothing to render", file=sys.stderr)
        return False

    if dry_run:
        print("Rendered files (dry-run, not pushed):")
        for name in sorted(files):
            print(f"  {name:24s} {len(files[name].encode()):>8,} bytes")
        return True

    host = (f"https://x-access-token:{token}@github.com"
            if token else "https://github.com")
    url = f"{host}/{repo}.wiki.git"
    with tempfile.TemporaryDirectory() as tmp:
        wiki = Path(tmp) / "wiki"
        if not wiki_git.clone_wiki(url, wiki):
            print("  [mirror] wiki clone failed — skipping render push", file=sys.stderr)
            return False
        wiki_git.configure_identity(wiki, "air-machine",
                                    "air-machine@users.noreply.github.com")
        for name, content in files.items():
            (wiki / name).write_text(content, encoding="utf-8")
        # Reconcile orphans: a renderer-OWNED wiki file (REVIEW.md + the shared
        # mirrors) whose store source was removed this render should be deleted,
        # so the mirror stays an exact projection. Scoped to owned names only —
        # REVIEW-HISTORY.md (pushed by the AI learn, not in the store) and any
        # human-added wiki page are never touched. REVIEW.md is always rendered,
        # so it never lands in `orphans`.
        owned = {"REVIEW.md"} | set(STORE_TO_WIKI.values())
        orphans = sorted(owned - set(files))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return wiki_git.commit_paths(wiki, list(files),
                                     f"air: store mirror {stamp}", remove=orphans)


def render_push_and_stamp(store_id: str, repo: str, token: str) -> bool:
    """render_and_push + stamp `meta.py mirror-rendered` on success (resets the
    per-review throttle so it doesn't re-fire). Returns render_and_push's
    result. Shared by both call sites — the throttled review epilogue
    (managed/review.py, after a `mirror-due` gate) and the authoritative
    post-curation render (managed/learn.py)."""
    ok = render_and_push(store_id, repo, token)
    if ok:
        meta_script = _LIB_DIR / "meta.py"
        if meta_script.is_file():
            stamp = subprocess.run(
                [sys.executable, str(meta_script), "mirror-rendered",
                 "--store-id", store_id],
                capture_output=True, text=True,
            )
            sys.stderr.write(stamp.stderr)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the rendered files + byte counts without pushing")
    args = ap.parse_args()

    store_id = memory_store.find_store(args.repo)
    if not store_id:
        print(f"No store for {args.repo} (not migrated) — nothing to render.",
              file=sys.stderr)
        return 1
    token = os.environ.get("AIR_BOT_TOKEN") or os.environ.get("GH_TOKEN", "")
    ok = render_and_push(store_id, args.repo, token, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
