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

    `floor_exposures=False` (kill switch, wired to AIR_CATEGORY_FLOOR by
    callers) disables the floor. The floor is inert on bodies without
    `[sec:...]` tags, so a disabled / tag-less run is byte-identical to the
    pre-floor gate.
    """
    blockers = count_blockers(review_body)
    floored, floored_cats = count_category_floored(review_body) if floor_exposures else (0, [])
    floor_note = f"; +{floored} floored exposure(s) [{', '.join(floored_cats)}]" if floored else ""
    exposure_reason = f"{floored} blocker-class exposure(s) [{', '.join(floored_cats)}] floored to blocker"
    if _REREVIEW_HEADER_RE.search(review_body):
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
_BLOCKERS_SECTION_RE = re.compile(
    r"^#{3,4}\s+Blockers\s*$\n(.*?)(?=^#{1,4}\s+|\Z)",
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
    cats: list[str] = []
    for m in _SEC_TAG_RE.finditer(review_body):
        cat = m.group(1).lower()
        if cat in _BLOCKER_CATEGORIES and not (bstart <= m.start() < bend):
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


# Cap the prior review body before inlining into specialist prompts. A noisy
# 10K-token review would blow up re-review context ~5x across agents and
# defeat the inter-diff savings.
PRIOR_REVIEW_MAX_CHARS = 8000


def find_prior_review(comments: list[dict], bot_login: str) -> dict | None:
    """Return the most recent bot-authored ## Code Review comment, or None.

    Filters on comment author so a PR participant can't hijack the
    auto-detect flow by posting a fake review body. Takes an already-
    fetched comment list to avoid re-paginating the endpoint.

    Assumes `comments` arrived in desc order (newest-first), matching
    `fetch_issue_comments`'s URL params. Walks the list and returns on
    first match so we get the deterministically newest bot review
    without materializing a full filtered list.
    """
    for c in comments:
        if (c.get("user") or {}).get("login") == bot_login \
           and (c.get("body") or "").startswith(BOT_REVIEW_PREFIXES):
            return c
    return None


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


def _extract_review_body(raw_text: str, head_sha: str) -> tuple[str, bool]:
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


class LedgerEntry:
    __slots__ = ("num", "prior_severity", "prior_status", "location", "change")

    def __init__(self, num, prior_severity, prior_status, location, change):
        self.num = num
        self.prior_severity = prior_severity
        self.prior_status = prior_status
        self.location = location
        self.change = change


def build_carry_forward_ledger(prior_body: str, inter_diff: str, base_sha: str,
                               *, sibling: bool = False) -> list:
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
    if triples or sibling:
        # Round 3+ (carried status block) or promote sibling: number-identity.
        return [LedgerEntry(num, sev, status, None, INDETERMINATE)
                for num, sev, status in triples]
    # Round 2: fresh prior — line evidence is safe and honors real fixes.
    fresh = extract_fresh_findings(prior_body)
    if not fresh:
        return []
    locs = extract_fresh_finding_locations(prior_body, base_sha or "")
    index = parse_changed_lines(inter_diff or "")
    ledger = []
    for num, sev, status in fresh:
        loc = locs.get(num)
        change = finding_changed(loc, index) if loc else INDETERMINATE
        ledger.append(LedgerEntry(num, sev, status, loc, change))
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
    sec = re.search(r"^###\s+Previous Findings Status\s*$", body, re.MULTILINE)
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
        # FIXED on non-CHANGED code is the hide-a-finding rewrite — but only
        # ENFORCE it for gate-relevant severities (medium+). Severity is already
        # pinned to max(prior, emitted) above, so a laundered blocker can't hide
        # behind a low/nit tag; a genuinely low/nit finding never gates, so
        # trusting its explicit FIXED just avoids needless re-open noise on long
        # chains. (Resurrection of a SILENTLY-dropped finding is unaffected — an
        # absent finding is suspicious regardless of severity; only an explicit
        # FIXED is trusted here.)
        if status == "FIXED" and entry.change != CHANGED and _SEVERITY_RANK.get(new_sev, 3) >= 2:
            new_status = "NOT FIXED"
            log.append(f"[pin] #{num} FIXED->NOT FIXED (no code change; change={entry.change})")
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
        "--pin", action="store_true",
        help="Read a re-review body on stdin; apply the deterministic severity-"
             "pin + ledger resurrection (needs --prior-body/--inter-diff/--base-"
             "sha) and print the corrected body on stdout, [pin]/[ledger] log on "
             "stderr. No-op (body verbatim) without the ledger inputs.",
    )
    parser.add_argument("--prior-body", help="Path to the prior review body (re-review ledger input).")
    parser.add_argument("--inter-diff", help="Path to the inter-diff (re-review ledger input).")
    parser.add_argument("--base-sha", default="", help="Prior-reviewed SHA (inter-diff base) for anchor validation.")
    args = parser.parse_args(argv)
    if not (args.decide or args.count_blockers or args.pin):
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
        ledger = build_carry_forward_ledger(prior, inter, args.base_sha)
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
    # AIR_CATEGORY_FLOOR=0/false/no is the fresh-gate floor kill switch (same
    # grammar as AIR_LEDGER_PIN). The CLI's Step 12 `--decide` inherits it from
    # the environment, so managed CI and the CLI gate identically.
    floor = os.environ.get("AIR_CATEGORY_FLOOR", "1").strip().lower() not in ("0", "false", "no")
    request_changes, reason = should_request_changes(_maybe_pin(body), floor_exposures=floor)
    print(f"request-changes\t{reason}" if request_changes else "approve")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
