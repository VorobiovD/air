"""Memory-store helpers for air pattern storage.

A per-repo memory store replaces the git wiki as the source of truth for
review patterns. Discovery is by NAME — ``air-patterns <owner>/<repo>`` —
mirroring api.list_agents's find-by-name idiom so no store IDs need to be
configured anywhere. A repo without a store simply hasn't migrated yet:
callers fall back to the legacy wiki mount (that absence IS the rollout
flag).

Store layout (the contract shared by agents, pattern_writer, meta.py and
the learn export):

    /authors/<login>.md        per-author pattern file (lifecycle format)
    /common-findings.md        cross-author patterns
    /service-patterns.md       service-specific patterns
    /accepted-patterns.md      verifier suppression whitelist
    /severity-calibration.md   per-agent+category thresholds
    /glossary.md               domain terms
    /project-profile.md        repo profile (Review Focus Rules etc.)
    /archive/<login>.md        older pattern narratives (capped out)
    /meta/air-meta.json        shared /air:learn trigger counter

Individual memories cap at 100KB — the splitter and pattern_writer keep
files under that by design (per-author split + narrative caps).
"""

import os
import sys

from anthropic import Anthropic

# Same beta surface review.py already pins. Re-pin when stable lands.
BETA_HEADER = "managed-agents-2026-04-01-research-preview"
STORE_NAME_PREFIX = "air-patterns "

AUTHOR_PREFIX = "/authors/"
ARCHIVE_PREFIX = "/archive/"
META_PATH = "/meta/air-meta.json"  # also defined in plugins/air/lib/meta.py (stdlib constraint) — update in sync
COMMON_FINDINGS_PATH = "/common-findings.md"
SERVICE_PATTERNS_PATH = "/service-patterns.md"
ACCEPTED_PATTERNS_PATH = "/accepted-patterns.md"
SEVERITY_CALIBRATION_PATH = "/severity-calibration.md"
GLOSSARY_PATH = "/glossary.md"
PROJECT_PROFILE_PATH = "/project-profile.md"

_client: Anthropic | None = None


def client() -> Anthropic:
    """Lazy singleton — module import stays side-effect free so test
    runners and constant-only importers (e.g. migrate's WIKI_FILE_MAP)
    never need an API key at import time."""
    global _client
    if _client is None:
        _client = Anthropic(default_headers={"anthropic-beta": BETA_HEADER})
    return _client


def store_name(repo: str) -> str:
    return f"{STORE_NAME_PREFIX}{repo}"


def _paginate(list_fn, **kwargs) -> list[dict]:
    """Exhaust an SDK list endpoint (next_page cursor), return all items.
    Mirrors api._paginate for the requests-based callers."""
    items: list[dict] = []
    page = list_fn(**kwargs)
    while True:
        data = page.model_dump()
        items.extend(data.get("data", []))
        next_page = data.get("next_page")
        if not next_page:
            break
        page = list_fn(page=next_page, **kwargs)
    return items


def find_store(repo: str) -> str | None:
    """Return the store id for ``repo``, or None if the repo hasn't been
    migrated. Exhausts ALL pages then keeps the first match (newest-first
    listing) — mirrors api.list_agents's exhaust-then-pick contract rather
    than stopping at the first page that happens to contain a match."""
    for s in _paginate(client().beta.memory_stores.list):
        if s.get("name") == store_name(repo) and not s.get("archived_at"):
            return s["id"]
    return None


def get_store_id(repo: str, flow: str = "review") -> str | None:
    """find_store with the standard graceful fallback — shared by
    review.py and learn.py so the warn message can't drift."""
    try:
        return find_store(repo)
    except Exception as e:
        print(f"  [warn] pattern-store lookup failed ({e}) — "
              f"{flow} falls back to the wiki", file=__import__("sys").stderr)
        return None


def create_store(repo: str) -> str:
    store = client().beta.memory_stores.create(
        name=store_name(repo),
        description=(
            f"air review patterns for {repo}: per-author pattern files under "
            f"/authors/, shared pattern files at the root, archived narratives "
            f"under /archive/. Source of truth — the repo's git wiki is an "
            f"exported mirror."
        ),
    )
    return store.id


def find_or_create_store(repo: str) -> str:
    return find_store(repo) or create_store(repo)


def list_memories(store_id: str, path_prefix: str = "/") -> dict[str, dict]:
    """Flat {path: {"id", "content_sha256"}} map for the given prefix."""
    out: dict[str, dict] = {}
    def _list(**kw):
        # No `order_by` / `depth`: the Managed-Agents Memories API dropped
        # `order_by` (SDK raises TypeError) and now bounds `depth` to 0–1 (a
        # `depth=20` 400s: "must be … less than or equal to 1"). A bare
        # `path_prefix` already returns the full RECURSIVE, prefix-filtered
        # listing this needs — verified live 2026-07-23 ("/" returns nested
        # /authors/*, /meta/*, /archive/* + root files). Passing the stale
        # kwargs made EVERY store read TypeError → reviews ran pattern-blind and
        # headless learn crashed. `**kw` still carries pagination (`page`).
        return client().beta.memory_stores.memories.list(
            store_id, path_prefix=path_prefix, **kw
        )
    for item in _paginate(_list):
        # Live API uses type "memory_metadata" in list responses (docs
        # examples show "memory") — accept both.
        if item.get("type") in ("memory", "memory_metadata"):
            out[item["path"]] = {
                "id": item["id"],
                "content_sha256": item.get("content_sha256"),
            }
    return out


