"""The review-gating contract: body parsing + APPROVE/REQUEST_CHANGES
decision (pure functions, no network; stdlib-only like every lib/ module).

THE single source for both delivery modes — `managed/verdict.py` is a thin
re-export shim, and the CLI's review.md Step 12 invokes this file directly
(`python3 $AIR_PLUGIN_ROOT/lib/verdict.py --decide < review-comment.md`) —
so the two paths can never gate the same review body differently. Covers:
blocker counting, prior-status parsing, the severity-aware re-review gating
decision (only unfixed BLOCKERS gate; DEFERRED-on-blocker gates as defense
in depth), the deterministic conflict-marker gate, and the SHA-validated
`## Code Review` body extractor.
"""
import json
import os
import re
import sys
from pathlib import Path

# This dir hosts the sibling shared modules. When this file is loaded by
# path (the managed shim, or review.md's direct invocation) the dir isn't
# implicitly importable — add it (idempotent).
_AIR_LIB_DIR = str(Path(__file__).resolve().parent)
if _AIR_LIB_DIR not in sys.path:
    sys.path.insert(0, _AIR_LIB_DIR)

from pr_conversation import BOT_REVIEW_PREFIXES  # noqa: E402

# The developer-response hint appended to a POSTED review body (after the
# `Reviewed at:` footer). SINGLE SOURCE — review.py and solo_prompt.py both use
# it, so a command rename (`--respond` → …) is one edit with a static link. NOT a
# gating string: it sits after `Reviewed at:`, outside every _extract_review_body
# / gate / skip-gate boundary.
RESPOND_HINT = "> After fixing, run `/air:review --respond` to verify and reply."


def count_blockers(review_body: str) -> int:
    """Count `**N. ...` numbered entries under a Blockers heading.

    Handles both fresh review (`### Blockers`) and re-review's new-
    findings subsection (`#### Blockers`) via a permissive heading match.
    Returns 0 if no Blockers section exists.
    """
    section = _BLOCKERS_SECTION_RE.search(review_body)
    if not section:
        return 0
    return len(_BLOCKER_ENTRY_RE.findall(section.group(1)))


def _count_gating_unfixed(review_body: str) -> int:
    """Count prior findings that should block re-review approval.

    Walks the `### Previous Findings Status` entries:
    - FIXED / DISPUTED / DEFERRED (non-blocker): never gate.
    - DEFERRED on a blocker: gates (defense-in-depth against the verifier
      emitting `[blocker] — DEFERRED` despite the prompt's instruction).
    - NOT FIXED / PARTIALLY FIXED: gates only if severity is `blocker`.
      Medium/low/nit unfixed entries surface as warnings in the comment
      body but no longer flip the verdict — see repo-D #37 for
      the production case where one medium-severity test-coverage
      recommendation kept a PR red across 13 consecutive re-reviews
      while the developer intentionally deferred it to a follow-up.

    Severity defaults to `blocker` (gating) when the verifier omits the
    `[severity]` tag — preserves conservative gating on legacy v1.10
    bodies emitted before severity tags existed. Without this default,
    upgrading to v1.12 would silently un-gate any pre-v1.12 prior body
    whose findings (real blockers among them) lack `[severity]` tags.
    """
    count = 0
    for m in _PRIOR_STATUS_RE.finditer(review_body):
        severity = (m.group(2) or "blocker").lower()
        # `re.sub` to normalize "NOT  FIXED" / "PARTIALLY  FIXED" with
        # any whitespace shape into the canonical form for set lookup.
        status = re.sub(r"\s+", " ", m.group(3).upper()).strip()
        if status in _GATING_STATUSES and severity in _GATING_SEVERITIES:
            count += 1
            continue
        if status == _BLOCKER_DEFERRED_STATUS and severity == "blocker":
            # Verifier prompt forbids DEFERRED for blockers; gate enforces
            # independently. Catches prompt drift, edge-case dispute flows,
            # or a verifier that misclassifies under model-tier swap.
            count += 1
    return count


def extract_prior_statuses(prior_body: str) -> list[tuple[int, str, str]]:
    """Parse a prior re-review's `Previous Findings Status` block.

    Returns `(finding_num, severity, status)` triples in source order.
    Severity is normalized lowercase, status uppercase + whitespace
    collapsed. Returns an empty list if the prior body has no parseable
    entries (e.g. the prior review was a fresh review with no prior-
    statuses block, or a malformed re-review).

    Used by the carry-forward suppression rule: when the verifier is
    about to emit `NOT FIXED` for finding #N and the immediately prior
    review already reported `NOT FIXED` for the same finding, the
    verifier promotes it to `DEFERRED` (for non-blocker severities) so
    a non-actionable recommendation doesn't keep gating the PR. See
    repo-D #37 finding #2: 13 consecutive `NOT FIXED` rounds on
    a medium-severity test-coverage recommendation that the developer
    intentionally deferred to a follow-up.

    Severity defaults to `blocker` for missing tags, matching the gate
    side (`_count_gating_unfixed`). This is the safer default in carry-
    forward too: a missing-tag NOT FIXED entry won't be auto-promoted
    to DEFERRED on the next round (carry-forward only fires for non-
    blockers), so a real legacy blocker keeps reappearing as NOT FIXED
    until explicitly addressed.
    """
    # `_PRIOR_STATUS_RE` captures (num, severity, status) — share the same
    # compiled pattern with `_count_gating_unfixed` so the gate counter and
    # the carry-forward parser can't drift on shape (severity enum, status
    # enum, anchor, dash). The finding-number capture is a no-op for the
    # gate's count-only iteration.
    triples: list[tuple[int, str, str]] = []
    for m in _PRIOR_STATUS_RE.finditer(prior_body or ""):
        # `\d+` capture is always parseable — no try/except needed.
        num = int(m.group(1))
        severity = (m.group(2) or "blocker").lower()
        status = re.sub(r"\s+", " ", m.group(3).upper()).strip()
        triples.append((num, severity, status))
    return triples


def format_prior_statuses_block(prior_body: str) -> str:
    """Render the `<prior-round-statuses>` block for the verifier_task.

    Empty string when there's nothing to carry — the verifier_task then
    omits the carry-forward rule entirely (round 2 of any PR, since the
    round-1 fresh review has no Previous Findings Status block).
    """
    triples = extract_prior_statuses(prior_body)
    if not triples:
        return ""
    lines = "\n".join(
        f"  - #{num} [{sev}] — {status}"
        for num, sev, status in triples
    )
    return f"<prior-round-statuses>\n{lines}\n</prior-round-statuses>"


def should_request_changes(review_body: str, floor_exposures: bool = True) -> tuple[bool, str]:
    """Decide whether to submit REQUEST_CHANGES instead of APPROVE.

    Returns (request_changes, reason). The verdict drives `reviewDecision`
    and branch-protection state.

    - Fresh review: REQUEST_CHANGES if any blockers exist OR any blocker-class
      exposure was FLOORED (a `[sec:<cat>]`-tagged finding the model placed
      below blocker — see `count_category_floored`; the deterministic
      "active exposure = blocker" enforcement, so a weaker tier rating a real
      PII/authz/credential exposure "medium" no longer silently un-gates).
    - Re-review: REQUEST_CHANGES if any NEW blockers exist OR any prior
      finding originally classified as `blocker` is still NOT FIXED /
      PARTIALLY FIXED / DEFERRED OR any blocker-class exposure was floored.
      Medium / low / nit prior findings left unfixed do NOT gate.
      A developer can clear a blocker gate by either fixing, explicitly
      disputing (verifier marks DISPUTED), or — for prompt-edge cases —
      escalating to a human reviewer.
      A body takes the re-review branch when it carries the `(Re-review)`
      header OR a `### Previous Findings Status` section (`_is_rereview_body`
      — M7: header-only detection let a suffix-less re-review body fall to the
      fresh branch and un-gate its NOT FIXED prior blockers).

    `floor_exposures=False` (kill switch, wired to AIR_CATEGORY_FLOOR by
    callers) disables the floor. The floor is inert on bodies without
    `[sec:...]` tags, so a disabled / tag-less run is byte-identical to the
    pre-floor gate.
    """
    blockers = count_blockers(review_body)
    floored, floored_cats = count_category_floored(review_body) if floor_exposures else (0, [])
    floor_note = f"; +{floored} floored exposure(s) [{', '.join(floored_cats)}]" if floored else ""
    exposure_reason = f"{floored} blocker-class exposure(s) [{', '.join(floored_cats)}] floored to blocker"
    if _is_rereview_body(review_body):
        unfixed = _count_gating_unfixed(review_body)
        if blockers > 0 and unfixed > 0:
            return True, f"{blockers} new blocker(s), {unfixed} prior blocker(s) still unfixed{floor_note}"
        if blockers > 0:
            return True, f"{blockers} new blocker(s){floor_note}"
        if unfixed > 0:
            return True, f"{unfixed} prior blocker(s) still unfixed{floor_note}"
        if floored > 0:
            return True, exposure_reason
        return False, ""
    if blockers > 0:
        return True, f"{blockers} blocker(s){floor_note}"
    if floored > 0:
        return True, exposure_reason
    return False, ""


def no_approve_enabled() -> bool:
    """`AIR_NO_APPROVE` advisory-gate mode (off by default): air may still submit
    REQUEST_CHANGES on blockers, but NEVER APPROVE — a clean review posts a
    neutral COMMENT instead. For a repo where a bot APPROVE isn't yet sanctioned
    (e.g. a broad dev-facing repo). Because a COMMENT does NOT supersede the same
    account's prior CHANGES_REQUESTED (only an APPROVE/REQUEST_CHANGES does), a
    clean re-review in this mode dismisses air's OWN prior block explicitly
    (github_client.dismiss_stale_air_verdicts include_own=True) so it still
    clears when the developer fixes the blockers."""
    return os.environ.get("AIR_NO_APPROVE", "").strip().lower() in ("1", "true", "yes")


def resolve_verdict_event(request_changes: bool) -> str:
    """Map the gate decision to the GitHub review `event`, honoring AIR_NO_APPROVE.
    Blockers → REQUEST_CHANGES in both modes; a clean review APPROVEs normally,
    but posts a COMMENT (no approval) when no-approve mode is on."""
    if request_changes:
        return "REQUEST_CHANGES"
    return "COMMENT" if no_approve_enabled() else "APPROVE"


