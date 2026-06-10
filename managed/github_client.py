"""GitHub REST helpers for the managed review driver.

Extracted from review.py (module split); GitHub auth headers consolidated
into `_gh_headers()`. All HTTP to api.github.com lives here: fetchers,
pagination, the review-comment POST with its 422 retry, and the formal
review-verdict POST.
"""
import os
import re
import sys
import time

import requests as req


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

    svc-transcribe #37 hit a 422 cascade (run 25368789413, 2026-05-05):
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
        token=token, json={"event": event, "body": body, "commit_id": commit_id},
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


# Generated/vendored content travels ~11-13× per review (the coordinator
# re-emits the diff to every specialist + the verifier, 5-6 of those as
# Sonnet output) without ever changing a verdict — a lockfile diff is noise
# to every lens. Stub those file segments to one visible line and cap the
# total so a pathological PR can't blow out the coordinator context.
# Manifests outside vendored dirs (package.json, composer.json, ...) stay
# whole: the security checklist's supply-chain item reads them. Lockfiles
# are stubbed ONLY when the same directory's manifest also changed in this
# diff — a lockfile-only change (resolver/integrity swap with no manifest
# touch) is the supply-chain attack shape and stays fully visible.
# Residual, by design: pairing ANY same-dir manifest edit re-enables
# stubbing of arbitrarily large lockfile churn — but the manifest then
# surfaces in review and the stub marker carries the changed-line count,
# so the signal to demand the lockfile is never silent. Stub lines start
# with neither `+` nor `-`, so changed-line counts (promote overlap,
# codex-skip) ignore stubbed files on both sides of any ratio. Conflict
# markers inside a stubbed file are caught by precomp's `git diff --check`
# warnings on checkout-enabled runs (AIR_TARGET_REPO set); checkout-less
# runs lose that net for stubbed segments — the marker keeps the path
# visible.
DIFF_MAX_BYTES = int(os.environ.get("AIR_DIFF_MAX_BYTES", "500000"))
# Cap-marker line prefix. review.py keys codex-skip off it: a truncated
# re-review delta must NOT skip codex (real changes may live in the
# omitted tail, and codex reads the git tree, not this diff). Detection is
# LINE-START anchored at the consumer — diff body lines always start with
# `+`/`-`/space, so PR content cannot forge a line beginning with this.
DIFF_TRUNCATION_MARKER = "[air: diff truncated"

# Lockfile → the manifest whose same-directory change justifies stubbing.
_LOCKFILE_MANIFESTS = {
    "package-lock.json": "package.json",
    "yarn.lock": "package.json",
    "pnpm-lock.yaml": "package.json",
    "bun.lock": "package.json",
    "bun.lockb": "package.json",
    "composer.lock": "composer.json",
    "Cargo.lock": "Cargo.toml",
    "poetry.lock": "pyproject.toml",
    "uv.lock": "pyproject.toml",
    "go.sum": "go.mod",
    "Gemfile.lock": "Gemfile",
}
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".snap")
# Whole-segment match only (`dist` matches `pkg/dist/x.js`, not
# `src/distance.py`). `build/` is deliberately absent — it collides with
# committed source in too many layouts.
_GENERATED_SEGMENTS = {"dist", "node_modules", "__snapshots__", "vendor"}


def _is_generated_path(path: str) -> bool:
    if not path:
        return False
    basename = path.rsplit("/", 1)[-1]
    if basename in _LOCKFILE_MANIFESTS:
        return True
    if basename.endswith(_GENERATED_SUFFIXES):
        return True
    return any(seg in _GENERATED_SEGMENTS for seg in path.split("/")[:-1])


def _segment_path(segment: str) -> str:
    """The b/-side path from a `diff --git a/x b/x` header (rename-safe)."""
    header = segment.splitlines()[0] if segment else ""
    return header.rsplit(" b/", 1)[-1] if " b/" in header else ""


def count_diff_changed_lines(diff: str) -> int:
    """Count added/removed lines in a unified diff (excl. +++/--- headers).

    The one shared sizing metric: promote overlap, codex-skip, and hygiene
    stub counts all use this definition (review.py re-exports it)."""
    n = 0
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            n += 1
    return n


def _stub_decision(path: str, changed_paths: set[str]) -> bool:
    """Should this generated-classified path actually be stubbed?

    Lockfiles: only when the paired manifest in the SAME directory also
    changed — dependency-bump noise gets stubbed, a lockfile-only change
    (the supply-chain evasion shape) stays fully reviewable. All other
    generated-classified paths are stubbed unconditionally."""
    basename = path.rsplit("/", 1)[-1]
    manifest = _LOCKFILE_MANIFESTS.get(basename)
    if manifest is None:
        return True
    prefix = path[: -len(basename)]  # "" at root, "dir/" otherwise
    return f"{prefix}{manifest}" in changed_paths