def _dir_prefix(path: str) -> str:
    """The directory-shaped `path_prefix` for a memory path (ends in `/`).

    The Memories API now regex-validates path_prefix as `^(/([^/\\x00]+/)*)?$`
    (a directory, trailing slash), so a FULL file path like `/authors/foo.md`
    must be listed via its dir `/authors/` and matched exactly from the result.
    `/glossary.md` → `/`; `/authors/foo.md` → `/authors/`. Verified live 2026-07-23.
    """
    head = path.rsplit("/", 1)[0]
    return (head + "/") if head else "/"


def author_path(login: str) -> str:
    """The canonical store path for an author's pattern file."""
    return f"{AUTHOR_PREFIX}{login}.md"


def match_author_path(paths, login: str) -> str | None:
    """Pick the author-file path for `login` from `paths`, tolerating a CASE
    VARIANT. Returns None when the author has no file at all.

    Pure + dependency-free so `plugins/air/lib/meta.py` (stdlib-only, its own
    raw-REST copy of this API) can hold a byte-equivalent copy and a parity
    test can lock the two together — same arrangement as `_dir_prefix`.

    Why tolerate case: GitHub logins are case-INSENSITIVE for identity but
    case-PRESERVING in the API, while store paths are case-SENSITIVE. So a file
    written as `/authors/vorobiovd.md` (the one-shot wiki migration took the
    login from a lowercased wiki heading) is INVISIBLE to a later exact-case
    read for login `VorobiovD` — which silently orphans that history AND makes
    every review for that author pattern-blind. Observed live on repo-C: 4
    patterns frozen at the migration, `read-author` reporting "new author"
    forever, `pattern_writer` no-opping on every review. Resolving
    case-insensitively heals it in place — no migration, and no divergent
    second file (the alternative, writing the canonical case, would leave two
    half-histories).

    Exact match wins. Otherwise a UNIQUE case-insensitive match is adopted.
    Two or more non-exact candidates are AMBIGUOUS — never silently pick one of
    two histories, so return None (treated as absent) and let the warning
    surface it for a manual merge.
    """
    canonical = author_path(login)
    if canonical in paths:
        return canonical
    want = canonical.lower()
    cands = sorted(p for p in paths if p.lower() == want)
    if len(cands) == 1:
        return cands[0]
    return None


def resolve_author_path(store_id: str, login: str) -> str:
    """`match_author_path` against the live store, falling back to the
    canonical path when the author has no file (so a caller can create it).

    Logs the case-variant adoption and the ambiguous case — a silent resolution
    here is what made the repo-C orphan invisible for weeks.
    """
    paths = list_memories(store_id, path_prefix=AUTHOR_PREFIX)
    canonical = author_path(login)
    hit = match_author_path(paths, login)
    if hit is None:
        dupes = sorted(p for p in paths if p.lower() == canonical.lower())
        if len(dupes) > 1:
            print(f"  [store][warn] {len(dupes)} case-variant author files for "
                  f"{login!r}: {dupes} — using {canonical}; merge them manually",
                  file=sys.stderr)
        return canonical
    if hit != canonical:
        print(f"  [store] author file {hit} matches login {login!r} "
              f"case-insensitively — using it (canonical is {canonical})",
              file=sys.stderr)
    return hit


def read_memory(store_id: str, path: str) -> tuple[str, str, str] | None:
    """Return (content, content_sha256, memory_id) or None if absent."""
    entry = list_memories(store_id, path_prefix=_dir_prefix(path)).get(path)
    if not entry:
        return None
    mem = client().beta.memory_stores.memories.retrieve(
        entry["id"], memory_store_id=store_id
    )
    return mem.content, mem.content_sha256, mem.id


def write_memory(store_id: str, path: str, content: str) -> None:
    """Create-or-overwrite without read-modify-write semantics. NOT safe
    for concurrent writers (no precondition) — migration/seeding only.
    For counter-style or concurrent mutations use update_with()."""
    existing = list_memories(store_id, path_prefix=_dir_prefix(path)).get(path)
    if existing:
        client().beta.memory_stores.memories.update(
            existing["id"], memory_store_id=store_id, content=content
        )
    else:
        client().beta.memory_stores.memories.create(
            store_id, path=path, content=content
        )


def update_with(store_id: str, path: str, fn, default: str = "",
                max_retries: int = 3, must_exist: bool = False) -> str | None:
    """Read-modify-write with content_sha256 optimistic concurrency.

    ``fn(old_content) -> new_content``. Replaces wiki_git.commit_meta's
    pull-rebase-retry: on precondition mismatch, re-read and re-apply.
    Returns the content that was written, or None when ``must_exist`` is
    set and the memory is absent (caller decides what absence means —
    e.g. pattern_writer defers author-file creation to /air:learn).
    """
    from anthropic import APIStatusError  # local: keep module import light

    for attempt in range(max_retries):
        current = read_memory(store_id, path)
        if current is None:
            if must_exist:
                return None
            new = fn(default)
            try:
                client().beta.memory_stores.memories.create(
                    store_id, path=path, content=new
                )
                return new
            except APIStatusError:
                # Raced with a concurrent create — fall through to update.
                continue
        content, sha, mem_id = current
        new = fn(content)
        if new == content:
            return new
        try:
            client().beta.memory_stores.memories.update(
                mem_id,
                memory_store_id=store_id,
                content=new,
                precondition={"type": "content_sha256", "content_sha256": sha},
            )
            return new
        except APIStatusError as e:
            if attempt == max_retries - 1:
                raise
            print(f"  [store] precondition raced on {path} "
                  f"(attempt {attempt + 1}): {e}; re-reading", file=sys.stderr)
    # Unreachable: the loop returns on success or re-raises on the final
    # attempt. Kept as a defensive guard against future loop edits.
    raise RuntimeError(f"update_with exhausted retries on {path}")