# The v2 verdict banner is a GitHub alert (`> [!CAUTION]` / `> [!NOTE]`) whose
# severity is model-written PROSE. `normalize_verdict_banner` forces that banner
# to agree with the deterministic gate. Match the alert line and the bold lead
# phrase that opens the banner's verdict line.
_BANNER_ALERT_RE = re.compile(r"(?m)^>[ \t]*\[!(?:CAUTION|WARNING|NOTE|TIP|IMPORTANT)\][ \t]*$")
# `> **Changes requested — …blockers-to-consider…**` — the over-escalation lead.
# Non-greedy to the first closing `**`, so trailing counts (`** 3 fixed · …`) survive.
_BANNER_CHANGES_REQUESTED_RE = re.compile(
    r"(?mi)^(>[ \t]*\*\*)[ \t]*changes requested\b[^\n]*?(\*\*)")
# The first "findings" marker — a section heading, a `**N.` blocker entry, or a
# `- **#N**` re-review status line. The verdict banner always precedes all of
# these; an alert AFTER the first marker is quoted inside a finding, not the banner.
_FINDINGS_MARKER_RE = re.compile(r"(?m)^(?:#{3,4}[ \t]|\*\*\d+\.|- \*\*#\d)")


def normalize_verdict_banner(body: str, *, request_changes: bool) -> str:
    """Force the top v2 verdict banner to match the deterministic gate.

    The verifier writes the `> [!CAUTION]`/`> [!NOTE]` banner + its bold verdict
    line as free prose and can DIVERGE from the gate — most often over-escalation:
    a re-review carrying unfixed *medium* findings emitting `[!CAUTION]` +
    "Changes requested" despite 0 blockers and a non-gating (COMMENT/APPROVE)
    verdict (observed live). This rewrites ONLY:
      1. the alert type → `[!CAUTION]` when `request_changes` else `[!NOTE]`
         (both directions, so the banner is never softer OR harsher than the gate);
      2. on a non-gating verdict, a `**Changes requested …**` lead → `**No blockers.**`
         (trailing counts preserved).

    GATE-SAFE by construction: every verdict parser (`count_blockers`,
    `should_request_changes`, `count_category_floored`, `_extract_review_body`,
    the `- **#N**` re-review status lines, `[sec:]` tags) anchors on the
    `### Blockers` section / status lines / tags — NEVER the banner. So this
    changes only what a human reads, never the verdict; the body re-parses to
    the identical gate. Idempotent. No-op on a legacy/flat body (no alert line).
    """
    m = _BANNER_ALERT_RE.search(body)
    if not m:
        return body  # legacy/flat format or a body with no verdict banner
    # Only the TOP banner is the verdict banner. If the first alert line falls
    # AFTER the first findings marker (a heading / `**N.` / `- **#N**` line), it's
    # an alert quoted inside a finding, not the banner — leave it alone. The real
    # banner always sits in the preamble, right under the `## Code Review` header.
    fm = _FINDINGS_MARKER_RE.search(body)
    if fm and fm.start() < m.start():
        return body
    want = "CAUTION" if request_changes else "NOTE"
    body = body[:m.start()] + f"> [!{want}]" + body[m.end():]
    if not request_changes:
        body = _BANNER_CHANGES_REQUESTED_RE.sub(r"\1No blockers.\2", body, count=1)
    return body


# The clean-review COMMENT body in AIR_NO_APPROVE mode — single-sourced so the
# three submission sites (review.py main + backfill, headless.py) can't drift.
# Deliberately mode-NEUTRAL: it must NOT name AIR_NO_APPROVE or announce that an
# approval was withheld. In this mode air simply doesn't approve (it posts a
# COMMENT rather than an APPROVE); the gate mode is never advertised on the PR,
# so the comment reads like any other clean-review summary.
NO_APPROVE_VERDICT_BODY = (
    "No blockers found. See review comment for medium/low/nit findings."
)


# Open/close conflict markers as ADDED diff lines. We require the 7-char
# `<<<<<<<` / `>>>>>>>` run (git's marker length) at the start of an added
# line — these never occur in real source, so precision is ~100%. We do NOT
# match `=======` (the middle marker): a 7-equals run is common in RST/ASCII
# headers and would false-positive. The open/close pair is sufficient to
# detect an unresolved conflict.
_CONFLICT_MARKER_RE = re.compile(r"^\+(?:<{7}|>{7})", re.MULTILINE)


def has_conflict_markers(diff: str = "", diff_check_warnings: str = "") -> bool:
    """True if the change introduces a merge-conflict marker.

    CLAUDE.md states "conflict markers in the PR diff = automatic blocker."
    That was only ever an instruction to the model (advisory); a model that
    missed it could still APPROVE. This is the deterministic detector the
    verdict gate uses to FORCE REQUEST_CHANGES independent of the model.

    Two signals, OR'd: (1) `git diff --check`'s own "leftover conflict
    marker" phrase (authoritative, but needs the local clone — present only
    when precomp ran); (2) a high-precision scan of the raw diff for added
    open/close marker lines (always available, covers the no-clone path).
    """
    if diff_check_warnings and "leftover conflict marker" in diff_check_warnings:
        return True
    return bool(diff and _CONFLICT_MARKER_RE.search(diff))


# Require a full 40-char SHA. A shorter match would break the strict
# `prior_sha == head_sha` equality at the skip gate, silently triggering a
# costly full review instead of no-op.
REVIEWED_AT_RE = re.compile(r"Reviewed at:\s*([0-9a-f]{40})", re.IGNORECASE)


