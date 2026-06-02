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
META_PATH = "/meta/air-meta.json"

_client: Anthropic | None = None


def client() -> Anthropic:
    """Lazy singleton — module import must stay side-effect free so
    pattern_lifecycle's pure-logic consumers can import constants without
    an API key in the environment."""
    global _client
    if _client is None:
        _client = Anthropic(default_headers={"anthropic-beta": BETA_HEADER})
    return _client


def store_name(repo: str) -> str:
    return f"{STORE_NAME_PREFIX}{repo}"


def find_store(repo: str) -> str | None:
    """Return the store id for ``repo``, or None if the repo hasn't been
    migrated. Pagination + newest-wins mirrors api.list_agents."""
    found = None
    page = client().beta.memory_stores.list()
    while True:
        data = page.model_dump()
        for s in data.get("data", []):
            if s.get("name") == store_name(repo) and not s.get("archived_at"):
                # newest-first listing: first match wins, keep the first
                if found is None:
                    found = s["id"]
        next_page = data.get("next_page")
        if not next_page or found:
            break
        page = client().beta.memory_stores.list(page=next_page)
    return found


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
    page = client().beta.memory_stores.memories.list(
        store_id, path_prefix=path_prefix, order_by="path", depth=20
    )
    while True:
        data = page.model_dump()
        for item in data.get("data", []):
            if item.get("type") == "memory":
                out[item["path"]] = {
                    "id": item["id"],
                    "content_sha256": item.get("content_sha256"),
                }
        next_page = data.get("next_page")
        if not next_page:
            break
        page = client().beta.memory_stores.memories.list(
            store_id, path_prefix=path_prefix, order_by="path", depth=20,
            page=next_page,
        )
    return out


def read_memory(store_id: str, path: str) -> tuple[str, str, str] | None:
    """Return (content, content_sha256, memory_id) or None if absent."""
    entry = list_memories(store_id, path_prefix=path).get(path)
    if not entry:
        return None
    mem = client().beta.memory_stores.memories.retrieve(
        entry["id"], memory_store_id=store_id
    )
    return mem.content, mem.content_sha256, mem.id


def write_memory(store_id: str, path: str, content: str) -> None:
    """Create-or-overwrite without read-modify-write semantics. For
    counter-style mutations use update_with()."""
    existing = list_memories(store_id, path_prefix=path).get(path)
    if existing:
        client().beta.memory_stores.memories.update(
            existing["id"], memory_store_id=store_id, content=content
        )
    else:
        client().beta.memory_stores.memories.create(
            store_id, path=path, content=content
        )


def update_with(store_id: str, path: str, fn, default: str = "",
                max_retries: int = 3) -> str:
    """Read-modify-write with content_sha256 optimistic concurrency.

    ``fn(old_content) -> new_content``. Replaces wiki_git.commit_meta's
    pull-rebase-retry: on precondition mismatch, re-read and re-apply.
    Returns the content that was written.
    """
    from anthropic import APIStatusError  # local: keep module import light

    for attempt in range(max_retries):
        current = read_memory(store_id, path)
        if current is None:
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
    raise RuntimeError(f"update_with exhausted retries on {path}")
