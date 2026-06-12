#!/usr/bin/env python3
"""Direct unit tests for the verdict-gating decision tree (managed/verdict.py).

This is the APPROVE / REQUEST_CHANGES logic that drives GitHub branch
protection on real repos. Before this file it had NO direct coverage — only
indirect exercise through two backfill tests — so a regex drift in
count_blockers / should_request_changes / _count_gating_unfixed could silently
un-gate a real blocker (approve a PR that must be blocked) with nothing to
catch it. (2026-06-10 audit finding F3.)

Also covers extract_reviewed_at_sha SHA-normalization (finding b — uppercase
footer must not bypass the skip gate) and has_conflict_markers (finding F2 —
the deterministic conflict-marker gate).

Pure functions, no network. Run: python -m pytest managed/test-verdict.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from verdict import (  # noqa: E402
    count_blockers,
    should_request_changes,
    _count_gating_unfixed,
    extract_reviewed_at_sha,
    has_conflict_markers,
)

HEAD = "fc3b2e03546153449edba2a224dbbbfff58a14b6"


# ---------------------------------------------------------------------------
# count_blockers
# ---------------------------------------------------------------------------

def test_count_blockers_none():
    assert count_blockers("## Code Review\n\nLooks good.\n\n### Strengths\n- nice\n") == 0


def test_count_blockers_fresh_section():
    body = (
        "## Code Review\n\n### Blockers\n\n"
        "**1. SQL injection** — bad\n\n"
        "**2. Auth bypass** — worse\n\n"
        "### Medium\n\n**3. style** — meh\n"
    )
    assert count_blockers(body) == 2  # only the 2 under Blockers, not the medium


def test_count_blockers_rereview_subsection():
    body = (
        "## Code Review (Re-review)\n\n### New Findings\n\n#### Blockers\n\n"
        "**1. new blocker** — x\n\n#### Medium / Low / Nits\n\n**2. nit** — y\n"
    )
    assert count_blockers(body) == 1


def test_count_blockers_no_section_is_zero():
    assert count_blockers("plain text, no headings, **1.** not under blockers") == 0


# ---------------------------------------------------------------------------
# should_request_changes — fresh
# ---------------------------------------------------------------------------

def test_fresh_blockers_request_changes():
    body = "## Code Review\n\n### Blockers\n\n**1. bug** — x\n"
    rc, reason = should_request_changes(body)
    assert rc is True and "blocker" in reason


def test_fresh_no_blockers_approves():
    body = "## Code Review\n\n### Medium\n\n**1. nit** — x\n\n### Strengths\n- ok\n"
    rc, reason = should_request_changes(body)
    assert rc is False and reason == ""


# ---------------------------------------------------------------------------
# should_request_changes — re-review (the svc-tx #37 class)
# ---------------------------------------------------------------------------

def test_rereview_new_blocker_gates():
    body = (
        "## Code Review (Re-review)\n\n### Previous Findings Status\n\n"
        "- **#1** [medium] — FIXED — done\n\n"
        "### New Findings\n\n#### Blockers\n\n**2. regression** — x\n"
    )
    rc, reason = should_request_changes(body)
    assert rc is True and "new blocker" in reason


def test_rereview_unfixed_prior_blocker_gates():
    body = (
        "## Code Review (Re-review)\n\n### Previous Findings Status\n\n"
        "- **#1** [blocker] — NOT FIXED — still broken\n"
    )
    rc, reason = should_request_changes(body)
    assert rc is True and "prior blocker" in reason


def test_rereview_unfixed_medium_does_NOT_gate():
    # The repo-D #37 case: a deferred medium must not keep the PR red.
    body = (
        "## Code Review (Re-review)\n\n### Previous Findings Status\n\n"
        "- **#1** [medium] — NOT FIXED — author punted to follow-up\n"
        "- **#2** [low] — PARTIALLY FIXED — partial\n"
    )
    rc, reason = should_request_changes(body)
    assert rc is False and reason == ""


def test_rereview_all_fixed_approves():
    body = (
        "## Code Review (Re-review)\n\n### Previous Findings Status\n\n"
        "- **#1** [blocker] — FIXED — addressed\n"
        "- **#2** [medium] — DISPUTED — accepted rationale\n"
    )
    rc, _ = should_request_changes(body)
    assert rc is False


# ---------------------------------------------------------------------------
# _count_gating_unfixed — severity + defense-in-depth
# ---------------------------------------------------------------------------

def test_gating_blocker_not_fixed_counts():
    body = "## Code Review (Re-review)\n- **#1** [blocker] — NOT FIXED — x\n"
    assert _count_gating_unfixed(body) == 1


def test_gating_medium_not_fixed_does_not_count():
    body = "## Code Review (Re-review)\n- **#1** [medium] — NOT FIXED — x\n"
    assert _count_gating_unfixed(body) == 0


def test_gating_missing_severity_defaults_to_blocker():
    # Pre-v1.12 bodies with no [severity] tag must keep gating (conservative).
    body = "## Code Review (Re-review)\n- **#1** — NOT FIXED — legacy body\n"
    assert _count_gating_unfixed(body) == 1


def test_gating_blocker_deferred_counts_defense_in_depth():
    # The verifier prompt forbids DEFERRED on a blocker; the gate enforces it
    # independently in case of prompt drift / model-tier misclassification.
    body = "## Code Review (Re-review)\n- **#1** [blocker] — DEFERRED — should not happen\n"
    assert _count_gating_unfixed(body) == 1


def test_gating_nonblocker_deferred_does_not_count():
    body = "## Code Review (Re-review)\n- **#1** [low] — DEFERRED — fine\n"
    assert _count_gating_unfixed(body) == 0


def test_gating_whitespace_normalized_status():
    body = "## Code Review (Re-review)\n- **#1** [blocker] — NOT  FIXED — double space\n"
    assert _count_gating_unfixed(body) == 1


# ---------------------------------------------------------------------------
# extract_reviewed_at_sha — finding (b): normalize case so the skip gate hits
# ---------------------------------------------------------------------------

def test_extract_sha_lowercases_uppercase_footer():
    body = f"## Code Review\n\nbody\n\nReviewed at: {HEAD.upper()}\n"
    extracted = extract_reviewed_at_sha(body)
    assert extracted == HEAD  # lowercased → skip gate `== head_sha` now matches


def test_extract_sha_passthrough_lowercase():
    body = f"## Code Review\n\nbody\n\nReviewed at: {HEAD}\n"
    assert extract_reviewed_at_sha(body) == HEAD


def test_extract_sha_none_when_absent():
    assert extract_reviewed_at_sha("## Code Review\n\nno footer\n") is None


def test_skip_gate_roundtrip_uppercase_footer():
    # The whole point of finding (b): a review posted with an uppercase footer
    # must let the NEXT run's `extract == head_sha` skip gate fire.
    posted_body = f"## Code Review\n\nReviewed at: {HEAD.upper()}\n"
    assert extract_reviewed_at_sha(posted_body) == HEAD  # head_sha is lowercase from GitHub


# ---------------------------------------------------------------------------
# has_conflict_markers — finding (F2): deterministic gate
# ---------------------------------------------------------------------------

def test_conflict_marker_from_git_check_phrase():
    assert has_conflict_markers("", "path/to/file.py:42: leftover conflict marker") is True


def test_conflict_marker_open_in_diff():
    diff = "@@ -1,3 +1,5 @@\n context\n+<<<<<<< HEAD\n+ours\n"
    assert has_conflict_markers(diff, "") is True


def test_conflict_marker_close_in_diff():
    diff = "@@ -1,3 +1,5 @@\n+>>>>>>> feature-branch\n"
    assert has_conflict_markers(diff, "") is True


def test_no_false_positive_on_equals_run():
    # A 7-equals run is common in docs/ASCII art — must NOT trip the gate.
    diff = "@@ -1,2 +1,3 @@\n+## Section\n+=======\n+some underline\n"
    assert has_conflict_markers(diff, "") is False


def test_no_conflict_clean_diff():
    diff = "@@ -1,2 +1,3 @@\n context\n+a normal added line\n-a removed line\n"
    assert has_conflict_markers(diff, "git diff --check: trailing whitespace.") is False


def test_conflict_marker_empty_inputs():
    assert has_conflict_markers("", "") is False


# ---------------------------------------------------------------------------
# Shared-contract CLI surface (lib/verdict.py --decide) — the exact entry
# point review.md Step 12 invokes, exercised as a real subprocess so the
# CLI and managed modes provably share one decision implementation.
# ---------------------------------------------------------------------------

import subprocess

_LIB = str(Path(__file__).parent.parent / "plugins" / "air" / "lib" / "verdict.py")


def _decide(body: str) -> str:
    out = subprocess.run(
        [sys.executable, _LIB, "--decide"], input=body,
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_cli_decide_fresh_blocker_requests_changes():
    line = _decide("## Code Review\n\n### Blockers\n\n**1. bug** — x\n")
    assert line.startswith("request-changes\t")
    assert "blocker" in line


def test_cli_decide_clean_body_approves():
    assert _decide("## Code Review\n\nAll good.\n") == "approve"


def test_cli_decide_rereview_unfixed_medium_approves():
    # The svc-tx #37 class through the CLI entry point: same semantics as
    # the in-process should_request_changes call managed makes.
    body = ("## Code Review (Re-review)\n\n### Previous Findings Status\n\n"
            "- **#1** [medium] — NOT FIXED — punted\n")
    assert _decide(body) == "approve"


def test_cli_decide_rereview_unfixed_blocker_gates():
    body = ("## Code Review (Re-review)\n\n### Previous Findings Status\n\n"
            "- **#1** [blocker] — NOT FIXED — still broken\n")
    assert _decide(body).startswith("request-changes\t")


def test_cli_count_blockers():
    out = subprocess.run(
        [sys.executable, _LIB, "--count-blockers"],
        input="## Code Review\n\n### Blockers\n\n**1. a** — x\n\n**2. b** — y\n",
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "2"


def test_cli_no_action_is_usage_error():
    out = subprocess.run(
        [sys.executable, _LIB], input="", capture_output=True, text=True,
    )
    assert out.returncode == 2


def test_managed_verdict_resolves_to_the_lib_implementation():
    # Whatever `import verdict` resolves to on this sys.path (the managed
    # shim, or the lib directly when plugins/air/lib precedes managed/),
    # its functions must come from the ONE shared lib file — a stale copy
    # under managed/ would silently fork the gating contract.
    import inspect
    import verdict as resolved
    src = Path(inspect.getsourcefile(resolved.should_request_changes)).resolve()
    assert src == Path(_LIB).resolve()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