# Counts numbered findings (`**N. ...`) under a Blockers heading.
# Fresh review uses `### Blockers`; re-review nests new blockers under
# `#### Blockers` inside `### New Findings (introduced since last review)`.
# Match 3-or-4 hashes to cover both shapes; section terminates on the
# next heading at the same OR shallower depth, so blocker counts don't
# bleed into adjacent Medium/Low/Nits.
#
# The heading may carry the v2 "calm suffix" a model can mirror from the
# sibling headings (`### Medium — consider fixing`): `### Blockers — must fix`,
# `### Blockers - must fix`, or a count `### Blockers (2)`. A BARE-only match
# (`Blockers\s*$`) silently counted 0 on such a heading → a non-security
# blocker un-gated (audit H5; the `[sec:]` floor only backstops security
# categories). The tolerated suffix is NARROW — a SPACE-DELIMITED dash suffix
# (` — … ` / ` - … `) or a parenthesized integer count (` (N)`) — so it can
# never be satisfied by a DISTINCT heading (`### Blockers Resolved`,
# `### Blockers: Resolved`, `### Blockers (Resolved)`, `### Blockers-Resolved`),
# which would miscount a re-review "resolved" summary's entries as new blockers.
# Emitted headings stay bare regardless (.air-checks.sh Check I) — this
# tolerance is only a drift safety-net. Section termination (next heading) is
# unchanged, so a suffix can't bleed into Medium/Low.
_BLOCKERS_SECTION_RE = re.compile(
    r"^#{3,4}\s+Blockers(?:\s+[-—]\s+[^\n]*|\s+\(\d+\))?\s*$\n(.*?)(?=^#{1,4}\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


_BLOCKER_ENTRY_RE = re.compile(r"^\*\*\d+\.", re.MULTILINE)


# --- Deterministic exposure floor (fresh-gate determinism) -------------------
# The security lens tags each finding with the 31-item-checklist bucket that
# fired, e.g. `[sec:pii-exposure]`. A finding whose bucket is a data-exposure /
# access-control / injection class gates as a blocker REGARDLESS of the model's
# own severity label — the deterministic "active exposure = blocker"
# enforcement (advisory→enforced, same move as has_conflict_markers and the
# re-review ledger). Closes the fresh-review gap where a weaker tier finds a
# real exposure but rates it "medium" and silently un-gates it (the 06-12
# Sonnet bench: org-wide PII + RBAC bypass, both found, both rated medium,
# both would have APPROVED). The model classifies into a bucket (reliable
# across tiers); verdict.py assigns the gate severity (ours, deterministic).
_BLOCKER_CATEGORIES = frozenset({
    "pii-exposure", "phi-exposure", "data-exposure", "sensitive-data-exposure",
    "authz-bypass", "authn-bypass", "auth-bypass", "broken-access-control",
    "idor", "privilege-escalation",
    "leaked-credential", "secret-exposure", "hardcoded-secret",
    "sqli", "injection", "rce", "ssrf", "deserialization",
})
_SEC_TAG_RE = re.compile(r"\[sec:([a-z0-9-]+)\]", re.IGNORECASE)


def count_category_floored(review_body: str) -> tuple[int, list[str]]:
    """Count blocker-class exposure findings the model did NOT already place in
    the Blockers section — these are floored to blocker for the gate.

    Returns (count, sorted-unique-categories). Findings whose `[sec:<cat>]`
    tag sits INSIDE the Blockers section are already counted by
    count_blockers, so they're excluded here (no double-count). Inert on
    bodies with no `[sec:...]` tags → (0, []), so the gate is byte-identical
    to pre-floor behavior until the security lens starts emitting tags.
    """
    sec = _BLOCKERS_SECTION_RE.search(review_body)
    bstart, bend = (sec.start(), sec.end()) if sec else (-1, -1)
    # Exclude a tag from the floor only when it is COVERED BY A NUMBERED (**N.) entry
    # — that's what count_blockers actually counts. The old whole-section byte-range
    # exclusion assumed "inside ### Blockers ⟹ already counted", but count_blockers
    # needs a `**N.` line: a [sec:] tag inside the section as a BULLET or prose (before
    # the first **N.) was counted by NEITHER detector → a confirmed exposure formatted
    # that way silently APPROVED (verified gate-bypass). The numbered-entry region runs
    # from the first **N. to the section end; a tag before it (or with no **N. at all)
    # is floored.
    excl_start = -1
    if sec is not None:
        first_entry = _BLOCKER_ENTRY_RE.search(review_body, bstart, bend)
        if first_entry is not None:
            excl_start = first_entry.start()
    cats: list[str] = []
    for m in _SEC_TAG_RE.finditer(review_body):
        cat = m.group(1).lower()
        if cat not in _BLOCKER_CATEGORIES:
            continue
        covered_by_numbered_blocker = (excl_start != -1 and excl_start <= m.start() < bend)
        if not covered_by_numbered_blocker:
            cats.append(cat)
    return len(cats), sorted(set(cats))


# In re-review mode, the "Previous Findings Status" section lists each
# prior finding as:
#   - **#N** [severity] — STATUS — rationale
# where severity ∈ {blocker, medium, low, nit} (carried from the prior
# review) and STATUS ∈ {FIXED, NOT FIXED, PARTIALLY FIXED, DEFERRED,
# DISPUTED}. The severity tag is optional for backward compatibility
# with reviews emitted before v1.12 — when missing, the gate counter
# and the carry-forward parser both default to `blocker` (conservative-
# gating).
#
# Verdict gating semantics:
# - FIXED / DISPUTED / DEFERRED on non-blocker: never gate.
# - NOT FIXED / PARTIALLY FIXED: gate ONLY if severity == `blocker`.
#   Medium/low/nit prior findings left unfixed surface as recommendations
#   in the comment body but no longer block approval.
# - DEFERRED on blocker: gates (defense in depth — verifier prompt
#   forbids it but the gate enforces independently).
# - New blockers under `#### Blockers`: always gate (existing behavior).
#
# Why blocker-only: repo-D #37 spent 13 consecutive re-review
# rounds in CHANGES_REQUESTED state because one medium-severity test-
# coverage recommendation was repeatedly NOT FIXED. The developer had
# fixed every blocker and was intentionally deferring tests to a
# follow-up PR, but the medium-severity gate kept the PR red. Mediums
# are now warnings in the body — humans can still request changes
# manually if they disagree with a developer's deferral.
#
# Capture groups (1-indexed): 1=finding-number, 2=severity (or None),
# 3=status. Both `_count_gating_unfixed` and `extract_prior_statuses`
# read this regex — keep one pattern, both call sites in lockstep.
# Shared status/severity alternation fragments — the FROZEN gate enum. BOTH
# _PRIOR_STATUS_RE (the parse/gate contract) and _PRIOR_STATUS_LINE_RE (the
# rewrite sibling, defined far below) build their groups from these, so a status
# can never be added to one regex but silently missed by the other — the drift
# footgun that would let a new status escape rewrite+`seen` and get a finding
# spuriously resurrected. The literal status alternation lives here (Check E and
# a cross-regex test both pin it). Editing the enum is editing the gate contract.
_STATUS_ALT = r"FIXED|NOT\s+FIXED|PARTIALLY\s+FIXED|DEFERRED|DISPUTED"
_SEVERITY_ALT = r"blocker|medium|low|nit"

_PRIOR_STATUS_RE = re.compile(
    r"^-\s+\*\*#(\d+)\*\*"
    rf"(?:\s*\[({_SEVERITY_ALT})\])?"
    r"\s+—\s+[^\w\n]*"  # tolerate ANY leading decoration before the status
                        # token — a `**`-bolded `— **FIXED**` OR a leading
                        # emoji/✅ the verifier emits — else the line reads as
    rf"({_STATUS_ALT})\b",  # absent and a real FIXED gets falsely resurrected
    re.MULTILINE | re.IGNORECASE,  # (repo-D #124: `— ✅ FIXED` block).
)


_GATING_SEVERITIES = {"blocker"}


_GATING_STATUSES = {"NOT FIXED", "PARTIALLY FIXED"}


# Carry-forward suppression promotes a NOT FIXED finding to DEFERRED
# once it's been NOT FIXED for at least this many consecutive rounds
# (counting the current round). Set to 2: prior round + current round =
# 2 consecutive misses → auto-defer. Update the verifier_task emit text
# below (`{CARRY_FORWARD_THRESHOLD}+ consecutive rounds...`) if widening
# this — the rule and the user-visible rationale must move together.
CARRY_FORWARD_THRESHOLD = 2


# DEFERRED is non-gating for non-blocker findings, but the verifier prompt
# forbids DEFERRED for blockers ("ONLY acceptable for non-blocker findings;
# do NOT use this status for findings originally classified as `blocker`").
# Defense in depth: the gate enforces the same rule independently — if the
# verifier (or a future prompt drift) emits `[blocker] — DEFERRED`, treat
# it as gating regardless. Prevents prompt-only enforcement of a rule that
# can flip a CHANGES_REQUESTED to APPROVE on a deferred blocker.
_BLOCKER_DEFERRED_STATUS = "DEFERRED"


_REREVIEW_HEADER_RE = re.compile(r"^##\s+Code Review\s*\(Re-review\)", re.MULTILINE)
_PRIOR_STATUS_SECTION_RE = re.compile(r"^###\s+Previous Findings Status\s*$", re.MULTILINE)


def _is_rereview_body(body: str) -> bool:
    """True when the gate must take the RE-REVIEW branch: the body carries the
    `## Code Review (Re-review)` header OR a `### Previous Findings Status`
    section.

    The section is the load-bearing second signal (M7): a verifier that emits
    prior-status lines but forgets the `(Re-review)` header suffix used to fall
    to the FRESH branch, which never counts `- **#N** [blocker] — NOT FIXED`
    lines — silently un-gating an unfixed prior blocker. The header repair in
    pin_and_resurrect (_ensure_rereview_shape) exists, but it runs only when
    the ledger is enabled AND non-empty, so AIR_LEDGER_PIN=0 (or an
    unparseable prior body → empty ledger) disabled a repair the gate itself
    depended on. Detecting the section here fixes branch selection for ALL
    paths (managed, headless, CLI --decide) at the single gating source.

    Monotone by construction: the re-review branch = the fresh gate
    (count_blockers + floor) PLUS unfixed-prior counting — widening detection
    can only ADD gate reasons, never remove one."""
    return bool(_REREVIEW_HEADER_RE.search(body) or _PRIOR_STATUS_SECTION_RE.search(body))


# Cap the prior review body before inlining into specialist prompts. A noisy
# 10K-token review would blow up re-review context ~5x across agents and
# defeat the inter-diff savings.
PRIOR_REVIEW_MAX_CHARS = 8000


def find_prior_review(comments: list[dict], bot_login: str) -> dict | None:
    """Return the most recent bot-authored ## Code Review comment, or None.

    Filters on comment author so a PR participant can't hijack the
    auto-detect flow by posting a fake review body. Takes an already-
    fetched comment list to avoid re-paginating the endpoint.

    Returns the NEWEST match by `created_at` — NOT the first in list order.
    The GitHub *issue-comments* endpoint IGNORES `sort`/`direction` and always
    returns ascending (oldest-first), so a "return first match" walk yielded the
    ORIGINAL review on every re-review — the baseline never advanced past round 1
    (a multi-round re-review then re-diffs the whole fix set against the original
    forever; a fixed PR can false-block). Select by max created_at so the result
    is correct regardless of the endpoint's delivery order. (`created_at` is
    ISO-8601 UTC — lexicographically sortable; ties are vanishingly rare and
    either review is a valid baseline.)
    """
    matches = [c for c in comments
               if (c.get("user") or {}).get("login") == bot_login
               and (c.get("body") or "").startswith(BOT_REVIEW_PREFIXES)]
    # Sort by (created_at, id): id breaks same-second ties deterministically.
    # (A comment missing created_at still sorts to the minimum — the `or ""`
    # first element dominates the tuple compare, the id is only consulted on a
    # created_at tie. Not load-bearing: created_at is always present on real
    # GitHub responses; this is just a deterministic tiebreak.)
    return max(matches, key=lambda c: (c.get("created_at") or "", c.get("id") or 0)) \
        if matches else None


def extract_reviewed_at_sha(body: str) -> str | None:
    # Lower-case the captured SHA: REVIEWED_AT_RE is IGNORECASE (so an
    # uppercase model-emitted footer still extracts), but the skip gate and
    # TOCTOU re-check compare `== head_sha` case-sensitively against GitHub's
    # always-lowercase SHA. Returning the raw (possibly uppercase) match made
    # an uppercase footer post fine, then MISS the next run's skip gate → a
    # duplicate full review on an unchanged SHA. Normalize here.
    match = REVIEWED_AT_RE.search(body or "")
    return match.group(1).lower() if match else None


# Anti-spoof: compare the `Reviewed at:` footer SHA on a 12-hex-char prefix
# (48 bits — unguessable for spoofing) rather than full 40-char equality, which
# proved too strict (models occasionally corrupt the SHA tail; repo-D
# #84). Named here, alongside the function that enforces it.
_SHA_PREFIX_LEN = 12


def _extract_review_body(raw_text: str, head_sha: str,
                         prefer_first_header: bool = False) -> tuple[str, bool]:
    """Extract the SHA-validated `## Code Review` body from a session output.

    Returns (review_body, extracted). The runtime interleaves sub-agent
    forwards (`<agent-notification thread_id="...">...</agent-notification>`)
    with the agent's own voice. Strategy: ignore segmentation — anchor on the
    `Reviewed at: <head_sha>` footer the review ALWAYS emits, walk back to the
    most recent `## Code Review` line, and validate the captured SHA matches
    the head_sha we reviewed. The SHA validation closes the verdict-flip
    prompt-injection surface (PR #47 v1 audit): an attacker echoing PR diff
    content can fake the `## Code Review` header + `### Blockers` template, but
    can't predict head_sha (the commit's own SHA, not in the diff). Candidates
    whose footer SHA doesn't match are rejected.

    Tag-stripping flattens `<agent-notification ...>` open + close tags into
    newlines so the header anchor works at byte 0, after a wrapper close, or
    after a `\\n`. Backtick-prefixed mid-narration mentions are rejected by the
    negative lookbehind.
    """
    _flattened = re.sub(r"</?agent-notification\b[^>]*>", "\n", raw_text)
    # Walk every `## Code Review[^\n]*` occurrence NOT preceded by a backtick
    # (inline-code narration). The header need NOT be at start-of-line
    # (repo-A #635 had narration concatenated on the same line), but
    # LINE-START candidates outrank mid-line ones: a review body that QUOTES
    # the header string mid-sentence (air PR #143's own review quoted the
    # jq filter `startswith("## Code Review\n")` inside a finding — preceded
    # by a quote char, so the backtick lookbehind missed it) must not beat
    # the real line-start header, or the posted comment starts mid-finding
    # and loses everything before the quote (observed live, 2026-06-09).
    # Within a rank, latest-first (a regenerated review supersedes its echo).
    #
    # A candidate's bound is the next LINE-START `## Code Review` header (or
    # EOF) — the true candidate boundary — NOT the next generic `## ` line
    # and NOT mid-line quoted occurrences. A `## ` line inside the body (a
    # fenced markdown example quoting a heading) is content; bounding on it
    # cut the candidate before its footer and converted a fully-billed
    # review into a run-failed comment (2026-06-09 audit). Markdown-fence
    # parsing was evaluated and rejected for this: transcript-wide fence
    # parity is hostile territory (unterminated fences in narration,
    # four-backtick nesting, quoted diffs toggling parity) — adversarial
    # review reproduced extraction drops on all three. The header bound +
    # SHA validation need no fence model: extraction always ENDS at the
    # matching footer, so an over-wide bound can at worst include
    # same-message content, and a too-early header can't capture a later
    # candidate's footer (the bound stops at that candidate's own header).
    _header_re = re.compile(r"(?<!`)## Code Review[^\n]*\n")
    _line_start_header_re = re.compile(r"(?:^|\n)## Code Review[^\n]*\n")
    # NOTE: do NOT add `\b` between the 40-char hex and `[^\n]*`. Word-boundary
    # fails when the SHA is followed by another word char (repo-A #666 round 7:
    # `...936Wiki push failed...` had no boundary between `6` and `W`). The
    # 40-char exact quantifier is the anchor; the 12-char prefix compare below
    # is the validator. `[^\n]*` eats the rest of the line so match end is defined.
    # Case-insensitive + `\s*` to match REVIEWED_AT_RE (the skip-gate parser):
    # the two used to disagree on case (`Reviewed At:` passed the gate but
    # failed extraction) and on the quantifier (`Reviewed at:<sha>` ditto).
    _footer_re = re.compile(r"\nReviewed at:\s*([0-9a-fA-F]{40})[^\n]*", re.IGNORECASE)
    _expected_prefix = head_sha.lower()[:_SHA_PREFIX_LEN]

    def _line_start(idx: int) -> bool:
        return idx == 0 or _flattened[idx - 1] == "\n"

    if prefer_first_header:
        # Headless emits exactly ONE review (no coordinator regenerate-and-echo),
        # so the FIRST line-start `## Code Review` is the real header and any LATER
        # line-start one is quoted CONTENT — e.g. a review OF a PR that edits the
        # review format (#240), whose findings quote the skeleton's own
        # `## Code Review` / `Reviewed at:` lines. Take the first line-start header
        # through the LAST head_sha-matching footer; quoted markers in between are
        # body content (extraction always ends at the matching footer, so an
        # over-wide span at worst includes same-review content). The default
        # (bounded, latest-wins) path FRAGMENTS here: the real header's candidate
        # is bounded by the quoted header and loses its footer, so a skeleton-
        # quoting review self-un-extracts. Falls through to the default path if the
        # first line-start header has no matching footer (safety). The raw-body
        # anti-decoy gate in headless still runs, so a wider span can't hide a
        # blocker. Managed/CLI keep the default (prefer_first_header=False) — their
        # coordinator CAN legitimately emit the review twice (echo), where the
        # LATER copy is the real regeneration.
        for _hm in _header_re.finditer(_flattened):
            if not _line_start(_hm.start()):
                continue
            _last = None
            for _m in _footer_re.finditer(_flattened, _hm.end()):
                if _m.group(1).lower()[:_SHA_PREFIX_LEN] == _expected_prefix:
                    _last = _m
            if _last is not None:
                return _flattened[_hm.start():_last.end()].rstrip(), True
            break  # first line-start header lacks a matching footer → default path

    _candidates = []
    for _hm in _header_re.finditer(_flattened):
        _body_start = _hm.end()
        _next_header = _line_start_header_re.search(_flattened, _body_start)
        _bound = _next_header.start() if _next_header else len(_flattened)
        # Prefer the first footer whose SHA prefix-matches head_sha — a body
        # may QUOTE a prior round's footer (any case) before its own; taking
        # the first match blindly would capture the stale SHA and discard the
        # candidate. Fall back to the first footer so the mismatch-warning
        # path below still fires for genuinely-spoofed candidates.
        _fm = None
        for _m in _footer_re.finditer(_flattened, _body_start, _bound):
            if _m.group(1).lower()[:_SHA_PREFIX_LEN] == _expected_prefix:
                _fm = _m
                break
            if _fm is None:
                _fm = _m
        if _fm is None:
            continue
        _candidates.append(
            (_line_start(_hm.start()), _hm.start(), _fm.end(), _fm.group(1))
        )
    # Rank: line-start candidates first, then mid-line; latest-first within
    # each rank (sort is stable; reverse positional order inside rank).
    _candidates.sort(key=lambda c: (not c[0], -c[1]))
    # Anti-spoof validator (see _SHA_PREFIX_LEN): a poisoned diff can echo
    # `## Code Review` but can't predict the run's head SHA. Prefix equality
    # keeps the security property while tolerating model tail-corruption.
    head_sha = head_sha.lower()
    for _is_ls, _start, _end, _sha in _candidates:
        _sha = _sha.lower()
        if _sha[:_SHA_PREFIX_LEN] != head_sha[:_SHA_PREFIX_LEN]:
            print(
                f"  [warn] discarding `## Code Review` block at offset "
                f"{_start} — `Reviewed at:` SHA {_sha} doesn't match "
                f"head_sha {head_sha} (first {_SHA_PREFIX_LEN} chars "
                f"compared)",
                file=sys.stderr,
            )
            continue
        if _sha != head_sha:
            print(
                f"  [info] footer SHA tail-corrupted by the model "
                f"({_sha} vs {head_sha}) — accepted on "
                f"{_SHA_PREFIX_LEN}-char prefix match",
                file=sys.stderr,
            )
        return _flattened[_start:_end].rstrip(), True
    return "", False


# ---------------------------------------------------------------------------
# PR 7 — Re-review severity-pinning + narrow deferred-findings ledger.
#
# On re-review the verifier re-judges every prior finding from scratch, so a
# prior finding whose code DID NOT change can drift to a different severity
# (silently un-gating a real blocker) or vanish entirely. These deterministic
# guards make severity carry-forward and finding-persistence a HARD guarantee
# — the same move `has_conflict_markers` made (an advisory prompt rule becomes
# an enforced gate rule).
#
# Spine = finding-NUMBER identity. A prior finding's severity + existence is an
# attribute of its #N, carried forward verbatim. Line evidence is used ONLY
# where it's provably safe (see build_carry_forward_ledger for the full rule):
#
#   - ROUND 3+ (prior is a re-review): NEVER use line evidence. Carried `#N`
#     lines have no anchor, and the body's only `**N.**` anchors belong to NEW
#     findings whose numbers RESTART at 1 and collide with carried #N — any join
#     cross-wires. Pure number-identity (INDETERMINATE → PIN).
#   - ROUND 2 (prior is a FRESH review): line evidence IS used. A fresh body's
#     `**N.**` findings are its only findings (no collision), and their anchors
#     sit at round-1's SHA (== the inter-diff OLD side), so `finding_changed`
#     exactly distinguishes a real fix (CHANGED → honor FIXED) from a fake one
#     (UNCHANGED → rewrite to NOT FIXED). This is what keeps the common case from
#     over-gating every genuine fix.
#
# Net: the guards can make the gate STRICTER on a finding that did NOT change,
# and HONOR a finding that demonstrably DID — but they can never un-gate (a fix
# is honored only with positive `-`-line proof on round-1's own anchor).
# `parse_changed_lines` / `finding_changed` / `ChangedIndex` power the round-2
# path; cross-round line tracking for round 3+ remains a v2 (needs a stable
# per-finding anchor that survives the per-round renumbering).
# ---------------------------------------------------------------------------

# Mirror of managed/github_client.py diff-hygiene markers. verdict.py is the
# shared lib (managed imports IT, never the reverse), so the marker strings
# are duplicated here with this note rather than imported. A test pins the
# stub shape; if the markers change in github_client, update these too.
_DIFF_TRUNCATION_MARKER = "[air: diff truncated"
_DIFF_STUB_RE = re.compile(
    r"^\[air: .* changed lines omitted \(generated/vendored\)\]", re.MULTILINE
)

CHANGED, UNCHANGED, INDETERMINATE = "CHANGED", "UNCHANGED", "INDETERMINATE"

# Severity ordering for the pin. Unknown/missing ranks as blocker (3) —
# conservative, matching the missing-tag default elsewhere in this module.
_SEVERITY_RANK = {"nit": 0, "low": 1, "medium": 2, "blocker": 3}


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 3) >= _SEVERITY_RANK.get(b, 3) else b


