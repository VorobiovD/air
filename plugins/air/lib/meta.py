#!/usr/bin/env python3
"""
Shared auto-trigger counter for `/air:learn`.

Tracks review cadence in `.air-meta.json` at the wiki root so CLI and
managed runs increment the same counter. When the threshold fires, the
caller is responsible for invoking `/air:learn` (CLI) or
`managed/learn.py` (managed) — this module only decides and mutates state.

Threshold:
    reviews_since >= 5
        → trigger
    days_since_cleanup >= 2 AND reviews_since > 0
        → trigger
    days_since_cleanup >= 2 AND reviews_since == 0
        → skip, bump last_check so we don't re-evaluate on every review
    else
        → skip

Stdlib-only, matches the `plugins/air/hooks/pre-commit-drift.py` idiom.

Usage (CLI or managed):
    python3 meta.py bump  --wiki-dir <dir> --pr-number <N>
    python3 meta.py check --wiki-dir <dir>        # exit 1 triggers /air:learn, 0 skips
    python3 meta.py reset --wiki-dir <dir> --pr-number <N>  # after /air:learn finishes
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wiki_git import META_FILENAME  # single source for the file name

# Threshold constants — mirror the CLI prose values in review.md Step 13.
REVIEWS_THRESHOLD = 5
DAYS_THRESHOLD = 2


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


def cmd_bump(args) -> int:
    wiki = Path(args.wiki_dir)
    meta = read_meta(wiki)
    meta["reviews_since"] = int(meta.get("reviews_since", 0)) + 1
    pr = int(args.pr_number)
    if pr > int(meta.get("last_processed_pr", 0)):
        meta["last_processed_pr"] = pr
    write_meta(wiki, meta)
    print(
        f"  [meta] bumped: reviews_since={meta['reviews_since']} last_processed_pr={meta['last_processed_pr']}",
        file=sys.stderr,
    )
    return 0


def cmd_check(args) -> int:
    wiki = Path(args.wiki_dir)
    meta = read_meta(wiki)
    trigger, reason = should_trigger_learn(meta)
    # On the "date passed but no PRs" skip, bump last_check so the next review
    # doesn't re-evaluate and log the same skip line. Only this branch mutates.
    if not trigger and int(meta.get("reviews_since", 0)) == 0 and days_since(meta["last_cleanup"]) >= DAYS_THRESHOLD:
        meta["last_check"] = _utc_now_iso()
        write_meta(wiki, meta)
    print(f"  [meta] {reason}", file=sys.stderr)
    # Exit 1 = trigger (signals caller to run /air:learn).
    # Exit 0 = skip. Matches the "exit code drives shell conditional" idiom.
    return 1 if trigger else 0


def cmd_reset(args) -> int:
    """Called after /air:learn finishes successfully. Resets the counter and
    records the cleanup timestamp + latest PR processed."""
    wiki = Path(args.wiki_dir)
    meta = read_meta(wiki)
    now = _utc_now_iso()
    meta["last_cleanup"] = now
    meta["last_check"] = now
    meta["reviews_since"] = 0
    pr = int(args.pr_number)
    if pr > int(meta.get("last_processed_pr", 0)):
        meta["last_processed_pr"] = pr
    write_meta(wiki, meta)
    print(f"  [meta] reset at {now} (last_processed_pr={meta['last_processed_pr']})", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    # __doc__ starts with a newline, so .splitlines()[0] is empty. Pick the
    # first non-blank line for a useful --help description.
    desc = next((l for l in (__doc__ or "").splitlines() if l.strip()), "")
    parser = argparse.ArgumentParser(description=desc)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bump = sub.add_parser("bump", help="Increment reviews_since after a successful review")
    p_bump.add_argument("--wiki-dir", required=True, help="Path to the checked-out wiki repo")
    p_bump.add_argument("--pr-number", required=True, type=int, help="PR number just reviewed")
    p_bump.set_defaults(fn=cmd_bump)

    p_check = sub.add_parser("check", help="Decide whether to trigger /air:learn (exit 1 = trigger)")
    p_check.add_argument("--wiki-dir", required=True)
    p_check.set_defaults(fn=cmd_check)

    p_reset = sub.add_parser("reset", help="Record a successful /air:learn run")
    p_reset.add_argument("--wiki-dir", required=True)
    p_reset.add_argument("--pr-number", required=True, type=int)
    p_reset.set_defaults(fn=cmd_reset)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
