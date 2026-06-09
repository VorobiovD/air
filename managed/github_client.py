"""GitHub REST helpers for the managed review driver.

Extracted verbatim from review.py (module split). All HTTP to api.github.com
lives here: fetchers, pagination, the review-comment POST with its 422 retry,
and the formal review-verdict POST.
"""
import re
import sys
import time

import requests as req


def _github_error_message(resp) -> str:
    """Extract a scrubbed GitHub API error summary safe to log in CI."""
    try:
        msg = resp.json().get("message") or "(no message)"
    except ValueError:
        msg = "(non-JSON response)"
    return f"{resp.status_code} {msg}"


# Heuristic strings that indicate GitHub's 422 was caused by near-
# duplicate-comment detection (vs e.g. body-too-long or schema). On a
# duplicate-detection 422, retrying with the SAME body is guaranteed to
# 422 again — the retry would just be a 2s tax with no behavioral win.
# Skip the retry and surface the diagnostic. Conservative match list —
# expand only with real production cases.
_GH_DUPLICATE_HINTS: tuple[str, ...] = (
    "already exists",
    "duplicate",
)


def _post_review_comment_with_retry(
    repo: str, pr_number: int, body: str, token: str,
) -> "req.Response":
    """POST the review comment to the PR's issue-comments endpoint.

    Retries once on 422 after a 2s backoff EXCEPT when the response
    body indicates duplicate-comment detection — in that case, retry
    can't change the outcome, so we log + return after the first POST.

    Body diagnostics are scrubbed: only the GitHub-controlled `message`
    field reaches stderr (matching `_github_error_message`'s shape) so
    a happy-path 422 caused by, say, a too-long-body containing PR
    code snippets doesn't leak that snippet to CI logs.

    svc-transcribe #37 hit a 422 cascade (run 25368789413, 2026-05-05):
    the prior fallback path posted near-duplicate content and GitHub
    rejected with 422; without diagnostic capture, the operator had no
    idea why.
    """
    url = (
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {"body": body}
    resp = req.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 422:
        return resp
    msg = _gh_error_message_only(resp)
    print(
        f"  [warn] first POST returned 422 — message: {msg!r}",
        file=sys.stderr,
    )
    if any(hint in msg.lower() for hint in _GH_DUPLICATE_HINTS):
        print(
            "  [warn] message looks like duplicate-comment detection — "
            "skipping retry (re-POST with identical body would 422 again)",
            file=sys.stderr,
        )
        return resp
    time.sleep(2.0)
    resp2 = req.post(url, headers=headers, json=payload, timeout=30)
    if resp2.status_code == 422:
        msg2 = _gh_error_message_only(resp2)
        print(
            f"  [warn] retry POST also returned 422 — message: {msg2!r}",
            file=sys.stderr,
        )
    return resp2


def _gh_error_message_only(resp) -> str:
    """Pull the GitHub-controlled `message` field from a JSON error
    response. Returns an empty string on non-JSON or missing-field —
    callers that lower-case match against keyword hints handle "" safely.
    Mirrors `_github_error_message` but without the status-code prefix.
    """
    try:
        return resp.json().get("message") or ""
    except ValueError:
        return ""


def fetch_pr_metadata(repo: str, pr_number: int, token: str) -> dict:
    resp = req.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if not resp.ok:
        print(f"Error fetching PR metadata: {_github_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    return resp.json()


def submit_review_verdict(
    repo: str,
    pr_number: int,
    token: str,
    event: str,
    body: str,
    commit_id: str,
) -> None:
    """POST a formal pull-request review (APPROVE / REQUEST_CHANGES / COMMENT).

    `commit_id` MUST be the SHA the review actually examined. Without it
    GitHub attaches the verdict to the PR's *current* head — if the
    developer pushed new commits during our 28-min coordinator session,
    we'd silently approve (or block) unreviewed code while the comment
    body still says `Reviewed at: <old sha>`. Pinning to the reviewed
    SHA makes the verdict honest: GitHub shows it as a stale review on
    later commits and the next push triggers a fresh re-review.

    The CLI plugin's review.md Step 12 always submits a formal verdict
    in addition to the issue comment; managed mode used to skip this
    and only post the comment, leaving `reviewDecision` stuck at
    REVIEW_REQUIRED no matter what the review said. This helper closes
    that gap. Failures are logged but never fatal — the issue comment
    is already published, so the review's signal isn't lost.

    GitHub rejects self-reviews (PR author == reviewer) with 422.
    Caller is responsible for the own-PR guard.
    """
    resp = req.post(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"event": event, "body": body, "commit_id": commit_id},
    )
    if not resp.ok:
        print(
            f"  [warn] verdict submission failed ({event}): "
            f"{_github_error_message(resp)} — issue comment was posted, "
            f"branch-protection state unchanged",
            file=sys.stderr,
        )
        return
    print(f"  Verdict: {event} (commit {commit_id[:8]})")


def fetch_pr_diff(repo: str, pr_number: int, token: str) -> str:
    resp = req.get(
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3.diff"},
    )
    if not resp.ok:
        print(f"Error fetching PR diff: {_github_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    return resp.text


def _github_paginate(url: str, token: str, max_pages: int | None = None) -> list[dict]:
    """Walk a GitHub list endpoint to completion and return all items.
    On a page failure, logs to stderr and returns whatever has been
    collected so far — callers see this as "empty or truncated" and
    cannot currently distinguish the two. Acceptable because both
    failure modes lead to a full-review fallback, which is the safe
    (more expensive) choice.

    `max_pages` caps the walk for callers that only need the first few
    pages of a newest-first list (e.g. the promote sibling search). None
    (default) preserves the walk-to-completion behavior all other callers
    rely on.
    """
    items: list[dict] = []
    pages = 0
    while url:
        resp = req.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        if not resp.ok:
            print(f"Error GETting {url}: {_github_error_message(resp)}", file=sys.stderr)
            return items
        items.extend(resp.json())
        pages += 1
        if max_pages is not None and pages >= max_pages:
            break
        link = resp.headers.get("Link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else None
    return items


def fetch_bot_login(token: str) -> str | None:
    """Query GET /user to learn the authenticated bot's login, so the
    prior-review lookup can filter on author. Without this filter, any PR
    participant could post a fake `## Code Review` comment to suppress or
    mis-steer the next review."""
    resp = req.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if not resp.ok:
        print(f"Error fetching bot identity: {_github_error_message(resp)}", file=sys.stderr)
        return None
    return resp.json().get("login")


def fetch_issue_comments(repo: str, pr_number: int, token: str) -> list[dict]:
    """Fetch all issue comments on a PR in one paginated pass.

    Single fetch source so `find_prior_review` and `filter_comments_after`
    can share the full comment list instead of paginating the same
    endpoint twice per re-review (doubles API calls on long-discussion
    PRs).

    `sort=created&direction=desc` is symmetric with the bash CLI fetch
    URL and gives newest-first ordering. In the happy path the merger
    re-sorts records anyway. The win is partial-fetch resilience: if
    `_github_paginate` returns early on a transient `not resp.ok`, the
    caller gets the *newest* slice (what specialists need) instead of
    the oldest slice. `find_prior_review` reads this same list and is
    written for desc ordering — keep them in sync.
    """
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments?per_page=100&sort=created&direction=desc"
    return _github_paginate(url, token)


def fetch_pr_reviews(repo: str, pr_number: int, token: str) -> list[dict]:
    """Fetch all top-level PR reviews (APPROVED / CHANGES_REQUESTED / COMMENTED).

    Distinct from issue comments — these carry a `state` field and are
    submitted via the GitHub review UI. Used by `pr_conversation.build_pr_conversation`
    so reviewer agents see formal approval state, not just chat.
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews?per_page=100"
    return _github_paginate(url, token)


def fetch_pr_review_comments(repo: str, pr_number: int, token: str) -> list[dict]:
    """Fetch inline (file:line) review comments on a PR.

    Distinct from issue comments — these are anchored to a specific path
    and line via the top-level `path` and `line` fields (`position` also
    exists but is GitHub's legacy diff-position int and is often null on
    outdated comments). Used by `pr_conversation.build_pr_conversation` so reviewer
    agents can locate prior inline feedback when picking up a PR
    mid-conversation. Same `sort=created&direction=desc` as issue
    comments for partial-fetch resilience.
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments?per_page=100&sort=created&direction=desc"
    return _github_paginate(url, token)


def fetch_inter_diff(
    repo: str, base_sha: str, head_sha: str, token: str
) -> str | None:
    """Fetch the diff between two SHAs via GitHub's compare endpoint.

    Uses three-dot semantics (`base...head` in URL). For a fast-forward
    PR branch this produces the same diff as two-dot; after a force-push
    that GC'd base_sha or rewrote history, the endpoint 404s. Distinguishes
    API failure from genuinely-empty diff:

    - Success (200, possibly empty body) → return str (may be "")
    - API error (404 / 5xx / rate-limit) → return None so the caller can
      fall back to a full review instead of silently skipping.
    """
    resp = req.get(
        f"https://api.github.com/repos/{repo}/compare/{base_sha}...{head_sha}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.v3.diff"},
    )
    if not resp.ok:
        print(f"Error fetching inter-diff: {_github_error_message(resp)}", file=sys.stderr)
        return None
    return resp.text