class ChangedIndex:
    """What the inter-diff changed, parsed once.

    `changed_old` holds OLD-side line numbers of PRECISE `-` lines (removals/
    modifications). `hunk_old` holds every OLD-side line a hunk SPANS (the
    `@@ -start,count` window, context included). `finding_changed` keys on
    `hunk_old`, not `changed_old`: a real fix is frequently ADDITIVE (insert a
    guard above the flagged line, refactor the enclosing function), so the
    finding's own anchor line is often a context line inside the edited hunk
    rather than a `-` line itself — `changed_old` alone misses those and
    over-gates genuine fixes (measured ~50% false REQUEST_CHANGES on real
    round-2 re-reviews). `hunk_old` = "the dev edited this finding's region",
    which is the right honor-a-fix signal. `changed_old` is kept for tests and
    a possible finer-grained v2."""

    __slots__ = ("present", "changed_old", "hunk_old", "renames",
                 "touched_by_rename", "stubbed", "truncated")

    def __init__(self):
        self.present: set = set()
        self.changed_old: dict = {}
        self.hunk_old: dict = {}
        self.renames: dict = {}
        self.touched_by_rename: set = set()
        self.stubbed: set = set()
        self.truncated = False


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_RENAME_HEADER_RE = re.compile(r"^rename (?:from|to) ", re.MULTILINE)