def _should_stub(path: str, changed_paths: set[str]) -> bool:
    """The complete stubbing decision — classification AND lockfile pairing.

    Callers outside apply_diff_hygiene must use this, not bare
    `_is_generated_path` (which says "stubbing candidate", not "stub it":
    lockfiles classify as generated but only stub when their same-dir
    manifest also changed)."""
    return _is_generated_path(path) and _stub_decision(path, changed_paths)


def apply_diff_hygiene(diff: str, *, max_bytes: int | None = None) -> str:
    """Stub generated-file segments, then enforce the global size cap.

    Both transformations leave an explicit in-diff marker (and a stdout
    decision-log line), so reviewers — human and agent — can always see
    what was omitted. Nothing is dropped silently.
    """
    if not diff:
        return diff
    budget = DIFF_MAX_BYTES if max_bytes is None else max_bytes
    segments = re.split(r"(?m)^(?=diff --git )", diff)
    paths = [_segment_path(s) for s in segments]
    changed_paths = {
        p for s, p in zip(segments, paths) if s.startswith("diff --git ")
    }
    kept: list[str] = []
    kept_paths: list[str] = []
    for seg, path in zip(segments, paths):
        if not seg.startswith("diff --git ") or not _should_stub(path, changed_paths):
            kept.append(seg)
            kept_paths.append(path)
            continue
        n = count_diff_changed_lines(seg)
        header = seg.splitlines()[0]
        kept.append(
            f"{header}\n[air: {path}: {n} changed lines omitted "
            f"(generated/vendored)]\n"
        )
        kept_paths.append(path)
        print(f"  diff hygiene: stubbed {path} ({n} changed lines)")
    result = "".join(kept)
    if len(result.encode("utf-8", errors="replace")) <= budget:
        return result

    def _marker(show_paths: list[str], n_omitted: int) -> str:
        # Paths are tail-truncated to 60 chars so 5 of them can't blow the
        # budget the marker exists to enforce.
        shown = ", ".join(p[-60:] for p in show_paths)
        extra = n_omitted - len(show_paths)
        suffix = f", … +{extra} more" if extra > 0 else ""
        named = f": {shown}{suffix}" if show_paths else ""
        return (
            f"{DIFF_TRUNCATION_MARKER} at {budget} bytes — "
            f"{n_omitted} file(s) omitted{named}]\n"
        )

    # Greedy first-fit at file boundaries: a single oversized segment is
    # omitted on its own — it must not drag down the (possibly small)
    # segments after it. The selection reserves room for the largest
    # marker we could emit (5 fully-truncated paths), then the marker
    # shrinks its shown-path list until the whole result fits. Guarantee:
    # output ≤ budget whenever budget ≥ the path-less marker (~80 bytes);
    # below that floor the marker is emitted anyway — visibility beats a
    # degenerate cap.
    capped: list[str] = []
    omitted: list[str] = []
    used = 0
    reserve = len(_marker(["x" * 60] * 5, len(kept)).encode("utf-8", errors="replace"))
    limit = max(0, budget - reserve)
    for seg, path in zip(kept, kept_paths):
        size = len(seg.encode("utf-8", errors="replace"))
        if used + size <= limit:
            capped.append(seg)
            used += size
        else:
            omitted.append(path or "(preamble)")
    for n_shown in (5, 4, 3, 2, 1, 0):
        marker = _marker(omitted[:n_shown], len(omitted))
        if used + len(marker.encode("utf-8", errors="replace")) <= budget:
            break
    capped.append(marker)
    print(
        f"  [warn] diff hygiene: {len(omitted)} file segment(s) over the "
        f"{budget}-byte cap omitted: {', '.join(omitted[:5])}"
        f"{'' if len(omitted) <= 5 else f', … +{len(omitted) - 5} more'}",
        file=sys.stderr,
    )
    return "".join(capped)


def fetch_pr_diff(repo: str, pr_number: int, token: str) -> str:
    resp = _gh_request(
        "GET", f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
        token=token, accept="application/vnd.github.v3.diff",
    )
    if not resp.ok:
        print(f"Error fetching PR diff: {_github_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    return apply_diff_hygiene(resp.text)


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
    resp = _gh_request(
        "GET", f"https://api.github.com/repos/{repo}/compare/{base_sha}...{head_sha}",
        token=token, accept="application/vnd.github.v3.diff",
    )
    if not resp.ok:
        print(f"Error fetching inter-diff: {_github_error_message(resp)}", file=sys.stderr)
        return None
    # Same hygiene as fetch_pr_diff — the promote overlap ratio divides one
    # changed-line count by the other, so both sides must see the same
    # stubbing or generated churn would skew the gate.
    return apply_diff_hygiene(resp.text)
