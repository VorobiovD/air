#!/usr/bin/env python3
"""
Shared auto-trigger counter for `/air:learn`.

Tracks review cadence in `.air-meta.json` at the wiki root so CLI and
managed runs increment the same counter. When the threshold fires, the
caller is responsible for invoking `/air:learn` (CLI) or
`managed/learn.py` (managed) — this module only decides and mutates state.

Threshold:
    reviews_since >= 15
        → trigger
    days_since_cleanup >= 14 AND reviews_since > 0
        → trigger
    days_since_cleanup >= 14 AND reviews_since == 0
        → skip, bump last_check so we don't re-evaluate on every review
    else
        → skip

Stdlib-only, matches the `plugins/air/hooks/pre-commit-drift.py` idiom.

Also gates the deterministic store→wiki mirror render (store-backed repos):
`mirror-due` (exit 1 when the wiki mirror is stale by >= MIRROR_INTERVAL_HOURS
or was never rendered) throttles the per-review render so it's a cheap meta
read in the common case and a git push at most ~once/hour; `mirror-rendered`
stamps the time after a successful render. (See managed/render_store_to_wiki.py.)

Usage (CLI or managed):
    python3 meta.py bump  --wiki-dir <dir> --pr-number <N>
    python3 meta.py check --wiki-dir <dir>        # exit 1 triggers /air:learn, 0 skips
    python3 meta.py reset --wiki-dir <dir> --pr-number <N>  # after /air:learn finishes
    python3 meta.py mirror-due      --store-id <id>   # exit 1 = render the mirror, 0 = within window
    python3 meta.py mirror-rendered --store-id <id>   # stamp after a successful render
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_git import META_FILENAME  # single source for the file name

# --- memory-store backend -------------------------------------------------
# When the repo has migrated to a memory store (see managed/memory_store.py
# for the layout contract), the shared counter lives at STORE_META_PATH and
# CLI + managed mutate it through the API with content_sha256 preconditions
# (replacing wiki_git.commit_meta's pull-rebase-retry). stdlib-only: urllib,
# matching this package's no-dependency rule.
# These three mirror managed/memory_store.py (META_PATH / BETA_HEADER) —
# the stdlib-only rule here forbids importing it; update both in sync.
STORE_META_PATH = "/meta/air-meta.json"
_API_BASE = "https://api.anthropic.com/v1"
_BETA = "managed-agents-2026-04-01-research-preview"


def _store_api(method: str, path: str, body: dict | None = None) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    req = urllib.request.Request(
        f"{_API_BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": _BETA,
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _store_find_meta(store_id: str) -> tuple[dict, str, str] | None:
    """Return (meta, content_sha256, memory_id) or None when absent."""
    # NOTE: no `depth` param — the API 400s with "depth requires
    # order_by=path" (observed live on the svc-transcribe pilot run).
    # A bare path_prefix returns the exact-path match we need.
    listing = _store_api(
        "GET",
        f"/memory_stores/{store_id}/memories"
        f"?path_prefix={STORE_META_PATH}",
    )
    for item in listing.get("data", []):
        # Live API lists memories as type "memory_metadata" (docs examples
        # show "memory") — accept both. Observed on the svc-transcribe pilot.
        if item.get("type") in ("memory", "memory_metadata") \
                and item.get("path") == STORE_META_PATH:
            mem = _store_api(
                "GET", f"/memory_stores/{store_id}/memories/{item['id']}"
            )
            try:
                return json.loads(mem["content"]), mem["content_sha256"], mem["id"]
            except (json.JSONDecodeError, KeyError):
                return _default_meta(), mem.get("content_sha256", ""), mem["id"]
    return None


def _store_mutate_meta(store_id: str, fn) -> dict:
    """Read-modify-write the counter with optimistic concurrency. fn(meta)
    mutates and returns the meta dict. Retries on precondition races."""
    for attempt in range(3):
        found = _store_find_meta(store_id)
        if found is None:
            meta = fn(_default_meta())
            try:
                _store_api(
                    "POST", f"/memory_stores/{store_id}/memories",
                    {"path": STORE_META_PATH,
                     "content": json.dumps(meta, indent=2, sort_keys=True)},
                )
                return meta
            except urllib.error.HTTPError:
                continue  # raced a concurrent create — retry as update
        else:
            meta, sha, mem_id = found
            meta = fn(meta)
            try:
                _store_api(
                    "POST", f"/memory_stores/{store_id}/memories/{mem_id}",
                    {"content": json.dumps(meta, indent=2, sort_keys=True),
                     "precondition": {"type": "content_sha256",
                                      "content_sha256": sha}},
                )
                return meta
            except urllib.error.HTTPError as e:
                if attempt == 2:
                    raise
                print(f"  [meta] store precondition raced "
                      f"(attempt {attempt + 1}): {e.code}; re-reading",
                      file=sys.stderr)
    raise RuntimeError("store meta mutation exhausted retries")

# Threshold constants — mirror the CLI prose values in review.md Step 13.
# Reviews-count leads; the days rule is only a slow-repo backstop. The old
# 2-day rule fired a full Opus learn session on nearly every review in
# repos reviewed less often than every 2 days — it fired on 4 of the 5
# runs preceding the 2026-05-22 credit exhaustion.
REVIEWS_THRESHOLD = 15
DAYS_THRESHOLD = 14

# Wiki-mirror render throttle: re-render the store→wiki mirror at most once
# per this interval on the per-review path (the learn cadence forces an
# authoritative render regardless). Keeps the wiki fresh within an hour while
# making the common per-review case a single cheap meta read (no git push).
MIRROR_INTERVAL_HOURS = 1


def _utc_now_iso() -> str:
    """Timezone-aware UTC ISO-8601 string. One format everywhere for round-trip."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> datetime:
    """Accept both `...Z` and `...+00:00` shapes produced by us or prior tools."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _default_meta() -> dict:
    now = _utc_now_iso()
    return {
        "last_cleanup": now,
        "last_check": now,
        "reviews_since": 0,
        "last_processed_pr": 0,
        # Empty = never rendered → the first mirror-due check renders.
        "last_mirror_render": "",
    }


def read_meta(wiki_dir: Path) -> dict:
    """Read `.air-meta.json` or return defaults if the file doesn't exist yet."""
    path = Path(wiki_dir) / META_FILENAME
    if not path.is_file():
        return _default_meta()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] meta: failed to read {path}: {e}; using defaults", file=sys.stderr)
        return _default_meta()
    # Fill missing fields from defaults — tolerant to schema evolution.
    merged = _default_meta()
    merged.update({k: v for k, v in data.items() if k in merged})
    return merged