def parse_changed_lines(diff: str) -> ChangedIndex:
    """Parse a unified inter-diff into a ChangedIndex (old+new side aware,
    rename / hygiene-stub / truncation aware). Pure stdlib; same segment-split
    primitive as github_client.apply_diff_hygiene."""
    idx = ChangedIndex()
    if not diff:
        return idx
    if any(ln.startswith(_DIFF_TRUNCATION_MARKER) for ln in diff.splitlines()):
        idx.truncated = True
    for seg in re.split(r"(?m)^(?=diff --git )", diff):
        if not seg.startswith("diff --git "):
            continue
        header = seg.splitlines()[0]
        # "diff --git a/<old> b/<new>" — rsplit on " b/" mirrors
        # github_client._segment_path; old side from the leading "a/".
        new_path = header.rsplit(" b/", 1)[-1] if " b/" in header else ""
        _om = re.match(r"^diff --git a/(.*) b/", header)
        old_path = _om.group(1) if _om else new_path
        for p in (old_path, new_path):
            if p and p != "/dev/null":
                idx.present.add(p)
        if (_RENAME_HEADER_RE.search(seg) or old_path != new_path) and old_path and new_path:
            idx.renames[old_path] = new_path
            idx.touched_by_rename.add(old_path)
            idx.touched_by_rename.add(new_path)
        if _DIFF_STUB_RE.search(seg):
            idx.stubbed.add(old_path)
            idx.stubbed.add(new_path)
            continue
        changed = idx.changed_old.setdefault(old_path, set())
        spanned = idx.hunk_old.setdefault(old_path, set())
        old_ptr = 0
        in_hunk = False
        for ln in seg.splitlines()[1:]:
            hm = _HUNK_RE.match(ln)
            if hm:
                old_ptr = int(hm.group(1))
                # The hunk's declared OLD-side window (context included): a fix
                # that edits anywhere in the region containing the finding marks
                # it CHANGED, so additive/refactor fixes are honored, not gated.
                _count = int(hm.group(2)) if hm.group(2) else 1
                spanned.update(range(old_ptr, old_ptr + max(_count, 1)))
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if ln.startswith("-"):
                changed.add(old_ptr)
                old_ptr += 1
            elif ln.startswith("+") or ln.startswith("\\"):
                pass  # new-side insertion / "\ No newline" — old side unchanged
            else:
                old_ptr += 1  # context advances the old-side counter
    return idx


def finding_changed(loc, index: ChangedIndex) -> str:
    """Tristate: did this finding's code change enough to authorize re-rating
    / retirement? Default UNCHANGED (pin-preserving). loc is (file, start, end)
    in OLD-side (prior-reviewed-SHA) coordinates, or None."""
    if not loc:
        return INDETERMINATE
    file, start, end = loc
    if file in index.touched_by_rename:
        return CHANGED                  # a moved file always re-opens the question
    if file in index.stubbed:
        return INDETERMINATE            # generated/vendored stub hides real lines
    if file not in index.present:
        # File untouched by the inter-diff, OR cap-omitted (absent segment) —
        # both PIN. Suppression still needs CHANGED, so this can't hide a
        # finding either way (closes the cap-padding bypass without needing to
        # parse the truncation marker's unreliable path list).
        return UNCHANGED
    # CHANGED iff the finding's span falls inside ANY edited hunk's old-side
    # window — "the dev edited this finding's region" (honors additive fixes).
    spanned = index.hunk_old.get(file, set())
    return CHANGED if any(n in spanned for n in range(start, end + 1)) else UNCHANGED


# A FRESH review numbers its findings sequentially across severity sections as
# `**N. title**`, and round-1's numbers are carried forward STABLY into later
# rounds' `### Previous Findings Status` block (verified on real chains). The
# `\s` after the dot keeps this from matching a carried `- **#N** [sev]` status
# line (which starts with `- `, never `**N. `).
_FRESH_FINDING_RE = re.compile(r"^\*\*(\d+)\.\s", re.MULTILINE)
_SECTION_HEADER_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


def _section_severity(name: str):
    """Map a `### <Section>` header to a gating severity, or None for sections
    whose `**N.**` entries don't carry one — Security Audit / Pre-existing /
    Strengths / Related PRs are skipped by extract_fresh_findings (non-gating)."""
    n = name.strip().lower()
    for prefix, sev in (("blocker", "blocker"), ("medium", "medium"),
                        ("low", "low"), ("nit", "nit")):
        if n.startswith(prefix):
            return sev
    return None


def extract_fresh_findings(body: str) -> list:
    """Enumerate a FRESH review body's findings as `(num, severity, "NOT FIXED")`,
    severity taken from the enclosing `### <severity>` section.

    This is what closes the round-1 → round-2 gap: a fresh prior body has NO
    `### Previous Findings Status` block, so `extract_prior_statuses` returns []
    and — before this — the FIRST (and most common) re-review built an empty
    ledger and skipped the pin entirely. Numbers are sequential across sections
    and match the next round's carried `#N`. Pre-existing / Strengths /
    Security-Audit / Related-PRs sections carry no gating severity and are
    skipped. Only ever called on a fresh body (no status block), so it never
    sees the carried-vs-new number collision that afflicts a re-review body."""
    if not body:
        return []
    headers = [
        (m.start(), _section_severity(m.group(1)))
        for m in _SECTION_HEADER_RE.finditer(body)
    ]
    out = []
    for fm in _FRESH_FINDING_RE.finditer(body):
        pos = fm.start()
        sev = None
        for hpos, hsev in headers:
            if hpos >= pos:
                break
            sev = hsev
        if sev is None:
            continue
        out.append((int(fm.group(1)), sev, "NOT FIXED"))
    return out


_BLOB_ANCHOR_RE = re.compile(r"/blob/([0-9a-fA-F]+)/(\S+?)#L(\d+)(?:-L(\d+))?")


def extract_fresh_finding_locations(body: str, base_sha: str) -> dict:
    """`{num: (file, start, end)}` from a FRESH body's `**N.**` finding anchors,
    SHA-prefix-validated against `base_sha`.

    SAFE on a fresh body (the round-2 path ONLY): a fresh review has no
    `### Previous Findings Status` block, so its `**N.**` entries are the only
    findings — there is no carried-`#N`-vs-new-`**N.**` number collision that
    makes line evidence unsafe on a re-review body. Used to honor a REAL fix
    (a round-1 finding whose code actually moved in the round1→round2 inter-diff)
    instead of over-gating it. The fresh findings' anchors sit at round-1's SHA,
    which IS `base_sha` and the inter-diff's OLD side (round-1 is an ancestor of
    round-2 on the same branch, so the three-dot compare's OLD side == round-1),
    so `finding_changed`'s old-side line test is exact."""
    out: dict = {}
    if not body or not base_sha:
        return out
    prefix = base_sha.lower()[:_SHA_PREFIX_LEN]
    nums = list(_FRESH_FINDING_RE.finditer(body))
    for i, nm in enumerate(nums):
        num = int(nm.group(1))
        seg_end = nums[i + 1].start() if i + 1 < len(nums) else len(body)
        for am in _BLOB_ANCHOR_RE.finditer(body, nm.end(), seg_end):
            if am.group(1).lower()[:_SHA_PREFIX_LEN] != prefix:
                continue
            s = int(am.group(3))
            e = int(am.group(4)) if am.group(4) else s
            out[num] = (am.group(2), s, e)
            break
    return out


# A backtick-wrapped token that looks like a repo file path (`.ext` suffix). We
# require a `/` at match time (a bare `File.php` basename is too loose to match a
# touched path safely). Captures prose file references beyond the blob-link anchor.
_FILE_PATH_TOKEN_RE = re.compile(r"`([A-Za-z0-9_][\w./-]*\.[A-Za-z0-9]+)`")


def extract_finding_files(body: str, base_sha: str) -> dict:
    """`{num: set(referenced repo file paths)}` for a FRESH body's findings — the
    union of each finding's blob-link anchor paths (SHA-validated) and the
    backtick-wrapped path-shaped tokens in its prose.

    Widens the round-2 cross-region exemption: a blocker can be legitimately
    FIXED by editing a file the finding REFERENCES that is NOT its primary
    blob-link anchor — e.g. a finding anchored at the symptom/read site
    (`config/services.php`) whose fix lands in the wiring file
    (`scripts/after_install.sh`). Keying `file_touched` on this SET (not just the
    single anchor) stops `pin_and_resurrect` from rewriting a genuine cross-FILE
    FIXED to NOT FIXED (the repo-A #1422 false-block). Only ever called on a
    FRESH body (round-2 path), same as extract_fresh_finding_locations."""
    out: dict = {}
    if not body:
        return out
    prefix = (base_sha or "").lower()[:_SHA_PREFIX_LEN]
    nums = list(_FRESH_FINDING_RE.finditer(body))
    for i, nm in enumerate(nums):
        num = int(nm.group(1))
        seg_end = nums[i + 1].start() if i + 1 < len(nums) else len(body)
        files: set = set()
        for am in _BLOB_ANCHOR_RE.finditer(body, nm.end(), seg_end):
            # SHA-validate the blob anchor, same as extract_fresh_finding_locations
            # (no base_sha → recover no blob path; prose tokens below need no SHA).
            if prefix and am.group(1).lower()[:_SHA_PREFIX_LEN] == prefix:
                files.add(am.group(2))
        for tm in _FILE_PATH_TOKEN_RE.finditer(body, nm.end(), seg_end):
            tok = tm.group(1)
            if "/" in tok:                      # require a path, not a bare basename
                files.add(tok)
        out[num] = files
    return out


def _referenced_file_touched(files: set, index: ChangedIndex) -> bool:
    """True iff ANY referenced repo path has a real content hunk in the inter-diff,
    by EXACT path match. Both sides are repo-root-relative full paths — a blob-link
    anchor / backtick'd prose path vs a `diff --git` path — so exact match is
    correct and avoids a loose suffix crediting a same-basename file in a different
    directory (#244 review). Keys on non-empty `hunk_old` (a real `@@` hunk), NOT
    mere `present`, so a metadata-only segment can't qualify. Over-crediting is the
    SAFE direction (it only honors a verifier's already-source-grounded FIXED, never
    un-gates a dropped/downgraded finding — those guards are independent)."""
    if not files:
        return False
    touched = {f for f, spanned in index.hunk_old.items() if spanned}
    return bool(files & touched)


