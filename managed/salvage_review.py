#!/usr/bin/env python3
"""Salvage a finished review from an orphaned session and post it.

When a CI run dies after its coordinator session was created (job cancel,
runner kill, network loss), the session usually keeps running server-side
and finishes a complete, fully-billed `## Code Review` that no runner is
left to extract — observed live 2026-06-12: a canceled promote review's
session completed 4 minutes after the cancel with an 8K-char review nobody
posted, and the re-dispatch spent another ~$5 reviewing the same PR. This
tool drains that session and posts the review for $0 of new inference.

Usage:
    export ANTHROPIC_API_KEY=...   # key for the workspace that ran the session
    export AIR_BOT_TOKEN=...       # token to post with (review attribution)
    python salvage_review.py <owner/repo> <pr_number>                # dry-run, newest session
    python salvage_review.py <owner/repo> <pr_number> --session-id sesn_...
    python salvage_review.py <owner/repo> <pr_number> --post         # actually post

The extraction is the production gating contract (`_extract_review_body`):
the salvaged body's `Reviewed at:` footer must SHA-match the PR's CURRENT
head — if the developer pushed after the orphaned run, salvage refuses
rather than posting a review of stale code. Verdict posts only on open PRs
(GitHub 422s verdicts on closed/merged). Pattern learning and the learn
counter are intentionally skipped — salvage is post-only.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anthropic import Anthropic  # noqa: E402

from github_client import (  # noqa: E402
    fetch_pr_metadata,
    _post_review_comment_with_retry,
    submit_review_verdict,
)
from session_runner import TERMINAL_SESSION_STATUSES  # noqa: E402
from verdict import _extract_review_body, should_request_changes  # noqa: E402
from review import _ensure_respond_footer  # noqa: E402  (same --respond footer restoration as managed/headless)

# Exact coordinator labels (title shape: "{label} — {repo}"). Anchored
# match — a prefix tuple was redundant ("air-coordinator-ma…" already
# startswith "air-coordinator") and unanchored against future agents.
COORDINATOR_LABELS = frozenset({"air-coordinator", "air-coordinator-ma"})


def _collect_agent_text(events) -> str:
    """Join agent.message text blocks in event order — the same rule
    run_session applies live (skip the runtime's literal '[empty message]'
    inter-turn placeholders)."""
    parts: list[str] = []
    for ev in events:
        if getattr(ev, "type", "") != "agent.message":
            continue
        for block in getattr(ev, "content", None) or []:
            text = getattr(block, "text", None)
            if text and text.strip() != "[empty message]":
                parts.append(text)
    return "".join(parts).strip()


def _drain_events(client, session_id: str) -> list:
    """All session events, walking next_page cursor pages."""
    events: list = []
    cursor = None
    for _ in range(50):
        kwargs: dict = {"limit": 200}
        if cursor is not None:
            kwargs["page"] = cursor
        page = client.beta.sessions.events.list(session_id, **kwargs)
        events.extend(getattr(page, "data", None) or [])
        cursor = getattr(page, "next_page", None)
        if not cursor:
            break
    else:
        print(f"  [warn] event drain stopped at the 50-page cap with a "
              f"continuation cursor still present ({len(events)} events) — "
              f"the final review message may be missing", file=sys.stderr)
    return events


def _find_newest_coordinator_session(client, repo: str, pr_number: int) -> str | None:
    """Newest coordinator session for this repo — PR-scoped when possible.

    Titles are '{label} — {repo}' (no PR number), so on a repo with
    concurrent reviews a title match alone can pick the wrong session.
    Sessions created since v1.30 carry metadata.pr (build_session_metadata);
    prefer an exact metadata match, fall back to the newest title match for
    pre-metadata sessions (the downstream footer-SHA validation still
    rejects a wrong pick — this just makes the right pick likelier)."""
    suffix = f" — {repo}"
    title_match = None
    for s in client.beta.sessions.list(limit=50).data:
        title = getattr(s, "title", "") or ""
        if not (title.endswith(suffix) and title[: -len(suffix)] in COORDINATOR_LABELS):
            continue
        meta = getattr(s, "metadata", None) or {}
        if isinstance(meta, dict) and meta.get("pr") == str(pr_number):
            return s.id
        if title_match is None:
            title_match = s.id
    return title_match


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("pr_number", type=int)
    ap.add_argument("--session-id", default="", help="explicit session to salvage (default: newest coordinator session titled for the repo)")
    ap.add_argument("--post", action="store_true", help="post comment + verdict (default: dry-run print)")
    args = ap.parse_args()

    bot_token = os.environ.get("AIR_BOT_TOKEN", "")
    if not bot_token or not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY and AIR_BOT_TOKEN.", file=sys.stderr)
        return 2

    client = Anthropic()
    sid = args.session_id or _find_newest_coordinator_session(client, args.repo, args.pr_number)
    if not sid:
        print(f"no coordinator session found titled for {args.repo}", file=sys.stderr)
        return 1
    print(f"session: {sid}")

    session = client.beta.sessions.retrieve(sid)
    status = getattr(session, "status", "?")
    print(f"session status: {status}")
    if status not in TERMINAL_SESSION_STATUSES:
        print(f"session status {status!r} is not terminal — salvage targets "
              "finished orphans; wait for idle (or interrupt it first).",
              file=sys.stderr)
        return 1

    meta = fetch_pr_metadata(args.repo, args.pr_number, bot_token)
    head_sha = meta["head"]["sha"]
    pr_state = meta.get("state", "open")

    text = _collect_agent_text(_drain_events(client, sid))
    if not text:
        print("no agent.message text in the session — nothing to salvage", file=sys.stderr)
        return 1
    body, extracted = _extract_review_body(text, head_sha)
    if not extracted:
        print(
            f"no `## Code Review` body with a `Reviewed at:` footer matching the "
            f"PR's CURRENT head {head_sha[:8]} — the PR may have moved since the "
            f"orphaned run (posting a stale review would mislead). First 500 chars "
            f"of session text:\n{text[:500]}",
            file=sys.stderr,
        )
        return 1

    request_changes, reason = should_request_changes(body)
    verdict = "REQUEST_CHANGES" if request_changes else "APPROVE"
    print(f"extracted review: {len(body)} chars; verdict would be {verdict}"
          + (f" ({reason})" if reason else ""))

    # `extracted` is guaranteed True here (early-return above otherwise), so the
    # body is a real `## Code Review` — restore the --respond hint the extractor
    # truncated (same fix as managed/headless).
    body = _ensure_respond_footer(body)

    if not args.post:
        print("\n" + "=" * 60 + "\nDRY RUN — not posting. Review below:\n" + "=" * 60)
        print(body)
        return 0

    resp = _post_review_comment_with_retry(args.repo, args.pr_number, body, bot_token)
    if not resp.ok:
        print(f"comment POST failed: HTTP {resp.status_code}", file=sys.stderr)
        return 1
    print("review comment posted")
    if pr_state == "open":
        # GitHub 422s a REQUEST_CHANGES review with an empty body (APPROVE
        # accepts one) — always send a short body so the gating verdict
        # can't silently fail.
        submit_review_verdict(
            args.repo, args.pr_number, bot_token, verdict,
            "Salvaged review — see the review comment above.", head_sha,
        )
        print(f"verdict POST attempted: {verdict} @ {head_sha[:8]} "
              f"(a failure would be logged as [warn] above)")
    else:
        print(f"PR is {pr_state} — verdict skipped (GitHub rejects verdicts on closed PRs)")
    print("NOTE: pattern learning + learn counter skipped (salvage is post-only).")
    print("NOTE: the conflict-marker verdict backstop and own-PR guard are NOT\n"
          "applied here (no diff fetch) — if the PR may contain unresolved\n"
          "conflict markers, eyeball the dry-run body before --post.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
