"""GitHub REST helpers for the managed review driver.

Extracted from review.py (module split); GitHub auth headers consolidated
into `_gh_headers()`. All HTTP to api.github.com lives here: fetchers,
pagination, the review-comment POST with its 422 retry, and the formal
review-verdict POST.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests as req

# Diff hygiene lives in the shared lib (single-sourced with the CLI — the
# verdict.py/agent_md.py pattern). Re-exported here so existing
# `from github_client import apply_diff_hygiene / count_diff_changed_lines / …`
# call sites (review.py, the test suites) keep working unchanged.
_LIB = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))
from diff_hygiene import (  # noqa: E402,F401  (re-export — these four are imported from github_client by review.py / the test suites)
    DIFF_TRUNCATION_MARKER, apply_diff_hygiene, count_diff_changed_lines, _is_generated_path,
)
from pr_conversation import BOT_REVIEW_PREFIXES  # noqa: E402  (canonical ## Code Review prefixes incl. re-review)


def _gh_message(resp) -> str | None:
    """Parse the GitHub-controlled `message` field; None on non-JSON.

    Shared parse for the two scrubbed-summary helpers below — they differ
    only in fallback semantics (loggers want placeholders, keyword-matching
    callers want "")."""
    try:
        return resp.json().get("message")
    except ValueError:
        return None


def _github_error_message(resp) -> str:
    """Extract a scrubbed GitHub API error summary safe to log in CI."""
    msg = _gh_message(resp)
    if msg is None:
        msg = "(non-JSON response)"
    elif not msg:
        msg = "(no message)"
    return f"{resp.status_code} {msg}"


def _gh_headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    """Standard GitHub API auth headers (one builder for every call site)."""
    return {"Authorization": f"Bearer {token}", "Accept": accept}


class PartialPageError(RuntimeError):
    """A paginated GitHub walk failed mid-way.

    Raised instead of returning a partial list: callers could not
    distinguish "no items" from "truncated", and for the prior-review
    lookup that ambiguity caused a duplicate full review on an unchanged
    SHA (the skip gate saw "no prior comments"). Callers that genuinely
    prefer a degraded result catch this explicitly."""


# Connect/read timeouts for every GitHub call. `requests` has NO default
# timeout — a black-holed TCP connection used to hang the run until the
# workflow-level 95-min kill, orphaning the live session the shutdown
# hook exists to interrupt.
GH_TIMEOUT = (10, 30)
GH_RETRIES = 2
GH_RETRY_BACKOFF_SECS = 3.0


def _gh_request(
    method: str,
    url: str,
    *,
    token: str,
    accept: str = "application/vnd.github+json",
    retries: int = GH_RETRIES,
    timeout: tuple = GH_TIMEOUT,
    retry_timeouts: bool = True,
    **kwargs,
) -> "req.Response":
    """Single entrypoint for GitHub HTTP: timeout + bounded retry.

    Retries (with linear backoff) on 5xx responses and on
    connection/timeout exceptions — the transient classes where a blind
    second attempt is cheap and usually succeeds. 4xx responses return
    immediately (retrying can't change them; the 422 duplicate-comment
    special case keeps its own logic in _post_review_comment_with_retry).
    Raises the last network exception when retries are exhausted —
    callers treat that like any other fatal GitHub failure.

    `retry_timeouts=False` is for non-replay-safe POSTs (comment posts):
    a READ timeout means the request was sent and the response lost — the
    server may have committed it, so a blind re-POST risks a duplicate.
    Connection errors and 5xx stay retryable for those callers.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = req.request(
                method, url, headers=_gh_headers(token, accept=accept),
                timeout=timeout, **kwargs,
            )
        except (req.exceptions.ConnectionError, req.exceptions.Timeout) as e:
            # `retry_timeouts=False` (non-replay-safe POSTs) must NOT retry a
            # READ timeout — the request was sent and the response was lost, so
            # the server may have committed it. But a CONNECT timeout (and any
            # plain ConnectionError) means the connection never established and
            # nothing was sent — those are safe to retry even for POSTs.
            # ConnectTimeout subclasses BOTH Timeout and ConnectionError, so we
            # exclude it explicitly rather than letting the Timeout check eat it.
            if (
                not retry_timeouts
                and isinstance(e, req.exceptions.Timeout)
                and not isinstance(e, req.exceptions.ConnectTimeout)
            ):
                raise
            last_exc = e
            if attempt < retries:
                print(
                    f"  [warn] GitHub {method} {url}: {type(e).__name__} — "
                    f"retry {attempt + 1}/{retries}",
                    file=sys.stderr,
                )
                time.sleep(GH_RETRY_BACKOFF_SECS * (attempt + 1))
            continue
        if resp.status_code >= 500 and attempt < retries:
            print(
                f"  [warn] GitHub {method} {url}: {resp.status_code} — "
                f"retry {attempt + 1}/{retries}",
                file=sys.stderr,
            )
            time.sleep(GH_RETRY_BACKOFF_SECS * (attempt + 1))
            continue
        return resp
    assert last_exc is not None
    raise last_exc


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

    repo-D #37 hit a 422 cascade (run 25368789413, 2026-05-05):
    the prior fallback path posted near-duplicate content and GitHub
    rejected with 422; without diagnostic capture, the operator had no
    idea why.
    """
    url = (
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    )
    payload = {"body": body}
    resp = _gh_request("POST", url, token=token, json=payload, retry_timeouts=False)
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
    resp2 = _gh_request("POST", url, token=token, json=payload, retry_timeouts=False)
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
    return _gh_message(resp) or ""


def fetch_pr_metadata(repo: str, pr_number: int, token: str) -> dict:
    resp = _gh_request(
        "GET", f"https://api.github.com/repos/{repo}/pulls/{pr_number}", token=token,
    )
    if not resp.ok:
        print(f"Error fetching PR metadata: {_github_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    return resp.json()


# Invisible marker appended to every air formal verdict body. Lets a later
# run recognize its OWN prior verdicts regardless of which rotated bot account
# posted them — multi-PAT fleets post verdicts under different logins, and
# GitHub's reviewDecision blocks on ANY account whose latest review is
# CHANGES_REQUESTED, so an APPROVE under account B never clears a stale block
# orphaned under account A. An HTML comment → hidden in the rendered GitHub
# review UI. Only air's code ever writes this string, so matching it can never
# dismiss a human's review.
AIR_VERDICT_SENTINEL = "<!-- air-review-verdict -->"


def _is_air_verdict(review: dict, bot_logins: frozenset) -> bool:
    """True iff this PR review is one air posted. Identified by the verdict
    sentinel as the TRAILING marker of the body — air always appends it as the
    last line (`{body}\n\n{sentinel}`, both the managed and CLI paths), so an
    end-of-body match stays account-independent (works across rotated PATs and
    CLI dev accounts, which is the sentinel's whole point) while no longer
    matching a human review that merely QUOTES a prior air verdict mid-body.
    Falls back to an explicitly caller-allowlisted bot login (catches legacy
    pre-sentinel verdicts by identity). A human review matches neither — only
    air emits the trailing sentinel, so this never dismisses a genuine human
    block. (Binding to a fixed login list instead was rejected: it would break
    account-independence for the very rotated/CLI accounts this must recognize.)"""
    if (review.get("body") or "").rstrip().endswith(AIR_VERDICT_SENTINEL):
        return True
    return ((review.get("user") or {}).get("login") or "") in bot_logins


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
    # retry_timeouts=False: POST /reviews is non-idempotent and GitHub does
    # NOT dedupe reviews — a read-timeout retry would submit a SECOND formal
    # review. Same replay-safety posture as the comment POST.
    resp = _gh_request(
        "POST", f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
        token=token,
        json={"event": event, "body": f"{body}\n\n{AIR_VERDICT_SENTINEL}", "commit_id": commit_id},
        retry_timeouts=False,
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


def dismiss_review(repo: str, pr_number: int, review_id: int, token: str, message: str) -> bool:
    """Dismiss a single PR review (PUT .../reviews/{id}/dismissals). Best-effort:
    a missing-permission 403 or any other failure is logged and swallowed — the
    verdict is already posted, so a failed cleanup never breaks the run. Returns
    True on success."""
    resp = _gh_request(
        "PUT",
        f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals",
        token=token, json={"message": message, "event": "DISMISS"},
        retry_timeouts=False,
    )
    if not resp.ok:
        print(f"  [warn] could not dismiss review {review_id}: {_github_error_message(resp)}", file=sys.stderr)
        return False
    return True


def dismiss_stale_air_verdicts(
    repo: str, pr_number: int, token: str, current_login: str | None,
    bot_logins: frozenset = frozenset(), include_own: bool = False,
) -> int:
    """Clear the multi-PAT gate-orphan: dismiss prior CHANGES_REQUESTED reviews
    air left under a DIFFERENT bot account than the one just used.

    GitHub's reviewDecision blocks on ANY account whose latest review is
    CHANGES_REQUESTED, so an APPROVE posted under a rotated account never clears
    a stale block left by an earlier cycle under another bot account — the PR
    stays gated despite a correct APPROVE at HEAD. This dismisses those orphans.

    Safe by construction: only reviews air provably owns are touched — those
    carrying the verdict sentinel, or authored by an explicitly allowlisted bot
    login. A human's CHANGES_REQUESTED matches neither and is never dismissed.
    The posting account's own prior reviews are auto-superseded by GitHub and
    left alone (UNLESS include_own=True — see below). Best-effort; returns the
    count dismissed.

    `include_own=True` ALSO dismisses the posting account's own prior
    CHANGES_REQUESTED. That's required in AIR_NO_APPROVE (advisory) mode: a clean
    re-review posts a COMMENT, which does NOT supersede the same account's prior
    CHANGES_REQUESTED (only an APPROVE/REQUEST_CHANGES does), so without this the
    block would persist after the developer fixed the blockers. Still ownership-
    gated by the sentinel, so it never touches a human's review — and the just-
    posted COMMENT isn't CHANGES_REQUESTED, so it's never dismissed."""
    try:
        reviews = fetch_pr_reviews(repo, pr_number, token)
    except Exception as e:  # noqa: BLE001 — cleanup must never break the run
        print(f"  [warn] orphan-block cleanup skipped (review fetch failed): {str(e)[:120]}", file=sys.stderr)
        return 0
    dismissed = 0
    for r in reviews:
        if r.get("state") != "CHANGES_REQUESTED":
            continue
        login = (r.get("user") or {}).get("login") or ""
        if not include_own and current_login and login == current_login:
            continue  # GitHub auto-supersedes the posting account's own prior state
        if not _is_air_verdict(r, bot_logins):
            continue  # not air's verdict — never touch a human's block
        # Reason-aware message: same-account is only reachable via include_own
        # (AIR_NO_APPROVE advisory mode), where a clean COMMENT re-review can't
        # self-supersede this account's prior CHANGES_REQUESTED. Cross-account is
        # a stale block from a different air-posting identity — PAT rotation, or a
        # local air CLI verdict posted under a developer's own account.
        same_account = bool(current_login and login == current_login)
        if same_account:
            reason = "a clean advisory-mode re-review supersedes this account's earlier block"
        else:
            reason = "stale block from a different air-posting account (PAT rotation or a local CLI review)"
        if dismiss_review(
            repo, pr_number, r["id"], token,
            f"Superseded by air's latest verdict — {reason}.",
        ):
            dismissed += 1
            scope = "same-account advisory" if same_account else "cross-account"
            print(
                f"  [dismiss] cleared stale air CHANGES_REQUESTED by @{login} "
                f"(review {r['id']}) — {scope} gate-orphan",
                file=sys.stderr,
            )
    return dismissed


def local_diff_fallback(base_sha: str, head_sha: str, *, checkout_dir: str | None = None) -> str | None:
    """Compute a PR/inter diff LOCALLY when GitHub's REST diff endpoint refuses an
    oversized PR (406: "the diff exceeded the maximum number of files (300)").

    Uses three-dot `git diff base...head`, which matches GitHub's PR-diff
    (merge-base) semantics, from the PR-head checkout air already has
    (`AIR_TARGET_REPO`; present for every CI mode at fetch-depth 0). Best-effort:
    returns the raw unified diff, or None if there's no checkout / a SHA is
    unreachable / git fails — the caller then keeps its existing failure path.

    shell=False argv; the SHAs are GitHub-provided 40-hex, never user text. The
    caller applies `apply_diff_hygiene` to the result (same as the REST path), so
    a huge sync/promote PR then rides the normal stub + byte-cap instead of
    hard-failing the run."""
    checkout_dir = checkout_dir or os.environ.get("AIR_TARGET_REPO", "")
    if not (checkout_dir and base_sha and head_sha and os.path.isdir(checkout_dir)):
        return None

    def _run(args, timeout=180):
        try:
            return subprocess.run(
                ["git", "-C", checkout_dir, *args],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  [diff] local fallback git error: {str(e)[:120]}", file=sys.stderr)
            return None

    # Both endpoints must resolve locally. head is the checkout HEAD (present);
    # the base may need a best-effort fetch (GitHub allows fetching a reachable
    # SHA), mirroring the promote-sibling fetch pattern.
    for sha in (base_sha, head_sha):
        r = _run(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], timeout=30)
        if r is None:
            return None
        if r.returncode != 0:
            if _run(["fetch", "--no-tags", "origin", sha], timeout=120) is None:
                return None
            r2 = _run(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], timeout=30)
            if r2 is None or r2.returncode != 0:
                print(f"  [diff] local fallback: {sha[:8]} unreachable in checkout — giving up", file=sys.stderr)
                return None

    # Pin the diff format so apply_diff_hygiene's `a/`+`b/` path parsing and rename
    # handling match GitHub's fixed .diff regardless of the runner's git config
    # (a stray diff.noprefix / diff.renames=false could otherwise skew hygiene
    # path-matching or inflate the diff into the byte cap earlier).
    r = _run(["-c", "diff.noprefix=false", "-c", "diff.renames=true",
              "diff", f"{base_sha}...{head_sha}"], timeout=180)
    if r is None or r.returncode != 0:
        if r is not None:
            print(f"  [diff] local fallback git diff exit {r.returncode}: {r.stderr[:160]}", file=sys.stderr)
        return None
    return r.stdout


def _pr_base_head(repo: str, pr_number: int, token: str) -> tuple[str, str] | None:
    """(base_sha, head_sha) for a PR via a JSON GET — used only on the oversized-
    diff fallback path (fetch_pr_diff has no SHAs of its own). None on ANY error,
    including a RAISED _gh_request (it re-raises on retry exhaustion rather than
    returning) — so the 406 path degrades to the original fail-loud exit(1),
    never an uncaught traceback on this extra GET."""
    try:
        r = _gh_request("GET", f"https://api.github.com/repos/{repo}/pulls/{pr_number}", token=token)
    except req.RequestException:
        return None
    if not r.ok:
        return None
    try:
        d = r.json()
        return (d["base"]["sha"], d["head"]["sha"])
    except (ValueError, KeyError, TypeError):
        return None


def fetch_pr_diff(repo: str, pr_number: int, token: str) -> str:
    resp = _gh_request(
        "GET", f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        token=token, accept="application/vnd.github.v3.diff",
    )
    if resp.ok:
        return apply_diff_hygiene(resp.text)
    # Oversized PR (>300 files) → GitHub 406s the diff media type. Fall back to a
    # local `git diff` so a big sync/promote PR gets a (byte-capped) review
    # instead of a hard exit(1) that leaves the PR ungated. Only on 406 — other
    # errors (auth/404/5xx) stay fail-loud.
    if resp.status_code == 406:
        bh = _pr_base_head(repo, pr_number, token)
        local = local_diff_fallback(*bh) if bh else None
        if local is not None:
            print("  [diff] REST PR diff 406 (>300 files) — using local git diff fallback", file=sys.stderr)
            return apply_diff_hygiene(local)
    print(f"Error fetching PR diff: {_github_error_message(resp)}", file=sys.stderr)
    sys.exit(1)


def _github_paginate(url: str, token: str, max_pages: int | None = None) -> list[dict]:
    """Walk a GitHub list endpoint to completion and return all items.

    Raises PartialPageError on a mid-walk page failure (after _gh_request's
    own retries) — a partial list is indistinguishable from a short one,
    and that ambiguity caused duplicate full reviews when the prior-review
    lookup saw a truncated comment list. Callers that prefer a degraded
    result catch it explicitly.

    `max_pages` caps the walk for callers that only need the first few
    pages of a newest-first list (e.g. the promote sibling search). None
    (default) preserves the walk-to-completion behavior all other callers
    rely on.
    """
    items: list[dict] = []
    pages = 0
    while url:
        resp = _gh_request("GET", url, token=token)
        if not resp.ok:
            raise PartialPageError(
                f"page {pages + 1} of {url} failed after {len(items)} items: "
                f"{_github_error_message(resp)}"
            )
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
    resp = _gh_request("GET", "https://api.github.com/user", token=token)
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

    NOTE: this per-issue endpoint IGNORES `sort`/`direction` (only the
    list-comments-in-a-REPO endpoint honors them) and always returns
    ASCENDING by id (oldest-first). The params are kept only for URL
    symmetry with the bash CLI fetch — do NOT rely on them for ordering.
    Both consumers are order-independent by construction: `find_prior_review`
    selects the newest bot review by `created_at` (a first-match walk used to
    return the ORIGINAL review here and wedge the re-review baseline), and
    `filter_comments_after` re-sorts by id internally. (Caveat: an early
    partial-fetch return therefore yields the *oldest* slice, not the newest —
    not currently load-bearing.)
    """
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments?per_page=100&sort=created&direction=desc"
    return _github_paginate(url, token)


def fetch_recent_review_bodies(repo: str, token: str, limit: int = 30,
                               bot_login: str | None = None) -> list[dict]:
    """Return [{'pr': <num>, 'body': <## Code Review body>}] for the most-recent
    `limit` closed PRs that air reviewed — the input to the headless REVIEW-HISTORY
    (KAIROS) regeneration. Newest-first.

    Anti-spoof: only a comment whose body starts with `## Code Review\\n` AND
    (when `bot_login` is given) is authored by the bot counts — same guard as
    `find_prior_review`, so a PR participant can't inject a fake history row.
    Best-effort: a per-PR fetch error skips that PR (a partial history is fine).
    """
    try:
        prs = _github_paginate(
            f"https://api.github.com/repos/{repo}/pulls"
            f"?state=closed&sort=updated&direction=desc&per_page={min(limit, 100)}",
            token, max_pages=1)
    except PartialPageError:
        return []
    out: list[dict] = []
    for pr in prs:
        if len(out) >= limit:
            break
        num = pr.get("number")
        if not num:
            continue
        # Merged-only (parity with managed's "last N merged"): a closed-unmerged
        # PR is noise that crowds out reviewed PRs and wastes a comments fetch.
        if not pr.get("merged_at"):
            continue
        try:
            comments = fetch_issue_comments(repo, num, token)
        except PartialPageError:
            continue
        # newest qualifying ## Code Review comment for this PR
        review = None
        for c in comments:
            body = c.get("body") or ""
            # Canonical set (fresh + re-review); a strict "## Code Review\n" would
            # miss "## Code Review (Re-review)" and undercount the history.
            if not body.startswith(BOT_REVIEW_PREFIXES):
                continue
            if bot_login and (c.get("user") or {}).get("login") != bot_login:
                continue
            review = c  # comments are oldest-first; keep walking → ends on newest
        if review:
            out.append({"pr": num, "body": review["body"]})
    return out


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
    resp = _gh_request(
        "GET", f"https://api.github.com/repos/{repo}/compare/{base_sha}...{head_sha}",
        token=token, accept="application/vnd.github.v3.diff",
    )
    if not resp.ok:
        # Oversized inter-diff (>300 files) → 406. We already hold both SHAs, so
        # fall back to a local `git diff base...head` before degrading to None
        # (which would send the caller to a full review that 406s the same way).
        if resp.status_code == 406:
            local = local_diff_fallback(base_sha, head_sha)
            if local is not None:
                print("  [diff] REST compare 406 (>300 files) — using local git diff fallback", file=sys.stderr)
                return apply_diff_hygiene(local)
        print(f"Error fetching inter-diff: {_github_error_message(resp)}", file=sys.stderr)
        return None
    # Same hygiene as fetch_pr_diff — the promote overlap ratio divides one
    # changed-line count by the other, so both sides must see the same
    # stubbing or generated churn would skew the gate.
    return apply_diff_hygiene(resp.text)


def fetch_compare_status(repo: str, base_sha: str, head_sha: str, token: str) -> str | None:
    """GitHub's compare `status` for base...head ('ahead'|'behind'|'diverged'|
    'identical', HEAD relative to BASE), or None on API error.

    The origin-anchor ancestor gate (#198): origin is a SAFE ancestor of head iff
    status in {'ahead','identical'} (head is at or past origin → origin..head is a
    clean superset window, so file_touched can only widen monotonically). A
    'diverged'/'behind'/error result REJECTS the origin so the wider window can
    never pull in unrelated history (a rebase/force-push that rewrote the origin
    commit). JSON accept (default) — distinct from fetch_inter_diff's .v3.diff."""
    resp = _gh_request(
        "GET", f"https://api.github.com/repos/{repo}/compare/{base_sha}...{head_sha}",
        token=token,
    )
    if not resp.ok:
        return None
    try:
        return resp.json().get("status")
    except (ValueError, AttributeError):
        return None


_OWN_FILE_PAGES = 3   # #3d: cap this PR's own files at 300 — the overlap base set


def fetch_related_prs(
    repo: str, pr_number: int, token: str, *, max_scan: int = 50, max_report: int = 10,
) -> str:
    """Concurrent OPEN PRs touching the same files as this PR — the managed/headless
    parity for the CLI's sibling-PR overlap scan (#3d). Returns a rendered block body
    (one line per overlapping sibling, file-level) for `<related-prs>`, or "none".

    Purpose: let specialists flag merge/rebase conflicts, interacting subsystem
    changes, and reference implementations in other in-flight work. File-level
    overlap only — the CLI's same-region hunk-collision check needs local diffs we
    don't fetch here, so this is the conservative subset (a same-file overlap is
    flagged; whether the hunks actually collide is left to the agent).

    Best-effort + BOUNDED: examines at most `max_scan` open PRs (newest-activity
    first) and reports at most `max_report` overlapping siblings. EVERY file fetch
    is page-capped so cost can't multiply: this PR's files at `_OWN_FILE_PAGES`
    (300) and each sibling's at ONE page (100) — enough to detect overlap in
    practice; a sibling touching >100 files is matched on its first 100 (a missed
    overlap on a giant sibling is harmless for advisory context). ANY API error or
    empty result → "none" — non-load-bearing background context that must never
    block or fail a review (mirrors the CLI: a rate-limited scan is
    indistinguishable from "no siblings" by design). Bounded cost: ≤
    `_OWN_FILE_PAGES` (this PR's files) + 1 (open-PR list) + `max_scan` (one page
    per sibling) GitHub REST calls."""
    try:
        own = _github_paginate(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files?per_page=100",
            token, max_pages=_OWN_FILE_PAGES,
        )
        own_files = {f.get("filename") for f in own if f.get("filename")}
        if not own_files:
            return "none"
        opens = _github_paginate(
            f"https://api.github.com/repos/{repo}/pulls"
            f"?state=open&per_page=100&sort=updated&direction=desc",
            token, max_pages=max(1, (max_scan + 99) // 100),
        )
    except Exception:
        return "none"

    siblings: list[tuple[int, str, list[str]]] = []
    scanned = 0
    for pr in opens:
        num = pr.get("number")
        if num is None or num == pr_number:
            continue
        if scanned >= max_scan:
            break
        scanned += 1
        try:
            files = _github_paginate(
                f"https://api.github.com/repos/{repo}/pulls/{num}/files?per_page=100",
                token, max_pages=1,   # one page (100 files) is enough to detect overlap; bounds cost
            )
        except Exception:
            continue   # one unreadable sibling never aborts the scan
        overlap = sorted(own_files & {f.get("filename") for f in files if f.get("filename")})
        if overlap:
            siblings.append((num, pr.get("title") or "", overlap))
            if len(siblings) >= max_report:
                break
    if not siblings:
        return "none"

    lines = []
    for num, title, overlap in siblings:
        shown = overlap[:5]
        more = f" (+{len(overlap) - len(shown)} more)" if len(overlap) > len(shown) else ""
        lines.append(f"- #{num} ({title}) — same-file overlap: {', '.join(shown)}{more}")
    return "\n".join(lines)