class LedgerEntry:
    __slots__ = ("num", "prior_severity", "prior_status", "location", "change",
                 "file_touched", "origin_sha")

    def __init__(self, num, prior_severity, prior_status, location, change,
                 file_touched=False, origin_sha=None):
        self.num = num
        self.prior_severity = prior_severity
        self.prior_status = prior_status
        self.location = location
        self.change = change
        # Did the finding's FILE appear anywhere in the inter-diff? `change`
        # (line-level) reads UNCHANGED both when the file is absent AND when it
        # changed but not at the flagged anchor (a cross-region fix). file_touched
        # distinguishes them so the FIXED→NOT FIXED rewrite fires ONLY on a
        # provably-untouched file (see pin_and_resurrect). False for the
        # number-identity rounds (no inter-diff index — INDETERMINATE, which the
        # pin trusts anyway).
        self.file_touched = file_touched
        # Origin-anchor (#198): the SHA at which this finding was FIRST raised. Set
        # only on round-3+ carried findings whose origin was recovered AND the
        # origin..head window resolved (else None → v1 number-identity). For
        # [pin][origin] telemetry only — the gate decision flows through
        # change/file_touched (computed against the origin window) exactly as round-2.
        self.origin_sha = origin_sha


def find_origin(chain, finding_num: int):
    """Origin-anchor (#198): walk the prior-review CHAIN oldest-first and return
    `(origin_sha, location, referenced_files)` for the OLDEST round that raised `#N`
    as a finding with a recoverable `**N.**` anchor (`extract_fresh_finding_locations`).
    The carry-forward contract preserves a finding's NUMBER across rounds, so a
    round-3+ carried `#N` traces to the same `#N` where it was first a NEW finding.
    Returns `(None, None, set())` when no anchor is recoverable → caller falls back
    to number-identity (conservative). `referenced_files` = every file that origin
    finding is about (its anchor + backtick'd prose paths, `extract_finding_files`),
    so round-3+ honors a cross-FILE fix the same way round-2 does (repo-A #1422/#244).

    `chain` is a list of `(review_body, reviewed_sha)` OLDEST-FIRST. Pure — the
    caller (which holds the comments + git/API) builds the chain, runs the
    ancestor check, and resolves the `origin..head` diff; this only does the anchor
    archaeology. Mis-attribution under per-round renumbering is bounded safe: the
    origin only ever widens `file_touched` (False->True) feeding pin_and_resurrect's
    already-validated cross_region trust — it cannot un-gate beyond that class."""
    for body, sha in chain:
        if not sha:
            continue
        locs = extract_fresh_finding_locations(body, sha)
        if finding_num in locs:
            files = extract_finding_files(body, sha).get(finding_num, set())
            return sha, locs[finding_num], files
    return None, None, set()


def make_file_origin_resolver(chain, diffs_dir):
    """PURE (no network) origin_resolver for the CLI path (#198) — the managed
    analogue is `review.make_origin_resolver`, but the CLI orchestrator
    (review.md Step 11.5) has ALREADY run the ancestor gate locally
    (`git merge-base --is-ancestor`) and written ONLY confirmed `origin..head`
    diffs into `diffs_dir` as `<sha[:12]>.diff`. So the gate-safety invariant is
    identical — a diff file present ⟺ the origin is a confirmed ancestor of head ⇒
    `origin..head` is a clean superset of `baseline..head` ⇒ `file_touched` only
    widens — without verdict.py ever touching the network (the stdlib-only
    contract). A missing/unreadable diff → None for that finding → v1
    number-identity fallback (conservative, never un-gates).

    `chain` is `[(body, reviewed_sha)]` OLDEST-FIRST (the orchestrator builds it
    from the prior bot-review comments, same anti-spoof author filter as the
    baseline selection). Returns a `resolver(num) -> (origin_sha, location,
    ChangedIndex, referenced_files) | None` matching `build_carry_forward_ledger`'s
    contract."""
    cache: dict = {}

    def resolver(num):
        sha, loc, files = find_origin(chain, num)
        if not (sha and loc):
            return None
        key = sha[:_SHA_PREFIX_LEN]
        if key not in cache:
            try:
                text = (Path(diffs_dir) / f"{key}.diff").read_text()
                cache[key] = parse_changed_lines(text)
            except (OSError, UnicodeDecodeError):
                cache[key] = None        # diff absent/unreadable ⇒ origin not ancestor-confirmed
        idx = cache[key]
        return (sha, loc, idx, files) if idx is not None else None

    return resolver


def build_carry_forward_ledger(prior_body: str, inter_diff: str, base_sha: str,
                               *, sibling: bool = False, origin_resolver=None) -> list:
    """One LedgerEntry per prior finding, keyed on #N. The change-state depends
    on which round we're carrying from — and that distinction is load-bearing:

    ROUND 3+ (prior is itself a re-review, has a `### Previous Findings Status`
    block) → PURE number-identity (`change=INDETERMINATE`, no line evidence).
    Carried `#N` lines have no anchor of their own, and the only `**N.**` anchors
    in such a body belong to that round's NEW findings — whose numbers RESTART at
    1 and COLLIDE with carried `#N`. Joining the two on the bare integer would
    cross-wire a carried blocker to an unrelated finding's diff state and could
    mark it CHANGED → un-pinned (the bug the dogfood review caught). So here line
    evidence is unsafe and deliberately unused.

    ROUND 2 (prior is a FRESH review, NO status block) → line evidence IS used,
    and IS safe: a fresh review's `**N.**` findings are its only findings, so
    there's no carried-vs-new collision, and their anchors sit at round-1's SHA
    (== `base_sha` == the inter-diff's OLD side, since round-1 is an ancestor of
    round-2). So `finding_changed` precisely tells a REAL fix (the finding's code
    moved → CHANGED → honor a `FIXED`) from a fake one (UNCHANGED → rewrite to
    NOT FIXED). Without this the first (and most common) re-review over-gated
    every genuine fix — measured at ~70% false REQUEST_CHANGES on live fleet
    re-reviews before this path existed.

    `sibling=True` (promote fast-path, a different PR's tree) → number-identity,
    no fresh fallback. Empty list when there's nothing to carry → no-op."""
    triples = extract_prior_statuses(prior_body)
    if sibling:
        # Promote fast-path (a DIFFERENT PR's tree): number-identity, never
        # origin-anchored — the sibling's origin SHA isn't an ancestor of this
        # head, so the caller's ancestor gate would reject it anyway. Conservative.
        return [LedgerEntry(num, sev, status, None, INDETERMINATE)
                for num, sev, status in triples]
    if triples:
        # Round 3+: carried #N has NO anchor in this body. ORIGIN-ANCHOR (#198):
        # when origin_resolver is supplied, recover #N's first-raise anchor + test
        # it against the WIDER origin..head window, so a fix that landed BEFORE the
        # current baseline un-poisons (the repo-A #1290 class: file absent from
        # baseline..head -> file_touched=False -> FIXED rewritten to NOT FIXED
        # forever). Same change/file_touched semantics as round-2, just over the
        # origin window. No resolver / unresolved origin -> v1 number-identity
        # (INDETERMINATE, file_touched=False) -> conservative pin. GATE-SAFE: the
        # origin window is a SUPERSET of baseline (origin is an ancestor of head,
        # enforced by the caller's ancestor check), so file_touched can only flip
        # False->True, and it feeds ONLY pin_and_resurrect's already-validated
        # cross_region trust (0 fleet un-gates) — never a new trust class.
        out = []
        for num, sev, status in triples:
            loc, change, file_touched, origin_sha = None, INDETERMINATE, False, None
            if origin_resolver is not None:
                res = origin_resolver(num)   # (origin_sha, location, ChangedIndex, referenced_files) | None
                if res:
                    origin_sha, loc, oidx, ofiles = res
                    change = finding_changed(loc, oidx) if loc else INDETERMINATE
                    # Cross-FILE parity with round-2: honor a fix in ANY file the
                    # origin finding references (its anchor is in the set), not just
                    # the single anchor — so a round-3+ cross-file fix isn't rewritten
                    # to NOT FIXED while the verifier's narrative says fixed (#244).
                    file_touched = _referenced_file_touched(ofiles, oidx)
            out.append(LedgerEntry(num, sev, status, loc, change, file_touched, origin_sha))
        return out
    # Round 2: fresh prior — line evidence is safe and honors real fixes.
    fresh = extract_fresh_findings(prior_body)
    if not fresh:
        return []
    locs = extract_fresh_finding_locations(prior_body, base_sha or "")
    files_by_num = extract_finding_files(prior_body, base_sha or "")
    index = parse_changed_lines(inter_diff or "")
    ledger = []
    for num, sev, status in fresh:
        loc = locs.get(num)
        change = finding_changed(loc, index) if loc else INDETERMINATE
        # File-level signal: did the dev make a real CODE edit to a file this
        # finding is about? A genuine cross-region (or cross-FILE) fix changes a
        # hunk in one of the finding's referenced files (just not the flagged
        # line); a bogus FIXED on code the dev never opened leaves them all
        # absent. We consider EVERY file the finding references (its anchor +
        # backtick'd prose paths), not just the single blob-link anchor, so a
        # blocker flagged at the symptom/read site but fixed in the wiring file
        # (repo-A #1422) is honored, not rewritten. Keys on a real `@@` hunk (not
        # mere `present`), so a metadata-only segment can't qualify.
        file_touched = _referenced_file_touched(files_by_num.get(num, set()), index)
        ledger.append(LedgerEntry(num, sev, status, loc, change, file_touched))
    return ledger