def write_meta(wiki_dir: Path, meta: dict) -> None:
    path = Path(wiki_dir) / META_FILENAME
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def days_since(iso_ts: str, now: datetime | None = None) -> float:
    """Days between `iso_ts` and `now` (default: current time)."""
    now = now or datetime.now(timezone.utc)
    return (now - _parse_iso(iso_ts)).total_seconds() / 86400.0


def should_trigger_learn(meta: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Return (trigger, reason) per the threshold rules.

    `reason` is a short human-readable line for the operator log.
    """
    reviews = int(meta.get("reviews_since", 0))
    days = days_since(meta["last_cleanup"], now=now)

    if reviews >= REVIEWS_THRESHOLD:
        return True, f"reviews_since={reviews} >= {REVIEWS_THRESHOLD}"
    if days >= DAYS_THRESHOLD and reviews > 0:
        return True, f"days_since_cleanup={days:.1f} >= {DAYS_THRESHOLD} with reviews_since={reviews}"
    if days >= DAYS_THRESHOLD and reviews == 0:
        return False, f"days_since_cleanup={days:.1f} but reviews_since=0 — nothing to learn from"
    return False, f"reviews_since={reviews}, days_since_cleanup={days:.1f} — below threshold"


def cmd_find_store(args) -> int:
    """Print the repo's pattern-store id (empty + exit 0 when the repo has
    not migrated — callers treat empty as 'use the wiki backend'). Name
    convention mirrors managed/memory_store.py: 'air-patterns <owner>/<repo>'."""
    wanted = f"air-patterns {args.repo}"
    try:
        page = "/memory_stores"
        while True:
            data = _store_api("GET", page)
            for s in data.get("data", []):
                if s.get("name") == wanted and not s.get("archived_at"):
                    print(s["id"])
                    return 0
            nxt = data.get("next_page")
            if not nxt:
                break
            page = f"/memory_stores?page={nxt}"
    except Exception as e:
        print(f"  [warn] meta: store lookup failed ({e}) — falling back to wiki backend",
              file=sys.stderr)
    return 0


def _bump_fn(pr: int):
    def fn(meta: dict) -> dict:
        meta["reviews_since"] = int(meta.get("reviews_since", 0)) + 1
        if pr > int(meta.get("last_processed_pr", 0)):
            meta["last_processed_pr"] = pr
        return meta
    return fn


def cmd_bump(args) -> int:
    pr = int(args.pr_number)
    if args.store_id:
        try:
            meta = _store_mutate_meta(args.store_id, _bump_fn(pr))
        except Exception as e:
            print(f"  [warn] meta: store bump failed ({e}) — counter not "
                  f"bumped this run", file=sys.stderr)
            return 0  # never block the review flow on counter plumbing
    else:
        wiki = Path(args.wiki_dir)
        meta = _bump_fn(pr)(read_meta(wiki))
        write_meta(wiki, meta)
    print(
        f"  [meta] bumped: reviews_since={meta['reviews_since']} last_processed_pr={meta['last_processed_pr']}",
        file=sys.stderr,
    )
    return 0


def cmd_check(args) -> int:
    if args.store_id:
        try:
            found = _store_find_meta(args.store_id)
            meta = found[0] if found else _default_meta()
        except Exception as e:
            print(f"  [warn] meta: store check failed ({e}) — treating as "
                  f"below threshold", file=sys.stderr)
            return 0
    else:
        meta = read_meta(Path(args.wiki_dir))
    trigger, reason = should_trigger_learn(meta)
    # On the "date passed but no PRs" skip, bump last_check so the next review
    # doesn't re-evaluate and log the same skip line. Only this branch mutates.
    if not trigger and int(meta.get("reviews_since", 0)) == 0 and days_since(meta["last_cleanup"]) >= DAYS_THRESHOLD:
        def touch(m):
            m["last_check"] = _utc_now_iso()
            return m
        if args.store_id:
            try:
                _store_mutate_meta(args.store_id, touch)
            except Exception:
                pass  # cosmetic optimization only — skip silently on error
        else:
            write_meta(Path(args.wiki_dir), touch(meta))
    print(f"  [meta] {reason}", file=sys.stderr)
    # Exit 1 = trigger (signals caller to run /air:learn).
    # Exit 0 = skip. Matches the "exit code drives shell conditional" idiom.
    return 1 if trigger else 0


def cmd_reset(args) -> int:
    """Called after /air:learn finishes successfully. Resets the counter and
    records the cleanup timestamp + latest PR processed."""
    pr = int(args.pr_number)
    now = _utc_now_iso()

    def fn(meta: dict) -> dict:
        meta["last_cleanup"] = now
        meta["last_check"] = now
        meta["reviews_since"] = 0
        if pr > int(meta.get("last_processed_pr", 0)):
            meta["last_processed_pr"] = pr
        return meta

    if args.store_id:
        try:
            meta = _store_mutate_meta(args.store_id, fn)
        except Exception as e:
            print(f"  [warn] meta: store reset failed ({e})", file=sys.stderr)
            return 0
    else:
        wiki = Path(args.wiki_dir)
        meta = fn(read_meta(wiki))
        write_meta(wiki, meta)
    print(f"  [meta] reset at {now} (last_processed_pr={meta['last_processed_pr']})", file=sys.stderr)
    return 0


def _mirror_due(meta: dict, now: datetime | None = None) -> tuple[bool, str]:
    """Return (due, reason): render if never rendered or stale by the interval."""
    last = meta.get("last_mirror_render", "") or ""
    if not last:
        return True, "never rendered → due"
    try:
        hrs = days_since(last, now=now) * 24
    except (ValueError, TypeError):
        return True, "unparseable last_mirror_render → due"
    if hrs >= MIRROR_INTERVAL_HOURS:
        return True, f"last render {hrs:.1f}h ago >= {MIRROR_INTERVAL_HOURS}h → due"
    return False, f"last render {hrs:.1f}h ago < {MIRROR_INTERVAL_HOURS}h → within window"


def cmd_mirror_due(args) -> int:
    """Exit 1 if the wiki mirror should be re-rendered, 0 if within the window.
    Read-only (one cheap meta read; never a git op). On store error, return 0
    (skip) — a render would hit the same unreachable store anyway."""
    if args.store_id:
        try:
            found = _store_find_meta(args.store_id)
            meta = found[0] if found else _default_meta()
        except Exception as e:
            print(f"  [warn] meta: mirror-due check failed ({e}) — skipping render",
                  file=sys.stderr)
            return 0
    else:
        meta = read_meta(Path(args.wiki_dir))
    due, reason = _mirror_due(meta)
    print(f"  [meta] mirror {reason}", file=sys.stderr)
    return 1 if due else 0


def cmd_mirror_rendered(args) -> int:
    """Stamp last_mirror_render after a successful render so the throttle resets."""
    now = _utc_now_iso()

    def fn(meta: dict) -> dict:
        meta["last_mirror_render"] = now
        return meta

    if args.store_id:
        try:
            _store_mutate_meta(args.store_id, fn)
        except Exception as e:
            print(f"  [warn] meta: mirror-rendered update failed ({e})", file=sys.stderr)
            return 0
    else:
        wiki = Path(args.wiki_dir)
        write_meta(wiki, fn(read_meta(wiki)))
    print(f"  [meta] mirror rendered at {now}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    # __doc__ starts with a newline, so .splitlines()[0] is empty. Pick the
    # first non-blank line for a useful --help description.
    desc = next((l for l in (__doc__ or "").splitlines() if l.strip()), "")
    parser = argparse.ArgumentParser(description=desc)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_backend_args(p):
        # Exactly one backend: --wiki-dir (legacy file in a wiki clone) or
        # --store-id (memory store, needs ANTHROPIC_API_KEY).
        p.add_argument("--wiki-dir", help="Path to the checked-out wiki repo")
        p.add_argument("--store-id", help="Memory store id (memstore_...) — store-backed counter")

    p_bump = sub.add_parser("bump", help="Increment reviews_since after a successful review")
    add_backend_args(p_bump)
    p_bump.add_argument("--pr-number", required=True, type=int, help="PR number just reviewed")
    p_bump.set_defaults(fn=cmd_bump)

    p_check = sub.add_parser("check", help="Decide whether to trigger /air:learn (exit 1 = trigger)")
    add_backend_args(p_check)
    p_check.set_defaults(fn=cmd_check)

    p_reset = sub.add_parser("reset", help="Record a successful /air:learn run")
    add_backend_args(p_reset)
    p_reset.add_argument("--pr-number", required=True, type=int)
    p_reset.set_defaults(fn=cmd_reset)

    p_find = sub.add_parser("find-store", help="Print the repo's pattern-store id (empty = not migrated)")
    p_find.add_argument("--repo", required=True, help="owner/repo")
    p_find.set_defaults(fn=cmd_find_store)

    p_mdue = sub.add_parser("mirror-due", help="Decide whether to re-render the wiki mirror (exit 1 = due)")
    add_backend_args(p_mdue)
    p_mdue.set_defaults(fn=cmd_mirror_due)

    p_mrendered = sub.add_parser("mirror-rendered", help="Stamp a successful mirror render")
    add_backend_args(p_mrendered)
    p_mrendered.set_defaults(fn=cmd_mirror_rendered)

    args = parser.parse_args(argv)
    if args.cmd != "find-store" and not args.wiki_dir and not getattr(args, "store_id", None):
        parser.error("one of --wiki-dir or --store-id is required")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
