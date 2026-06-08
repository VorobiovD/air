#!/usr/bin/env python3
"""Unit tests for the promote fast-path — review._detect_promote_fastpath and
review._count_diff_changed_lines.

Pure logic + seam-mocked: no live network. Importing review.py pulls in
anthropic + requests, so run inside the managed venv:

    python managed/test-promote-fastpath.py

Also works under pytest. The detector's HTTP seams (_github_paginate,
fetch_issue_comments, fetch_inter_diff, fetch_pr_diff) are monkeypatched on the
review module; the pure helpers it composes (find_prior_review,
extract_reviewed_at_sha, _count_diff_changed_lines) run for real.

Covers: changed-line counting (header exclusion), the 0.80 overlap boundary
(fires/falls back), the head-prefix gate short-circuiting before any network
call, sibling selection (most-recent merged promote by merged_at — open /
unmerged / self / non-promote-head excluded, and list-order distinct from
merged-order so it locks in the max-by-merged_at fix), every
fallback-returns-None path (no sibling, sibling never reviewed, sibling missing
a Reviewed-at SHA, compare failure, unknown bot identity), and the
build_pr_context `prior_pr_number` provenance plumbing (present when set,
byte-absent when None).
"""
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import review  # noqa: E402
from review import (  # noqa: E402
    _count_diff_changed_lines,
    _detect_promote_fastpath,
    build_pr_context,
)

BOT = "air-bot"
HEAD = "fc3b2e03546153449edba2a224dbbbfff58a14b6"   # current PR head, 40-char hex
SIB_SHA = "0123456789abcdef0123456789abcdef01234567"  # sibling's Reviewed-at SHA


def _meta(head_ref, base_ref="main"):
    return {"head": {"ref": head_ref}, "base": {"ref": base_ref}}


def _diff(n_add, n_del=0):
    """A unified diff with n_add added + n_del removed lines, wrapped in the
    headers (`diff --git`, `---`, `+++`, `@@`, context) that the counter must
    exclude."""
    lines = ["diff --git a/f b/f", "--- a/f", "+++ b/f", "@@ -1,1 +1,1 @@", " ctx"]
    lines += [f"+added {i}" for i in range(n_add)]
    lines += [f"-removed {i}" for i in range(n_del)]
    return "\n".join(lines) + "\n"


def _review_comment(sha=SIB_SHA, cid=999, login=BOT):
    body = f"## Code Review\n\nFindings.\n\nReviewed at: {sha}\n" if sha \
        else "## Code Review\n\nNo footer here.\n"
    return {"user": {"login": login}, "body": body, "id": cid}


# Closed-PR list as the API returns it under sort=updated (NOT merged_at order).
# #48 is the answer: merged + promote head + not the current PR + newest
# merged_at among valid promotes. Critically, the first VALID promote in *list
# order* (#47) is NOT the newest-merged — so this fixture only passes under the
# max-by-merged_at selection, not a naive break-on-first-valid. #49 (newest
# overall merge) is a non-promote head and must be excluded; #51 (newer promote
# merge than #48) is the current PR and must be excluded as self.
CANDIDATES = [
    {"number": 50, "merged_at": None, "head": {"ref": "promote/staging-to-main-10"}},   # open
    {"number": 49, "merged_at": "2026-06-07T00:00:00Z", "head": {"ref": "feature/x"}},  # non-promote head
    {"number": 51, "merged_at": "2026-06-06T12:00:00Z", "head": {"ref": "promote/staging-to-main-09"}},  # self
    {"number": 47, "merged_at": "2026-06-05T00:00:00Z", "head": {"ref": "promote/staging-to-main-07"}},  # valid but OLDER — first in list order
    {"number": 48, "merged_at": "2026-06-06T00:00:00Z", "head": {"ref": "promote/staging-to-main-08"}},  # ← newest-merged promote, later in list
]


@contextmanager
def patched(**overrides):
    saved = {k: getattr(review, k) for k in overrides}
    for k, v in overrides.items():
        setattr(review, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(review, k, v)


def _boom(*a, **k):
    raise AssertionError("network seam called when it should not have been")


# --- _count_diff_changed_lines ----------------------------------------------

def test_count_excludes_headers_and_context():
    assert _count_diff_changed_lines(_diff(20, 0)) == 20
    assert _count_diff_changed_lines(_diff(7, 5)) == 12


def test_count_empty_diff_is_zero():
    assert _count_diff_changed_lines("") == 0
    assert _count_diff_changed_lines(None) == 0


# --- head-prefix gate: no network -------------------------------------------

def test_non_promote_head_short_circuits_before_network():
    with patched(_github_paginate=_boom, fetch_issue_comments=_boom,
                 fetch_inter_diff=_boom, fetch_pr_diff=_boom):
        assert _detect_promote_fastpath(
            "o/r", 51, _meta("feature/foo"), HEAD, BOT, "tok") is None


def test_unknown_bot_identity_returns_none():
    with patched(_github_paginate=_boom):
        assert _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, None, "tok") is None