# Same shape/enums as _PRIOR_STATUS_RE but with a trailing-tail capture so the
# rationale survives a rewrite. _PRIOR_STATUS_RE stays frozen (the gate/parse
# contract); this is the rewrite-only sibling. Both build their severity/status
# groups from the SHARED `_SEVERITY_ALT`/`_STATUS_ALT` fragments so they can
# never drift apart on the enum (PR7 review #8 — a status added to the gate
# regex but missed here would escape rewrite+`seen` and spuriously resurrect).
_PRIOR_STATUS_LINE_RE = re.compile(
    r"^-\s+\*\*#(\d+)\*\*"
    rf"(?:\s*\[({_SEVERITY_ALT})\])?"
    r"\s+—\s+[^\w\n]*"  # tolerate ANY leading decoration (see _PRIOR_STATUS_RE);
    rf"({_STATUS_ALT})\b"   # a leading `**`/✅ is consumed here, a trailing `**`
    r"(.*)$",               # falls into the tail and is stripped in _rewrite so
    re.MULTILINE | re.IGNORECASE,  # the rewritten line stays canonical.
)


# Off-enum SYNONYMS the verifier sometimes emits for an exit status (observed on
# repo-D #124: `ACCEPTED` for accept-by-design, whose canonical token is
# `DISPUTED`). The frozen status regexes only know the five-token enum, so a
# synonym WORD reads as "absent" and the finding is spuriously resurrected as
# NOT FIXED — the same footgun the `[^\w\n]*` prefix closes for emoji decoration,
# but for the status word itself. Mapped to canonical BEFORE parsing. Unknown
# words are left alone → they still resurrect (over-gate, fail-safe). The
# verifier prompt is also tightened to emit enum-only tokens; this is the
# deterministic backstop for when the model ignores it.
_STATUS_SYNONYMS = {"ACCEPTED": "DISPUTED", "WONTFIX": "DISPUTED", "RESOLVED": "FIXED"}
# IGNORECASE: match a `[BLOCKER]`-cased tag too (else the synonym misses and the
# finding resurrects). `(?P<word>...)(?=\s*(?:[^\w\s]|$))`: rewrite ONLY when the
# synonym is the COMPLETE leading status token — followed by a DELIMITER (the
# ` — ` rationale dash, but also `:`/`(`/`.`/bold `**`, matching the canonical
# parser's `\b` tolerance so a `— **ACCEPTED**` / `— RESOLVED: x` normalizes too)
# or end-of-line. The `[^\w\s]` (a non-word, non-space char) is what refuses to
# fire on a word that merely PRECEDES a real status (`— RESOLVED NOT FIXED —`,
# where a space-then-word follows) — that would corrupt the line the gate then
# re-parses. `num`/`sev` are captured for the severity-aware rule below.
_SYNONYM_STATUS_RE = re.compile(
    r"(?P<prefix>^-\s+\*\*#(?P<num>\d+)\*\*(?:\s*\[(?P<sev>" + _SEVERITY_ALT + r")\])?\s+—\s+[^\w\n]*)"
    r"(?P<word>[A-Za-z]+)(?=\s*(?:[^\w\s]|$))",
    re.MULTILINE | re.IGNORECASE,
)


def _canonicalize_status_synonyms(review_body: str, by_num: dict) -> tuple:
    """Rewrite a known off-enum status synonym on a `### Previous Findings
    Status` line to its canonical token before the frozen regexes parse it.
    Returns (body, log_lines). Only a synonym that is the COMPLETE leading
    status token is touched; the rationale and every other line are untouched.

    Severity-aware: an accept-by-design synonym (ACCEPTED/WONTFIX → DISPUTED) on
    a BLOCKER is instead rewritten to NOT FIXED. DISPUTED is a non-gating exit,
    so promoting an accept word to it on a blocker would convert the pre-fix
    fail-safe (resurrect → NOT FIXED → gate) into an UN-gate — violating the
    PR7 invariant that the gate may only ever get STRICTER on a carried finding.
    An accept-by-design call on a blocker must escalate (gate → human override,
    or an explicit DISPUTED the verifier deliberately typed), never auto-clear.
    RESOLVED→FIXED is exempt: the FIXED-on-unchanged guard in `_rewrite` already
    re-gates an unsubstantiated FIXED on a medium+ finding. Severity is read
    from the ledger (authoritative carried severity), maxed with the emitted
    tag, so a downgraded-but-pinned blocker is still treated as a blocker here."""
    log: list = []

    def _repl(m):
        canon = _STATUS_SYNONYMS.get(m.group("word").upper())
        if not canon:
            return m.group(0)
        if canon == "DISPUTED":
            # Governing severity = the ledger's carried severity (authoritative)
            # maxed with the emitted tag, so a downgraded-but-pinned blocker is
            # still treated as a blocker. With no ledger entry (only in direct
            # unit calls — the real ledger enumerates every prior finding), fall
            # back to the emitted tag, then to blocker (the gate's missing-tag
            # default).
            entry = by_num.get(int(m.group("num")))  # `\d+` ⇒ always parseable
            emitted_sev = (m.group("sev") or "").lower()
            prior_sev = entry.prior_severity if entry else (emitted_sev or "blocker")
            if _max_severity(prior_sev, emitted_sev or prior_sev) == "blocker":
                canon = "NOT FIXED"  # accept-by-design must not auto-clear a blocker
        log.append(f"[pin] normalized status synonym {m.group('word')!r} -> {canon}")
        return m.group("prefix") + canon

    return _SYNONYM_STATUS_RE.sub(_repl, review_body), log


def _section_end(body: str, from_idx: int) -> int:
    # Stop at the next markdown header OR the `Reviewed at:` footer line,
    # whichever comes first. The footer is not a header, so without it
    # resurrected entries would be appended AFTER the footer when `Previous
    # Findings Status` is the last section — gating still works (the status
    # regex is MULTILINE) but the posted comment reads as malformed, with the
    # timestamp stranded mid-section (PR7 review #6).
    m = re.search(r"^(?:#{2,4}\s|Reviewed at:)", body[from_idx:], re.MULTILINE)
    return from_idx + m.start() if m else len(body)


def _ensure_rereview_shape(body: str, log: list, resurrected: list) -> str:
    """Guarantee the `## Code Review (Re-review)` header + a `### Previous
    Findings Status` section (so should_request_changes takes the re-review
    branch and counts these), and append any resurrected lines into it."""
    if not _REREVIEW_HEADER_RE.search(body):
        new = re.sub(r"^##\s+Code Review\b[^\n]*", "## Code Review (Re-review)",
                     body, count=1, flags=re.MULTILINE)
        if new != body:
            log.append("[pin] repaired header -> ## Code Review (Re-review)")
            body = new
    if not resurrected:
        return body
    block = "\n".join(resurrected)
    sec = _PRIOR_STATUS_SECTION_RE.search(body)
    if sec:
        at = _section_end(body, sec.end())
        return body[:at].rstrip("\n") + "\n" + block + "\n\n" + body[at:]
    hdr = _REREVIEW_HEADER_RE.search(body)
    log.append("[ledger] created Previous Findings Status section for resurrected findings")
    if hdr:
        nl = body.find("\n", hdr.end())
        nl = len(body) if nl == -1 else nl + 1
        return body[:nl] + "\n### Previous Findings Status\n\n" + block + "\n" + body[nl:]
    return body.rstrip("\n") + "\n\n### Previous Findings Status\n\n" + block + "\n"


# Appended to a status line when the pin rewrites a verifier FIXED→NOT FIXED, so
# the label is self-explaining rather than contradicting its own "fixed" rationale
# (the repo-A #1422 report). Inert to the gate — the STATUS token is parsed, the
# tail is free prose.
_PIN_REWRITE_MARKER = (
    "[air: pinned NOT FIXED — the verifier judged this fixed, but the re-review "
    "inter-diff shows no code change to this finding's file(s); verify manually. "
    "Clears on a later review once the fix lands in the inter-diff, or via a "
    "DISPUTED reply.]"
)


