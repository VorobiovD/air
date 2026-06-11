#!/usr/bin/env python3
"""One-shot workspace migration for air's pattern memory stores.

Context (2026-06-11): the org is consolidating API usage into the dedicated
LifeMD workspace; the old workspace (where air's agents/stores live) is
being de-privileged and the Claude Code workspace removed. Agents and
environments are STATELESS — setup.py recreates them from the repo on the
first run with a new key. The per-repo pattern stores are the only stateful
assets (learned author patterns, severity calibration, accepted patterns);
keys are workspace-bound and resources are workspace-scoped, so they must
be COPIED store→store across keys.

Usage:
    export AIR_OLD_API_KEY=sk-ant-...   # key in the OLD workspace (source)
    export AIR_NEW_API_KEY=sk-ant-...   # key in the NEW (LifeMD) workspace
    python migrate_workspace_stores.py --dry-run   # inventory + plan only
    python migrate_workspace_stores.py             # copy + verify
    python migrate_workspace_stores.py --verify    # re-verify only

Copies every store whose name starts with "air-patterns " (the production
per-repo stores; experiment stores are deliberately left behind). Idempotent:
re-runs overwrite same-path memories (write semantics match
memory_store.write_memory — migration/seeding only, no concurrent writers).
Verification compares per-path content_sha256 maps on both sides.
"""
import argparse
import os
import sys

import anthropic

PREFIX = "air-patterns "


def _paginate(list_fn, **kw):
    """Exhaust an SDK list endpoint. Memory-store endpoints signal
    continuation via an opaque `next_page` cursor consumed as the `page`
    param — NOT `has_more`/`starting_after`; probing those single-pages
    the walk silently (mirrors `memory_store.py:_paginate`; same cursor
    bug class as the session-event drain fixed on this PR)."""
    cursor = None
    while True:
        page = list_fn(**kw) if cursor is None else list_fn(page=cursor, **kw)
        body = page if isinstance(page, dict) else page.model_dump()
        for item in body.get("data", []):
            yield item
        cursor = body.get("next_page")
        if not cursor:
            return


def _stores(client) -> dict[str, str]:
    """{name: id} for production pattern stores."""
    return {
        s["name"]: s["id"]
        for s in _paginate(client.beta.memory_stores.list)
        if s.get("name", "").startswith(PREFIX)
    }


def _memories(client, store_id: str) -> dict[str, dict]:
    """{path: {id, content_sha256}} for the whole store."""
    out = {}
    for item in _paginate(
        client.beta.memory_stores.memories.list,
        memory_store_id=store_id, path_prefix="/", order_by="path", depth=20,
    ):
        if item.get("type") in ("memory", "memory_metadata"):
            out[item["path"]] = {"id": item["id"], "content_sha256": item.get("content_sha256")}
    return out


def _read(client, store_id: str, mem_id: str) -> str:
    return client.beta.memory_stores.memories.retrieve(
        mem_id, memory_store_id=store_id
    ).content


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="compare only, copy nothing")
    args = ap.parse_args()

    old_key = os.environ.get("AIR_OLD_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    new_key = os.environ.get("AIR_NEW_API_KEY")
    if not old_key or (not new_key and not args.dry_run):
        print("Set AIR_OLD_API_KEY (or ANTHROPIC_API_KEY) and AIR_NEW_API_KEY.", file=sys.stderr)
        return 2
    old = anthropic.Anthropic(api_key=old_key)
    new = anthropic.Anthropic(api_key=new_key) if new_key else None

    src = _stores(old)
    print(f"source stores ({len(src)}):")
    plans: list[tuple[str, str, dict[str, dict]]] = []
    for name, sid in sorted(src.items()):
        mems = _memories(old, sid)
        print(f"  {name}  {sid}  ({len(mems)} memories)")
        plans.append((name, sid, mems))

    if new is not None:
        dst_existing = _stores(new)
        same = set(src.values()) & set(dst_existing.values())
        if same:
            print(
                "\nNOTE: destination key sees the SAME store ids — both keys "
                "are in one workspace; no migration needed.",
            )
            return 0

    if args.dry_run:
        total = sum(len(m) for _, _, m in plans)
        print(f"\ndry-run: would copy {total} memories across {len(plans)} stores.")
        return 0

    failures = 0
    # `dst_existing` (fetched once above — `new` is always set past the
    # dry-run return) covers every pre-existing store; newly-created ones
    # get their id from the create response. No per-store re-fetch.
    for name, src_id, mems in plans:
        dst_id = dst_existing.get(name)
        if dst_id is None and not args.verify:
            store = new.beta.memory_stores.create(
                name=name,
                description=(
                    f"air review patterns for {name.removeprefix(PREFIX)}: per-author "
                    f"pattern files under /authors/, shared pattern files at the root, "
                    f"archived narratives under /archive/. Source of truth — the repo's "
                    f"git wiki is an exported mirror. (Migrated from workspace store "
                    f"{src_id}, 2026-06-11.)"
                ),
            )
            dst_id = store.id
            print(f"\n{name}: created {dst_id}")
        elif dst_id is None:
            print(f"\n{name}: MISSING at destination", file=sys.stderr)
            failures += 1
            continue
        else:
            print(f"\n{name}: destination exists {dst_id}")

        if not args.verify:
            existing_dst = _memories(new, dst_id)
            for path, meta in sorted(mems.items()):
                content = _read(old, src_id, meta["id"])
                d = existing_dst.get(path)
                if d and d.get("content_sha256") == meta.get("content_sha256"):
                    continue  # already identical (idempotent re-run)
                if d:
                    new.beta.memory_stores.memories.update(
                        d["id"], memory_store_id=dst_id, content=content
                    )
                else:
                    new.beta.memory_stores.memories.create(
                        dst_id, path=path, content=content
                    )
                print(f"    copied {path} ({len(content)} chars)")

        # Verify: sha maps must match exactly.
        src_shas = {p: m.get("content_sha256") for p, m in mems.items()}
        dst_shas = {p: m.get("content_sha256") for p, m in _memories(new, dst_id).items()}
        if src_shas == dst_shas:
            print(f"    VERIFIED: {len(src_shas)} memories, all sha256 match")
        else:
            missing = set(src_shas) - set(dst_shas)
            extra = set(dst_shas) - set(src_shas)
            diff = {p for p in set(src_shas) & set(dst_shas) if src_shas[p] != dst_shas[p]}
            print(
                f"    MISMATCH: missing={sorted(missing)} extra={sorted(extra)} "
                f"differing={sorted(diff)}",
                file=sys.stderr,
            )
            failures += 1

    if failures:
        print(f"\n{failures} store(s) failed verification.", file=sys.stderr)
        return 1
    print("\nAll stores migrated + verified. Next: run setup.py with the new key "
          "(recreates agents/env), update the GitHub secrets, smoke one review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