# --- sibling selection ------------------------------------------------------

def test_picks_most_recent_merged_promote_sibling():
    with patched(
        _github_paginate=lambda url, token, max_pages=None: CANDIDATES,
        fetch_issue_comments=lambda repo, num, token: [_review_comment()],
        fetch_inter_diff=lambda repo, b, h, token: _diff(20),
        fetch_pr_diff=lambda repo, num, token: _diff(100),
    ):
        result = _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, BOT, "tok")
    assert result is not None
    sib_review, sib_sha, sib_num = result
    # #48 wins on newest merged_at among valid promotes — NOT #47 (first valid
    # in list order). Self (#51), open (#50), non-promote (#49) all excluded.
    assert sib_num == 48
    assert sib_sha == SIB_SHA
    assert sib_review["id"] == 999


def test_no_merged_sibling_returns_none():
    only_open = [{"number": 50, "merged_at": None,
                  "head": {"ref": "promote/staging-to-main-10"}}]
    with patched(
        _github_paginate=lambda url, token, max_pages=None: only_open,
        fetch_issue_comments=_boom, fetch_inter_diff=_boom, fetch_pr_diff=_boom,
    ):
        assert _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, BOT, "tok") is None


# --- sibling-review resolution fallbacks ------------------------------------

def test_sibling_never_reviewed_returns_none():
    with patched(
        _github_paginate=lambda url, token, max_pages=None: CANDIDATES,
        fetch_issue_comments=lambda repo, num, token: [],  # no air review
        fetch_inter_diff=_boom, fetch_pr_diff=_boom,
    ):
        assert _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, BOT, "tok") is None


def test_sibling_review_without_sha_returns_none():
    with patched(
        _github_paginate=lambda url, token, max_pages=None: CANDIDATES,
        fetch_issue_comments=lambda repo, num, token: [_review_comment(sha=None)],
        fetch_inter_diff=_boom, fetch_pr_diff=_boom,
    ):
        assert _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, BOT, "tok") is None


def test_compare_failure_returns_none():
    with patched(
        _github_paginate=lambda url, token, max_pages=None: CANDIDATES,
        fetch_issue_comments=lambda repo, num, token: [_review_comment()],
        fetch_inter_diff=lambda repo, b, h, token: None,  # 404 / GC'd base
        fetch_pr_diff=_boom,
    ):
        assert _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, BOT, "tok") is None


# --- overlap gate -----------------------------------------------------------

def _overlap_result(inter_changed, full_changed):
    with patched(
        _github_paginate=lambda url, token, max_pages=None: CANDIDATES,
        fetch_issue_comments=lambda repo, num, token: [_review_comment()],
        fetch_inter_diff=lambda repo, b, h, token: _diff(inter_changed),
        fetch_pr_diff=lambda repo, num, token: _diff(full_changed),
    ):
        return _detect_promote_fastpath(
            "o/r", 51, _meta("promote/staging-to-main-09"), HEAD, BOT, "tok")


def test_overlap_at_threshold_fires():
    # inter 20 / full 100 -> overlap 0.80 == threshold -> fires.
    assert _overlap_result(20, 100) is not None


def test_overlap_below_threshold_falls_back():
    # inter 21 / full 100 -> overlap 0.79 < threshold -> None (full review).
    assert _overlap_result(21, 100) is None


def test_overlap_high_fires():
    # Near-identical promote: inter 2 / full 200 -> overlap 0.99 -> fires.
    assert _overlap_result(2, 200) is not None


# --- prior_pr_number provenance plumbing (build_pr_context) ------------------

def _full_meta(head_ref="promote/staging-to-main-09"):
    return {
        "user": {"login": "dev"},
        "title": "Promote staging to main",
        "body": "promotion body",
        "number": 51,
        "base": {"ref": "main", "sha": "b" * 40},
        "head": {"ref": head_ref, "sha": HEAD},
        "additions": 10, "deletions": 2, "changed_files": 3, "commits": 4,
    }


def test_provenance_line_present_when_prior_pr_number_set():
    ctx = build_pr_context(
        _full_meta(), "o/r", mode="re-review",
        prior_review_body="## Code Review\n\nx\n",
        prior_sha=SIB_SHA, prior_pr_number=48)
    assert "predecessor promote PR #48" in ctx
    assert "carried from" in ctx


def test_provenance_absent_when_prior_pr_number_none():
    # Default (None) must be byte-identical to a normal same-PR re-review.
    ctx = build_pr_context(
        _full_meta(), "o/r", mode="re-review",
        prior_review_body="## Code Review\n\nx\n",
        prior_sha=SIB_SHA, prior_pr_number=None)
    assert "predecessor promote PR" not in ctx
    assert "carried from" not in ctx


_TESTS = [v for k, v in sorted(globals().items())
          if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    sys.exit(1 if failed else 0)