def pin_and_resurrect(review_body: str, ledger: list) -> tuple:
    """The hard guard. Given the emitted re-review body + the ledger:
    pin each prior finding's severity to max(prior, emitted) unless its code
    CHANGED; rewrite illegitimate retirements (FIXED/DEFERRED on non-CHANGED,
    blocker-DEFERRED) to NOT FIXED at pinned severity; and resurrect any prior
    finding silently dropped from the status block. Returns (body, log_lines).
    No-op (returns body verbatim) when ledger is empty."""
    if not ledger:
        return review_body, []
    by_num = {e.num: e for e in ledger}
    # Normalize off-enum status synonyms (e.g. `— ACCEPTED` → `— DISPUTED`)
    # BEFORE the rewrite/parse pass, so a real exit isn't misread as absent and
    # resurrected. Leading emoji/symbol decoration is handled by the regex
    # prefix tolerance; this handles the WORD. Seeds the log.
    review_body, log = _canonicalize_status_synonyms(review_body, by_num)
    seen: set = set()

    def _rewrite(m):
        num = int(m.group(1))
        entry = by_num.get(num)
        if not entry:
            return m.group(0)
        seen.add(num)
        emitted_sev = (m.group(2) or "blocker").lower()
        status = re.sub(r"\s+", " ", m.group(3).upper()).strip()
        # Drop a `**` left over from a bolded status (`— **FIXED**`) — the
        # leading `**` was consumed by the regex, the trailing one lands here;
        # strip it so the rewritten line is canonical `— STATUS — rationale`.
        tail = re.sub(r"^\*{0,2}", "", m.group(4))
        if entry.change == CHANGED:
            new_sev = emitted_sev                       # re-rating authorized
        else:
            new_sev = _max_severity(entry.prior_severity, emitted_sev)
            if new_sev != emitted_sev:
                log.append(f"[pin] #{num} severity {emitted_sev}->{new_sev} "
                           f"(pinned to prior; change={entry.change})")
        new_status = status
        # FIXED → NOT FIXED rewrite (the hide-a-finding guard), gate-relevant
        # severities only (medium+; severity is already pinned to max above, so a
        # laundered blocker can't hide behind a low tag, and a genuine low/nit
        # FIXED never gates — trusting it avoids re-open noise on long chains).
        #
        # CROSS-REGION EXEMPTION: the ONLY behavior change vs the prior
        # `change != CHANGED` rule. A pure line-level test false-blocked genuine
        # CROSS-REGION fixes — the fix lands near, not at, the flagged anchor, so
        # the anchor line reads UNCHANGED even though the file changed (the
        # documented ~10% round-2 false-block; hit a live prod hotfix whose
        # verifier rationale literally said "resolved" while the status was
        # rewritten to NOT FIXED). We now trust the verifier's source-grounded
        # FIXED for EXACTLY that case — a genuine UNCHANGED-anchor-but-file-edited
        # cross-region fix — and nothing else. It is scoped narrowly on PURPOSE:
        #   • round-3+ carried findings have no anchor (INDETERMINATE, loc=None ⇒
        #     file_touched=False) → still pin-by-number to NOT FIXED;
        #   • a stubbed/cap-omitted file is INDETERMINATE (no hunk ⇒
        #     file_touched=False), and `cross_region_fix` keys on UNCHANGED not
        #     `!= CHANGED`, so it stays the conservative over-gate either way;
        #   • round-2 fakes on an untouched file (UNCHANGED, no code hunk) →
        #     rewrite and gate;
        #   • resurrection of SILENTLY-dropped findings is untouched.
        # NOTE this is NOT monotone-strict like the severity-pin above: for a
        # touched-file cross-region FIXED the gate now yields APPROVE where the
        # old over-gating rule gave CHANGES_REQUESTED — by design (same trust
        # class as a fresh-review blocker). Gate-safety is preserved by the
        # OTHER guards, not this one: severity is still pinned to max(prior,
        # emitted) so a downgrade can't un-gate, and a silently-dropped finding
        # is still resurrected.
        cross_region_fix = entry.change == UNCHANGED and entry.file_touched
        if (status == "FIXED" and entry.change != CHANGED and not cross_region_fix
                and _SEVERITY_RANK.get(new_sev, 3) >= 2):
            new_status = "NOT FIXED"
            # Annotate the rewrite so the label doesn't silently contradict the
            # verifier's (retained) "fixed" rationale + the summary header — the
            # reader sees WHY it reads NOT FIXED (repo-A #1422 confusion). The gate
            # parses only the STATUS token, so the marker in the tail is inert.
            tail = f"{tail.rstrip()} {_PIN_REWRITE_MARKER}"
            log.append(f"[pin] #{num} FIXED->NOT FIXED (no cross-region edit; change={entry.change}, file_touched={entry.file_touched})")
        elif (status == "FIXED" and cross_region_fix
                and _SEVERITY_RANK.get(new_sev, 3) >= 2):
            # Trace the trust decision — every other rewrite logs, so the
            # exemption must too (a silent honor is indistinguishable from "no
            # pin needed" in a post-incident audit).
            log.append(f"[pin] #{num} cross-region/cross-file FIXED trusted (change=UNCHANGED, file_touched=True; verifier-judged)")
        elif status == "DEFERRED":
            if new_sev == "blocker":
                new_status = "NOT FIXED"
                log.append(f"[pin] #{num} blocker DEFERRED->NOT FIXED")
            elif entry.change == CHANGED:
                new_status = "NOT FIXED"
                log.append(f"[pin] #{num} DEFERRED->NOT FIXED (code changed; re-evaluate)")
        return f"- **#{num}** [{new_sev}] — {new_status}{tail}"

    body = _PRIOR_STATUS_LINE_RE.sub(_rewrite, review_body)

    # Resurrect prior findings silently dropped from the status block — keyed
    # on PRESENCE, anchor-independent (FIXED/DISPUTED last round = legitimately
    # closed, not resurrected).
    resurrected = []
    for e in ledger:
        if e.num in seen or e.prior_status in ("FIXED", "DISPUTED"):
            continue
        resurrected.append(
            f"- **#{e.num}** [{e.prior_severity}] — NOT FIXED — "
            f"[air: re-inserted — prior finding absent from this re-review; pinned from prior round]"
        )
        log.append(f"[ledger] #{e.num} resurrected [{e.prior_severity}] NOT FIXED (silently dropped)")

    return _ensure_rereview_shape(body, log, resurrected), log


def _main(argv: list[str]) -> int:
    """CLI surface for review.md Step 12 — the SAME decision code managed runs.

    `--decide` reads the formatted review body on stdin and prints exactly
    one machine-parseable line:

        approve
        request-changes\t<reason>

    Exit codes: 0 = decided (either way), 2 = usage error. The verdict is
    NOT an exit code on purpose — bash callers `[ "$(...)" = approve ]`
    without `set -e` interference, and a crash (traceback, exit 1) stays
    distinguishable from a legitimate request-changes.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="air review-gating contract (shared by CLI and managed modes)."
    )
    parser.add_argument(
        "--decide", action="store_true",
        help="Read a review body on stdin; print 'approve' or 'request-changes\\t<reason>'.",
    )
    parser.add_argument(
        "--count-blockers", action="store_true",
        help="Read a review body on stdin; print the blocker count.",
    )
    parser.add_argument(
        "--normalize-banner", action="store_true",
        help="Read a review body on stdin; rewrite ONLY the v2 verdict banner to "
             "match the deterministic gate (alert type + 'Changes requested' lead) "
             "and print the full corrected body. Gate-safe; no-op on a flat/legacy body.",
    )
    parser.add_argument(
        "--pin", action="store_true",
        help="Read a re-review body on stdin; apply the deterministic severity-"
             "pin + ledger resurrection (needs --prior-body/--inter-diff/--base-"
             "sha) and print the corrected body on stdout, [pin]/[ledger] log on "
             "stderr. No-op (body verbatim) without the ledger inputs.",
    )
    parser.add_argument("--prior-body", help="Path to the prior review body (re-review ledger input).")
    parser.add_argument("--inter-diff", help="Path to the inter-diff (re-review ledger input).")
    parser.add_argument("--base-sha", default="", help="Prior-reviewed SHA (inter-diff base) for anchor validation.")
    parser.add_argument("--origin-chain", help="Path to a JSON array [{\"body\":..,\"sha\":..}] of prior bot reviews OLDEST-FIRST (#198 origin-anchor; CLI Step 11.5).")
    parser.add_argument("--origin-diffs", help="Directory of ancestor-confirmed origin..head diffs named <sha12>.diff (#198 origin-anchor; pairs with --origin-chain).")
    parser.add_argument("--head-sha", default="", help="Reviewed HEAD SHA; when set, --decide gates on the SHA-validated `## Code Review` block (anti-decoy), falling back to the raw body if none matches.")
    args = parser.parse_args(argv)
    if not (args.decide or args.count_blockers or args.pin or args.normalize_banner):
        parser.print_usage(sys.stderr)
        return 2
    body = sys.stdin.read()

    # Build + apply the re-review ledger when the inputs are supplied (CLI
    # Step 11.5 / parity tests). Absent inputs → byte-identical to pre-PR7.
    def _maybe_pin(b: str) -> str:
        if not (args.prior_body and args.inter_diff):
            return b
        try:
            prior = Path(args.prior_body).read_text()
            inter = Path(args.inter_diff).read_text()
        except OSError as exc:
            print(f"  [warn] ledger inputs unreadable ({exc}); skipping pin", file=sys.stderr)
            return b
        # #198 origin-anchor (CLI Step 11.5): when the orchestrator supplies the
        # prior-review chain + a dir of ancestor-confirmed origin..head diffs, build
        # the pure file-backed resolver so round-3+ carried findings un-poison
        # exactly as managed/headless do. Absent either flag → v1 number-identity.
        origin_resolver = None
        if args.origin_chain and args.origin_diffs:
            try:
                chain = [(e["body"], e["sha"])
                         for e in json.loads(Path(args.origin_chain).read_text())
                         if isinstance(e, dict) and e.get("sha") and e.get("body")]
                if chain:
                    origin_resolver = make_file_origin_resolver(chain, args.origin_diffs)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                print(f"  [pin][origin][warn] origin-chain unreadable ({exc}); "
                      f"number-identity pin", file=sys.stderr)
        ledger = build_carry_forward_ledger(prior, inter, args.base_sha,
                                            origin_resolver=origin_resolver)
        pinned, log = pin_and_resurrect(b, ledger)
        for line in log:
            print(f"  {line}", file=sys.stderr)
        return pinned

    if args.pin:
        sys.stdout.write(_maybe_pin(body))
        return 0
    if args.count_blockers:
        print(count_blockers(body))
        return 0
    if args.normalize_banner:
        # Decide on the same gate the verdict uses (floor honored; the body piped
        # here is already pinned by Step 11.5, so we do NOT re-pin), then rewrite
        # only the banner to match. head_sha anti-decoy extraction mirrors --decide.
        floor = os.environ.get("AIR_CATEGORY_FLOOR", "1").strip().lower() not in ("0", "false", "no")
        decide_on = body
        if args.head_sha:
            extracted, ok = _extract_review_body(body, args.head_sha)
            if ok:
                decide_on = extracted
        rc, _ = should_request_changes(decide_on, floor_exposures=floor)
        sys.stdout.write(normalize_verdict_banner(body, request_changes=rc))
        return 0
    # AIR_CATEGORY_FLOOR=0/false/no is the fresh-gate floor kill switch (same
    # grammar as AIR_LEDGER_PIN). The CLI's Step 12 `--decide` inherits it from
    # the environment, so managed CI and the CLI gate identically.
    floor = os.environ.get("AIR_CATEGORY_FLOOR", "1").strip().lower() not in ("0", "false", "no")
    gate_body = _maybe_pin(body)
    # Anti-decoy SHA validation (CLI Step 12 passes --head-sha): gate on the
    # SHA-anchored `## Code Review` block, not whatever was piped — a prompt-injected
    # decoy block with a wrong/absent footer SHA can't displace the real one. Falls
    # back to pre-existing behavior (same gate risk as without --head-sha — a decoy
    # blocker in the raw body still gates) when none validates, so it never introduces
    # a NEW false gate; absent --head-sha it is a pure no-op.
    if args.head_sha:
        extracted, ok = _extract_review_body(gate_body, args.head_sha)
        if ok:
            gate_body = extracted
        else:
            print(f"  [gate] no ## Code Review block validated against head_sha "
                  f"{args.head_sha[:12]} — gating on the raw body", file=sys.stderr)
    request_changes, reason = should_request_changes(gate_body, floor_exposures=floor)
    print(f"request-changes\t{reason}" if request_changes else "approve")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
