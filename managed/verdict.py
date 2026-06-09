"""Review-body parsing + verdict gating (pure functions, no network).

Extracted verbatim from review.py (module split): blocker counting, prior-status
parsing, the re-review gating decision, and the SHA-validated `## Code Review`
body extractor.
"""
import re
import sys
from pathlib import Path

# plugins/air/lib is shared between the CLI plugin and managed mode
# (same mechanism as review.py's own tweak; idempotent).
_AIR_LIB_DIR = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
if str(_AIR_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_AIR_LIB_DIR))

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
      body but no longer flip the verdict — see svc-transcribe #37 for
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
    svc-transcribe #37 finding #2: 13 consecutive `NOT FIXED` rounds on
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


def should_request_changes(review_body: str) -> tuple[bool, str]:
    """Decide whether to submit REQUEST_CHANGES instead of APPROVE.

    Returns (request_changes, reason). The verdict drives `reviewDecision`
    and branch-protection state.

    - Fresh review: REQUEST_CHANGES if any blockers exist.
    - Re-review: REQUEST_CHANGES if any NEW blockers exist OR any prior
      finding originally classified as `blocker` is still NOT FIXED /
      PARTIALLY FIXED / DEFERRED. Medium / low / nit prior findings left
      unfixed do NOT gate — they appear in the body as recommendations.
      A developer can clear a blocker gate by either fixing, explicitly
      disputing (verifier marks DISPUTED), or — for prompt-edge cases —
      escalating to a human reviewer.
    """
    blockers = count_blockers(review_body)
    if _REREVIEW_HEADER_RE.search(review_body):
        unfixed = _count_gating_unfixed(review_body)
        if blockers > 0 and unfixed > 0:
            return True, f"{blockers} new blocker(s), {unfixed} prior blocker(s) still unfixed"
        if blockers > 0:
            return True, f"{blockers} new blocker(s)"
        if unfixed > 0:
            return True, f"{unfixed} prior blocker(s) still unfixed"
        return False, ""
    if blockers > 0:
        return True, f"{blockers} blocker(s)"
    return False, ""


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
# Why blocker-only: svc-transcribe #37 spent 13 consecutive re-review
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
_PRIOR_STATUS_RE = re.compile(
    r"^-\s+\*\*#(\d+)\*\*"
    r"(?:\s*\[(blocker|medium|low|nit)\])?"
    r"\s+—\s+"
    r"(FIXED|NOT\s+FIXED|PARTIALLY\s+FIXED|DEFERRED|DISPUTED)\b",
    re.MULTILINE | re.IGNORECASE,
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
    match = REVIEWED_AT_RE.search(body or "")
    return match.group(1) if match else None


# Anti-spoof: compare the `Reviewed at:` footer SHA on a 12-hex-char prefix
# (48 bits — unguessable for spoofing) rather than full 40-char equality, which
# proved too strict (models occasionally corrupt the SHA tail; svc-transcribe
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
    # (qai-be #635 had narration concatenated on the same line). Walk
    # candidates in reverse; pick the first whose footer matches.
    #
    # A candidate's bound is the NEXT `## Code Review` header (or EOF) — the
    # true candidate boundary — NOT the next generic `## ` line. A `## ` line
    # inside the body (a fenced markdown example quoting a heading, a
    # malformed mid-review section) is content; bounding on it cut the
    # candidate before its footer and converted a fully-billed review into a
    # run-failed comment (2026-06-09 audit). Markdown-fence parsing was
    # evaluated and rejected for this: transcript-wide fence parity is
    # hostile territory (unterminated fences in narration, four-backtick
    # nesting, quoted diffs toggling parity) — adversarial review reproduced
    # extraction drops on all three. The header bound + SHA validation need
    # no fence model: extraction always ENDS at the matching footer, so an
    # over-wide bound can at worst include same-message content, and a
    # too-early header can't capture a later candidate's footer (the bound
    # stops at that candidate's own header).
    _header_re = re.compile(r"(?<!`)## Code Review[^\n]*\n")
    # NOTE: do NOT add `\b` between the 40-char hex and `[^\n]*`. Word-boundary
    # fails when the SHA is followed by another word char (qai-be #666 round 7:
    # `...936Wiki push failed...` had no boundary between `6` and `W`). The
    # 40-char exact quantifier is the anchor; the 12-char prefix compare below
    # is the validator. `[^\n]*` eats the rest of the line so match end is defined.
    # Case-insensitive to match REVIEWED_AT_RE (the skip-gate parser): the two
    # used to disagree — `Reviewed At:` passed the gate but failed extraction.
    _footer_re = re.compile(r"\nReviewed at:\s+([0-9a-fA-F]{40})[^\n]*", re.IGNORECASE)
    _expected_prefix = head_sha.lower()[:_SHA_PREFIX_LEN]

    _candidates = []
    for _hm in _header_re.finditer(_flattened):
        _body_start = _hm.end()
        _next_header = _header_re.search(_flattened, _body_start)
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
        _candidates.append((_hm.start(), _fm.end(), _fm.group(1)))
    # Anti-spoof validator (see _SHA_PREFIX_LEN): a poisoned diff can echo
    # `## Code Review` but can't predict the run's head SHA. Prefix equality
    # keeps the security property while tolerating model tail-corruption.
    head_sha = head_sha.lower()
    for _start, _end, _sha in reversed(_candidates):
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
