#!/usr/bin/env python3
"""
Merge GitHub PR conversation API responses into a single chronological
<pr-conversation> block for agent context.

Stdlib-only. Used both as a CLI (review.md / review-respond.md bash blocks
shell out to it) and as an importable function (managed/review.py adds the
plugin lib dir to sys.path and imports build_pr_conversation directly).

API shapes consumed (raw `gh api` JSON dumps):
- issues:  list[{user.login, body, created_at}]                  # /issues/<n>/comments
- reviews: list[{user.login, body, state, submitted_at, ...}]    # /pulls/<n>/reviews
- inline:  list[{user.login, body, path, line, created_at, ...}] # /pulls/<n>/comments

The output is a string ready to drop into the PR Context block. It returns
the literal "none" when there's nothing to show — keeps the caller's
prefix byte-stable across PRs of varying chattiness, which matters for
prompt-cache reuse across the four parallel review agents.
"""

import argparse
import html
import json
import sys
from pathlib import Path

DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_BODY = 1500

# Single source of truth for what the bot's own review comments look like.
# Used both here (to filter the bot's own ## Code Review out of the
# conversation block) and by managed/review.py (to detect a prior review
# for re-review delta tracking). Trailing `\n` guards against false
# matches on documentation lines like "## Code Reviewers Guide" — login
# filtering already handles spoofing, but defense in depth.
BOT_REVIEW_PREFIXES: tuple[str, ...] = (
    "## Code Review\n",
    "## Code Review (Re-review)\n",
)

# Header of the headless "run couldn't complete" diagnostic comment
# (managed/headless.py:_post_incomplete_comment builds its body from this, so the
# two can't drift). This is NOT a review — it must never be treated as a prior
# review by re-review/verdict detection (those key off BOT_REVIEW_PREFIXES ONLY),
# but it SHOULD be filtered from the conversation block fed to specialists (it's a
# stale status note with zero review value). Hence a separate tuple that only
# `_is_bot_self` unions in — see BOT_NONREVIEW_PREFIXES below.
RUN_INCOMPLETE_HEADER = "## air review — could not complete"

# Bot-authored comments that are NOT reviews but should still be dropped from the
# conversation context. Kept SEPARATE from BOT_REVIEW_PREFIXES on purpose: adding
# these there would make prior-review detection (managed/review.py, verdict.py,
# github_client.py) mistake a diagnostic note for a real review and wreck the
# re-review baseline. `## Review Response` (--respond) is deliberately absent —
# agents benefit from seeing it (see _is_bot_self).
BOT_NONREVIEW_PREFIXES: tuple[str, ...] = (
    RUN_INCOMPLETE_HEADER + "\n",
)


def _load_json_array(path: Path | None) -> list[dict]:
    """Load a JSON array from path. Returns [] on missing/empty/malformed —
    a transient `gh api` failure or a fresh PR with no comments yet must
    not break review."""
    if path is None:
        return []
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return []
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return data


def _attr_escape(s: str) -> str:
    return html.escape(s, quote=True)


def _normalize(entry: dict, kind: str) -> dict | None:
    """Map a raw API entry to a normalized record. Returns None if the
    entry is structurally invalid (no author) or shouldn't be surfaced
    (review umbrella with no body)."""
    user = entry.get("user") or {}
    author = user.get("login")
    if not author:
        return None
    body = (entry.get("body") or "").strip()
    # Reviews have submitted_at; comments have created_at. Either works
    # for chronological sort — fall back to "" so missing-timestamp
    # entries sort to the front consistently.
    ts = entry.get("created_at") or entry.get("submitted_at") or ""
    record = {
        "author": author,
        "body": body,
        "kind": kind,
        "ts": ts,
    }
    if kind == "inline":
        path = entry.get("path") or ""
        # Outdated inline comments (line moved across rebases) lose `line`
        # but keep `original_line`. Either is informative for the agent.
        line = entry.get("line") or entry.get("original_line") or ""
        record["path"] = f"{path}:{line}" if path and line else path
    elif kind == "review":
        state = entry.get("state") or ""
        # COMMENTED-with-no-body reviews are pure umbrellas for inline
        # children we already render separately — skip the noise.
        # PENDING reviews aren't visible to anyone until submitted; skip.
        if state == "PENDING":
            return None
        if state == "COMMENTED" and not body:
            return None
        record["state"] = state
    return record


def _is_bot_self(record: dict, bot_login: str | None) -> bool:
    """True if this entry is our own bot's '## Code Review' comment.

    Filtered out because re-review delta logic tracks those separately
    (FIXED/NOT-FIXED classification keys off finding numbers, not flat
    chronology). Login + prefix double-check: login alone would drop the
    bot's --respond flow comments ('## Review Response') which agents
    benefit from seeing.
    """
    if not bot_login:
        return False
    if record["author"] != bot_login:
        return False
    return record["body"].startswith(BOT_REVIEW_PREFIXES + BOT_NONREVIEW_PREFIXES)


def _truncate(body: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    return body[:max_chars].rstrip() + "[...]"


def _render(record: dict, max_body: int) -> str:
    parts = [
        f'author="{_attr_escape(record["author"])}"',
        f'kind="{record["kind"]}"',
    ]
    if record.get("path"):
        parts.append(f'path="{_attr_escape(record["path"])}"')
    if record.get("state"):
        parts.append(f'state="{_attr_escape(record["state"])}"')
    attrs = " ".join(parts)

    body = _truncate(record["body"], max_body)
    if not body:
        return f"<conv-comment {attrs} />"
    # Escape `<` `>` `&` so a comment body containing literal
    # `</conv-comment>` (or any markup) can't close the wrapper early
    # and smuggle instructions past the untrusted-input guard.
    safe_body = html.escape(body, quote=False)
    return f"<conv-comment {attrs}>{safe_body}</conv-comment>"


def build_pr_conversation(
    issues: list[dict],
    reviews: list[dict],
    inline: list[dict],
    bot_login: str | None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_body: int = DEFAULT_MAX_BODY,
) -> str:
    records: list[dict] = []
    for entries, kind in [(issues, "issue"), (reviews, "review"), (inline, "inline")]:
        for entry in entries:
            r = _normalize(entry, kind)
            if r is not None:
                records.append(r)

    records = [r for r in records if not _is_bot_self(r, bot_login)]
    records.sort(key=lambda r: r["ts"])

    total_post_filter = len(records)
    truncated = total_post_filter > max_entries
    if truncated:
        records = records[-max_entries:]

    if not records:
        return "none"

    lines: list[str] = []
    if truncated:
        lines.append(
            f'<conv-truncated total="{total_post_filter}" shown="{len(records)}"/>'
        )
    for r in records:
        lines.append(_render(r, max_body))
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build the <pr-conversation> block from gh-api JSON dumps."
    )
    parser.add_argument("--issues", help="path to /issues/<n>/comments JSON dump")
    parser.add_argument("--reviews", help="path to /pulls/<n>/reviews JSON dump")
    parser.add_argument("--inline", help="path to /pulls/<n>/comments JSON dump")
    parser.add_argument("--bot-login", default="", help="bot login to filter")
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    args = parser.parse_args(argv)

    block = build_pr_conversation(
        _load_json_array(Path(args.issues)) if args.issues else [],
        _load_json_array(Path(args.reviews)) if args.reviews else [],
        _load_json_array(Path(args.inline)) if args.inline else [],
        args.bot_login or None,
        max_entries=args.max_entries,
        max_body=args.max_body,
    )
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
