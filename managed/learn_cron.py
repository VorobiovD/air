#!/usr/bin/env python3
"""Out-of-band learn driver — runs headless learn on a SCHEDULE, fully decoupled
from any PR's CI job (the autonomy "Option A").

Today a threshold-fired learn rides the review job's tail (review.py:_run_learn_sync),
extending that job by minutes. This driver moves learn off the PR critical path
entirely: a scheduled workflow (.github/workflows/air-learn-cron.yml) runs this,
which enumerates the store-backed repos whose counter says learn is DUE and runs
`learn_headless.run_headless_learn` for each. Reviews then only need to keep
bumping the counter (meta.py claim) — the in-job execution can be turned off
separately (the "go-live" step), since this is additive + dry-run-safe.

Due-detection reuses the EXACT review-side primitives (no forked logic):
  * store names are the registry — `air-patterns <owner>/<repo>` (memory_store).
  * `meta.should_trigger_learn` — the same predicate `meta.py claim` uses.
  * `meta._learn_lock_live` — skip a repo with an in-flight learn (anti-double-fire,
    same CAS lock as the in-job path).

Store-backed repos only (legacy-wiki repos keep the managed/CLI wiki pipeline).
Needs ANTHROPIC_API_KEY (read stores + curate) and, for non-dry-run, AIR_BOT_TOKEN
that can push the wiki mirror of each due repo.
"""

import argparse
import os
import sys

import memory_store
import learn_headless

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "air", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import meta  # noqa: E402  (plugins/air/lib — shared counter + due predicate)


def find_due_repos(repos_filter=None, log=print) -> list:
    """Return [(repo, store_id, reason)] for store-backed repos whose learn is DUE
    and not already in flight. repos_filter (a set of owner/repo) narrows the scan
    for a targeted run. Best-effort per repo: a bad counter read skips that repo."""
    due = []
    try:
        stores = memory_store._paginate(memory_store.client().beta.memory_stores.list)
    except Exception as e:
        log(f"  [cron] store listing failed: {type(e).__name__}: {e}")
        return due
    for s in stores:
        name = s.get("name", "") or ""
        if not name.startswith(memory_store.STORE_NAME_PREFIX) or s.get("archived_at"):
            continue
        repo = name[len(memory_store.STORE_NAME_PREFIX):].strip()
        if repos_filter and repo not in repos_filter:
            continue
        store_id = s.get("id")
        try:
            found = meta._store_find_meta(store_id)
        except Exception as e:
            log(f"  [cron] {repo}: counter read failed ({e}) — skip")
            continue
        if not found:
            continue  # store exists but no counter yet (no reviews) → nothing to learn
        m = found[0]
        if meta._learn_lock_live(m):
            log(f"  [cron] {repo}: learn already in flight (locked) — skip")
            continue
        trigger, reason = meta.should_trigger_learn(m)
        if trigger:
            due.append((repo, store_id, reason))
    return due


def run(repos_filter=None, dry_run=False, limit=None, log=print) -> dict:
    """Find due repos and run headless learn for each. Returns a summary."""
    token = os.environ.get("AIR_BOT_TOKEN", "")
    due = find_due_repos(repos_filter=repos_filter, log=log)
    if limit:
        due = due[:limit]
    log(f"  [cron] {len(due)} repo(s) due: " +
        (", ".join(f"{r} ({reason})" for r, _, reason in due) or "none"))
    results = {}
    for repo, store_id, _reason in due:
        log(f"  [cron] === learn {repo} (dry_run={dry_run}) ===")
        try:
            results[repo] = learn_headless.run_headless_learn(
                repo, token=token, store_id=store_id, dry_run=dry_run, log=log)
        except Exception as e:
            log(f"  [cron] {repo}: learn errored: {type(e).__name__}: {e}")
            results[repo] = {"error": str(e)}
    return {"due": [r for r, _, _ in due], "ran": list(results), "dry_run": dry_run}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Scheduled out-of-band headless learn (store-backed repos)")
    p.add_argument("--list", action="store_true",
                   help="enumerate due repos ONLY — make NO model calls (zero cost; the safe scheduled-trial default)")
    p.add_argument("--dry-run", action="store_true", help="run the curation but write nothing (makes model calls)")
    p.add_argument("--repos", default="", help="comma-separated owner/repo filter (default: all due)")
    p.add_argument("--limit", type=int, default=0, help="cap repos learned this run (0 = no cap)")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    repos_filter = {r.strip() for r in args.repos.split(",") if r.strip()} or None
    if args.list:
        # Enumeration only — NO model calls, NO writes. For the scheduled trial.
        due = find_due_repos(repos_filter=repos_filter)
        print(f"[cron] {len(due)} repo(s) due (list-only, no learns): "
              + (", ".join(f"{r} ({reason})" for r, _, reason in due) or "none"), file=sys.stderr)
        return 0
    summary = run(repos_filter=repos_filter, dry_run=args.dry_run,
                  limit=(args.limit or None))
    print(f"[cron] done: {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
