#!/usr/bin/env python3
"""
Trigger an air review via Managed Agents — single multi-agent coordinator.

The Python driver does upstream client-side prep (fetch PR data, state
gates, mode detection, build PR context, optionally run codex), then
hands off to a single `air-coordinator` session that dispatches the 4
specialists in parallel + verifier as `callable_agents` sub-agents
within one Anthropic session, mirroring the local CLI's architecture.

Codex stays client-side and completes BEFORE the coordinator session —
Sonnet coordinator with codex inside doesn't parallelize reliably and
Opus coordinator costs ~2.5× the Sonnet equivalent. Pattern B (GHA-side
codex → coordinator user message) keeps clean parallelism. The codex
subprocess launches as a background task that overlaps the precomp +
context-build leg (precomp runs in a worker thread so the event loop
stays free to drain codex's pipes).

Replaces the prior asyncio.gather over 4 specialist sessions + sequential
verifier session (5 sessions → 1) introduced in v1.7.0; that shape was
chosen when `callable_agents` was research-preview and inaccessible.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export AIR_BOT_TOKEN=ghp_...
    python review.py myorg/myrepo 123
    python review.py myorg/myrepo 123 --dry-run
"""

import argparse
import asyncio
import fnmatch
import html
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from anthropic import Anthropic, AsyncAnthropic
from requests import RequestException

from api import list_agents, find_environment
from setup import MODEL_ALIASES, parse_agent_pins
import memory_store
import pattern_writer
import render_store_to_wiki

# Make plugins/air/lib importable so we share stdlib helpers (the
# conversation merger and the review-header constant) with the CLI path
# at top-level rather than via per-call sys.path inserts. Crash loudly if
# the layout is broken — degrading silently here would let managed runs
# diverge from the CLI on bot-self-filter behavior.
_AIR_LIB_DIR = Path(__file__).resolve().parent.parent / "plugins" / "air" / "lib"
if str(_AIR_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_AIR_LIB_DIR))

import pr_conversation  # noqa: E402  (deferred import; relies on sys.path tweak above)

# --- Module split: moved code lives in github_client / verdict / session_runner /
# --- prompts (same dir). Names re-exported so `from review import X` keeps
# --- working, and `setattr(review, ...)` patching still reaches every caller
# --- that LIVES in this module. Cross-module calls (e.g. verdict.should_request_changes
# --- → count_blockers) resolve in the owning module's namespace — patch there.
from github_client import (  # noqa: E402,F401 — split modules; re-exported for tests/callers
    PartialPageError,
    _github_error_message,
    _GH_DUPLICATE_HINTS,
    _post_review_comment_with_retry,
    _gh_error_message_only,
    fetch_pr_metadata,
    submit_review_verdict,
    dismiss_stale_air_verdicts,
    fetch_pr_diff,
    _github_paginate,
    fetch_bot_login,
    fetch_issue_comments,
    fetch_pr_reviews,
    fetch_pr_review_comments,
    fetch_inter_diff,
    count_diff_changed_lines,
    DIFF_TRUNCATION_MARKER,
)
from verdict import (  # noqa: E402,F401 — split modules; re-exported for tests/callers
    count_blockers,
    _count_gating_unfixed,
    extract_prior_statuses,
    format_prior_statuses_block,
    should_request_changes,
    has_conflict_markers,
    REVIEWED_AT_RE,
    _BLOCKERS_SECTION_RE,
    _BLOCKER_ENTRY_RE,
    _PRIOR_STATUS_RE,
    _GATING_SEVERITIES,
    _GATING_STATUSES,
    CARRY_FORWARD_THRESHOLD,
    _BLOCKER_DEFERRED_STATUS,
    _REREVIEW_HEADER_RE,
    PRIOR_REVIEW_MAX_CHARS,
    find_prior_review,
    extract_reviewed_at_sha,
    _SHA_PREFIX_LEN,
    _extract_review_body,
    build_carry_forward_ledger,
    pin_and_resurrect,
)
from session_runner import (  # noqa: E402,F401 — split modules; re-exported for tests/callers
    LIVE_SESSIONS,
    INTERRUPT_EVENT,
    _interrupt_live_sessions_sync,
    _install_shutdown_handlers,
    SESSION_TIMEOUT_SECS,
    COORDINATOR_TIMEOUT_SECS,
    SpecialistSessionError,
    _BILLING_REASON_HINTS,
    BILLING_RETRY_MAX_ATTEMPTS,
    BILLING_RETRY_BACKOFF_SECS,
    BILLING_RETRY_PREFLIGHT_SECS,
    run_session,
    _run_session_with_billing_retry,
    build_session_metadata,
)
from prompts import (  # noqa: E402,F401 — split modules; re-exported for tests/callers
    build_pr_context,
    build_verifier_task,
)


SPECIALIST_AGENTS = [
    "air-code-reviewer",
    "air-simplify",
    "air-security-auditor",
    "air-git-history-reviewer",
]

VERIFIER_AGENT = "air-review-verifier"

COORDINATOR_AGENT = "air-coordinator"

# GA multiagent-roster coordinator (PR6′ migration, opt-in via
# AIR_MULTIAGENT=1, default off). Same prompt as air-coordinator; the
# delegation primitive differs — its roster shares /workspace across
# threads, enabling MODE: WORKSPACE-HANDOFF (the coordinator writes
# context files ONCE instead of re-emitting them into every delegation).
# Created by setup.py only when the flag is on; not pinnable.
# AIR_MA_COORDINATOR_MODEL opts a caller into a cheaper/faster coordinator tier
# (e.g. "haiku"). The MA coordinator only delegates + relays the verifier's
# review VERBATIM (it does NOT synthesize), so a cheaper tier is relay-safe
# (validated 2026-06-19: 0 findings dropped, verdict unchanged) and cuts
# idle-wake latency. Routes to a SEPARATE agent (created by setup.py 4c) so a
# per-repo opt-in never touches the shared Sonnet coordinator. Default / an
# unset / "sonnet" / unknown value → the standard air-coordinator-ma.
def _ma_coordinator_name(tier: str) -> str:
    """Route the MA coordinator to a tiered agent (air-coordinator-ma-<alias>)
    when AIR_MA_COORDINATOR_MODEL names a known, non-default (non-sonnet) model;
    otherwise the standard air-coordinator-ma. Unset / "sonnet" / unknown → the
    standard agent (fail-safe to the validated default)."""
    t = (tier or "").strip().lower()
    return f"air-coordinator-ma-{t}" if t in MODEL_ALIASES and t != "sonnet" else "air-coordinator-ma"


COORDINATOR_MA_AGENT = _ma_coordinator_name(os.environ.get("AIR_MA_COORDINATOR_MODEL", ""))


def _multiagent_enabled() -> bool:
    return os.environ.get("AIR_MULTIAGENT", "") in ("1", "true")


def _air_bot_logins() -> frozenset:
    """Optional allowlist of bot accounts air rotates through — lets the
    gate-orphan cleanup recognize air's OWN prior verdicts left under a
    different account even on LEGACY reviews posted before the verdict
    sentinel shipped. Sourced from the caller's `AIR_PAT_MAP` (login→secret
    map) keys if present in env, plus an optional comma-separated
    `AIR_BOT_LOGINS`. Empty when neither is set — then only sentinel-stamped
    verdicts are recognized, which already covers everything posted since the
    sentinel shipped. Never hardcoded (anonymization)."""
    logins = set()
    raw = os.environ.get("AIR_PAT_MAP", "").strip()
    if raw:
        try:
            logins.update(json.loads(raw).keys())
        except (ValueError, AttributeError):
            pass
    logins.update(x.strip() for x in os.environ.get("AIR_BOT_LOGINS", "").split(",") if x.strip())
    return frozenset(logins)


def _ledger_pin_enabled() -> bool:
    # PR 7 re-review severity-pin + finding resurrection. Default ON;
    # AIR_LEDGER_PIN=0/false is the instant kill switch (caller/org variable,
    # no deploy). Read once; gates BOTH halves together — the advisory prompt
    # ledger block AND the deterministic pin_and_resurrect guard — so there's
    # never a half-on state.
    return os.environ.get("AIR_LEDGER_PIN", "1").strip().lower() not in ("0", "false", "no")


def _category_floor_enabled() -> bool:
    # PR-(b) fresh-gate determinism. Default ON; AIR_CATEGORY_FLOOR=0/false is
    # the instant kill switch (caller/org variable, no deploy). Floors any
    # `[sec:<cat>]`-tagged blocker-class exposure to a blocker for the gate,
    # so a weaker tier rating a real PII/authz/credential exposure "medium"
    # can no longer silently un-gate. Inert on tag-less bodies, so disabling
    # is byte-identical to the pre-floor gate. Same kill-switch grammar as
    # AIR_LEDGER_PIN — read once, applied at every should_request_changes site.
    return os.environ.get("AIR_CATEGORY_FLOOR", "1").strip().lower() not in ("0", "false", "no")


def _post_verifier_body_enabled() -> bool:
    # Direct-post: post the VERIFIER's body verbatim instead of the
    # coordinator's relay. The coordinator is a pure relay layer (the verifier
    # synthesizes), so its relay turn is the only place a cheap coordinator can
    # drop findings (the 2026-06-20 Haiku 11→9 drop on repo-A #1243). Posting
    # the verifier body directly removes that failure mode for ANY coordinator
    # model. Default OFF (opt-in until fleet-soaked); set AIR_POST_VERIFIER_BODY=1.
    # Safe-by-construction: falls back to the coordinator relay when no verifier
    # body is captured or it fails SHA validation.
    return os.environ.get("AIR_POST_VERIFIER_BODY", "").strip().lower() in ("1", "true", "yes")


# Finding-title line: `**1. <title>` or re-review `**#3. <title>`. The title
# text (normalized) is the join key between the coordinator's relayed findings
# and the verifier's delivered ones.
_FINDING_TITLE_RE = re.compile(r"(?m)^\*\*#?\d+\.\s*(.+?)\s*$")
# Sections the VERIFIER template emits that specialists do not — used to reject
# specialist bodies (which also carry a `## Code Review` header) before any
# title comparison.
_VERIFIER_ONLY_SECTIONS = ("### Strengths", "### Pre-existing", "### Blockers")
# The coordinator relays a SUBSET of the verifier's findings (the drop), so the
# real verifier body's titles cover (nearly) all relayed titles. A specialist's
# titles are its own pre-verification wording → low coverage. 0.6 tolerates
# minor title reformatting (e.g. the coordinator escaping backticks) while
# still cleanly separating the verifier (~1.0) from any specialist (~0).
_VERIFIER_COVERAGE_MIN = 0.6


def _finding_titles(body: str) -> list[str]:
    """Normalized finding-title prefixes (`**N. <title>` lines) for matching."""
    out = []
    for m in _FINDING_TITLE_RE.finditer(body):
        norm = re.sub(r"[^a-z0-9]", "", m.group(1).lower())[:40]
        if norm:
            out.append(norm)
    return out


def _append_review_footer(body: str, head_sha: str) -> str:
    """The verifier delivers review CONTENT without the `Reviewed at:` footer
    (the coordinator appends it on relay). For direct-post we append it
    ourselves — head_sha is deterministic and ours, so it can't be spoofed and
    is more trustworthy than a model-emitted footer. Makes the body pass
    _extract_review_body + the re-review skip-gate."""
    return (
        f"{body.rstrip()}\n\n---\n\nReviewed at: {head_sha}\n\n"
        "> After fixing, run `/air:review --respond` to verify and reply.\n"
    )


def _select_verifier_body(candidates, coord_body, head_sha):
    """Pick the verifier's delivered body from the captured `## Code Review`
    candidates (specialists emit that header too). Returns (body, status).

    Safe by construction — only returns a candidate that is UNAMBIGUOUSLY the
    body the coordinator relayed from: it must carry a verifier-only section
    AND its finding titles must cover >= `_VERIFIER_COVERAGE_MIN` of the
    coordinator's relayed titles AND it must have >= as many findings as the
    relay (the verifier body is a superset of the dropped relay). Anything else
    falls back to the coordinator body — a specialist's (unverified) body can
    never be selected.
    """
    relayed = _finding_titles(coord_body)
    if not relayed:
        # 0 relayed findings → can't distinguish a clean approve from a total
        # drop by coverage; keep the coordinator body (a total drop would
        # surface as a run-failure, handled elsewhere). Documented v1 limit.
        return coord_body, "no-relayed-findings"
    relayed_set = set(relayed)
    best, best_cov, best_n = None, 0.0, 0
    for c in candidates:
        if not re.search(r"(?m)^## Code Review", c):
            continue
        if not any(s in c for s in _VERIFIER_ONLY_SECTIONS):
            continue  # specialist-shaped → never a candidate
        ct = _finding_titles(c)
        if not ct:
            continue
        cov = len(relayed_set & set(ct)) / len(relayed_set)
        if cov > best_cov or (cov == best_cov and len(ct) > best_n):
            best, best_cov, best_n = c, cov, len(ct)
    if best is None or best_cov < _VERIFIER_COVERAGE_MIN:
        return coord_body, "no-verifier-match"
    if best_n < len(relayed):
        return coord_body, "fewer-findings"  # never post FEWER than the relay
    return best, "direct"


def _select_review_source(coordinator_out, session_capture, head_sha, review_arch):
    """Choose the body to post: the verifier's delivered body (direct-post,
    recovering any findings the coordinator's relay dropped) or the coordinator
    relay. Returns (source_text, status_msg). Direct-post applies only to the
    coordinator architectures (full/both) — solo IS the reviewer. The selected
    verifier body is re-validated through the SAME SHA-checking extractor after
    footer synthesis, so direct-post can ONLY ever swap in a faithful, SHA-valid
    verifier body that supersets the relay — never a specialist/unverified body.
    """
    if session_capture is None or review_arch not in ("full", "both"):
        return coordinator_out, "off"
    candidates = session_capture.get("received_reviews") or []
    if not candidates:
        return coordinator_out, "no candidates captured"
    coord_body, coord_ok = _extract_review_body(coordinator_out, head_sha)
    if not coord_ok:
        return coordinator_out, "coordinator body not extractable"
    verifier, status = _select_verifier_body(candidates, coord_body, head_sha)
    if status != "direct":
        return coordinator_out, status
    finalized = _append_review_footer(verifier, head_sha)
    if not _extract_review_body(finalized, head_sha)[1]:
        return coordinator_out, "verifier body failed SHA re-validation"
    recovered = len(_finding_titles(verifier)) - len(_finding_titles(coord_body))
    return finalized, f"direct (verifier body; +{recovered} finding(s) the relay dropped)"


def _required_agents(review_arch: str) -> list[str]:
    """The agents a run must find synced before any session spend.

    Conditional on the architecture: full needs specialists+verifier+
    coordinator; solo needs only the solo agent; both needs all. The MA
    coordinator joins only when AIR_MULTIAGENT is on AND the architecture
    uses a coordinator at all — solo never does, so the flag can't make a
    solo run depend on an agent it never sessions."""
    if review_arch == "solo":
        return [SOLO_AGENT]
    required = SPECIALIST_AGENTS + [VERIFIER_AGENT, COORDINATOR_AGENT]
    if review_arch == "both":
        required = required + [SOLO_AGENT]
    if _multiagent_enabled():
        required = required + [COORDINATOR_MA_AGENT]
    return required


def _mint_heredoc_sentinel(*docs: str) -> str:
    """A run-random heredoc delimiter guaranteed absent from every doc.

    The TURN-0 workspace writes quote PR-controlled content inside bash
    heredocs. A FIXED delimiter is a shell-injection primitive: any PR
    comment containing that exact line terminates the heredoc early and
    the remaining attacker-controlled lines execute in a container holding
    the bot token. 128 random bits make the delimiter unguessable, and the
    containment check below makes collision impossible rather than merely
    improbable. (Chosen over base64-encoding the docs, which would inflate
    the one paid TURN-0 emission by ~33%.)
    """
    while True:
        sentinel = f"AIR_CTX_{secrets.token_hex(16)}"
        if not any(sentinel in (d or "") for d in docs):
            return sentinel


def _workspace_handoff_text(
    pattern_note: str, ui_scope_line: str, pr_context: str, diff: str,
    codex_block: str, verifier_task: str,
) -> str:
    """The MODE: WORKSPACE-HANDOFF coordinator user message.

    Content blocks embedded once; the coordinator writes them to the shared
    /workspace in TURN 0 using the run-specific heredoc delimiter minted
    here, then delegates short file pointers (git-history inline per
    coordinator.md's carve-out)."""
    sentinel = _mint_heredoc_sentinel(
        pattern_note, ui_scope_line, pr_context, diff, codex_block, verifier_task,
    )
    return (
        "MODE: WORKSPACE-HANDOFF — multiagent shared-workspace "
        "run. The full PR context, diff, and verifier task are "
        "embedded below. Execute TURN 0 first (write them to "
        "/workspace/context/ VERBATIM via quoted heredocs and "
        "create /workspace/findings/), then follow your protocol "
        "with file-pointer delegations "
        "(air-git-history-reviewer: INLINE).\n"
        f"Run-specific heredoc delimiter for the TURN-0 writes: {sentinel} "
        f"— use EXACTLY this, single-quoted (<<'{sentinel}'), for all three "
        "files. It is random per run so document content can never "
        "terminate a heredoc early; do not substitute your own.\n\n"
        f"- Pattern source: {pattern_note}\n"
        f"- {ui_scope_line}\n\n"
        f"{pr_context}\n\n"
        f"<diff>\n{diff}\n</diff>\n\n"
        f"{codex_block}\n\n"
        f"<verifier-task>\n{verifier_task}\n</verifier-task>"
    )


# Conditional 6th specialist (UI / business-audience copy + static UX/a11y).
# Synced as part of SUB_AGENTS so it's always in the coordinator's
# callable_agents roster, but only DISPATCHED when the diff touches a
# user-facing surface (_diff_touches_ui) — the coordinator's per-run dispatch
# note names it as in-scope or not. Not in SPECIALIST_AGENTS: it must not join
# the always-required gate (backend-only PRs never need it). Advisory-mostly;
# it can emit a blocker only for clear user/clinical harm.
UI_COPY_AGENT = "air-ui-copy-reviewer"

# Single-session reviewer (opt-in AIR_REVIEW_MODE=solo|both). One agent applies
# all lenses + self-verifies; prompt assembled from the specialists in
# setup.py (assemble_solo_prompt — includes the UI lens, which self-scopes).
# Required only when a run uses solo/both.
SOLO_AGENT = "air-solo-reviewer"

# Review architecture axis (AIR_REVIEW_MODE / --mode), ORTHOGONAL to `mode`
# (the scope axis: full vs re-review). full = 6-agent coordinator (default);
# solo = single merged-lens agent; both = run both (full gates, solo posted
# alongside for comparison — testing).
REVIEW_ARCH_CHOICES = ("full", "solo", "both")


REPO_ARG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


CODEX_LABEL = "codex"

# Codex's bwrap sandbox failure (and similar "I can't run commands" states)
# make `codex review` EXIT 0 but emit a first-person apology INSTEAD of
# findings — e.g. "I could not inspect the diff because every shell command
# failed in the provided sandbox." Before the v1.19.1 config.toml fix this
# happened on EVERY managed review for ~5 weeks, and the apology text was
# forwarded to the coordinator as if it were findings — so a fleet-wide
# regression looked green. The guard below treats these signatures as a hard
# failure (raise → caller logs `[warn] codex failed` and proceeds without it)
# so the next regression surfaces loudly instead of silently degrading 5
# reviewers to 4.
#
# HARD signatures are codex's own runtime-inability phrasing (unlikely in a
# real review even one that *discusses* sandboxing code). SOFT is a generic
# first-person "I could not inspect/review" — gated on a short total length,
# because a genuine review is long and structured while an apology is a
# sentence or two.
_CODEX_HARD_FAIL_RE = re.compile(
    r"could not inspect the (diff|changes|repository)"
    r"|every shell command.{0,60}(failed|denied|blocked|not permitted)"
    r"|RTM_NEWADDR"
    r"|\bbwrap\b"
    r"|in the (provided|restricted) sandbox",
    re.IGNORECASE,
)
_CODEX_SOFT_FAIL_RE = re.compile(
    r"\bI(?:'m|\s+am|\s+was)?\s+(?:could\s+not|couldn'?t|cannot|can'?t|unable\s+to)\b"
    r".{0,80}\b(inspect|review|access|read|run|execute|analyze)\b",
    re.IGNORECASE,
)
_CODEX_SOFT_FAIL_MAX_LEN = 1200


def _kill_process_group(proc) -> None:
    """SIGKILL the subprocess's whole process group, falling back to the bare
    process. The subprocess must have been spawned with start_new_session=True
    so it leads its own group; killing the group takes its children (which
    inherit the stdio pipes) with it so the reap can't hang. Best-effort —
    a process that already exited (ProcessLookupError) is the success case."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_codex_session(target_repo: str, base_sha: str) -> str:
    """Invoke `codex review --base <sha>` in the target repo; return stdout.

    Opt-in 5th specialist. The caller (`run_review`) is responsible for
    deciding whether to launch this — the three environmental
    preconditions (OPENAI_API_KEY, codex binary, AIR_TARGET_REPO) are
    gated there, not here. The single safety check below catches a
    directory that disappeared between the gate and this call.

    Raises SpecialistSessionError on any non-success path so the caller
    can include `(codex unavailable or disabled)` in the coordinator user
    message instead of silently posting a failure string as if it were
    findings.

    Subprocess lifecycle: the outer asyncio.wait_for in run_review cancels
    this coroutine on timeout, which raises CancelledError into our
    try/finally — finally path kills the subprocess so it doesn't outlive
    the review and burn OpenAI tokens.
    """
    if not os.path.isdir(target_repo):
        raise SpecialistSessionError(CODEX_LABEL, f"target repo not found: {target_repo}")

    # Codex's default sandbox uses bubblewrap (Linux user namespaces) to
    # isolate model-generated shell commands. GHA runners run inside
    # containers that block nested user namespaces — bwrap fails with
    # `RTM_NEWADDR: Operation not permitted`, so codex can't even run
    # `git diff` and emits a one-line apology instead of findings (PR #41
    # first run: 24.9s, output `I could not inspect the diff because every
    # shell command... failed in the provided sandbox`).
    #
    # `--dangerously-bypass-approvals-and-sandbox` asks codex to skip its
    # internal bwrap sandbox AND approval prompts. NOTE: this GLOBAL flag does
    # NOT propagate to the `review` subcommand's command-execution sandbox on
    # the pinned codex — `codex review` still tries bwrap and fails on GHA
    # runners (blocked user/net namespaces → loopback `RTM_NEWADDR` error →
    # "could not inspect the diff" apology). The ACTUAL fix lives in
    # managed-review.yml's Codex-setup step, which writes
    # `~/.codex/config.toml` with `sandbox_mode = "danger-full-access"` +
    # `approval_policy = "never"` (config IS honored by `review`). We keep the
    # flag here as belt-and-suspenders. The runner IS sandboxed at
    # the OS level (ephemeral VM, destroyed after the job), but it does
    # carry secrets — AIR_BOT_TOKEN (repo write), ANTHROPIC_API_KEY (cost
    # exposure), OPENAI_API_KEY (cost exposure). Without sandbox/approval
    # gates, a prompt-injection payload buried in a PR diff could ask the
    # codex model to run shell commands that exfiltrate those.
    #
    # Mitigation: pass a narrow `env=` that omits all three. Codex reads
    # OPENAI_API_KEY from ~/.codex/auth.json (written by the workflow's
    # `codex login` step), so it doesn't need it in env. AIR_BOT_TOKEN and
    # ANTHROPIC_API_KEY are unrelated to codex.
    #
    # Residual risk: a prompt-injected codex run could still `cat
    # ~/.codex/auth.json` (the model has shell access) — but that file
    # only leaks OPENAI_API_KEY, not the GitHub or Anthropic tokens.
    # External-contributor PR diffs remain the main attack surface; the
    # dogfood workflow gates on `air-machine` review-requested only, but
    # the reusable `managed-review.yml` example still shows the broader
    # `[opened, synchronize]` trigger — consumers handling untrusted
    # contributors should switch to review-requested.
    narrow_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "TERM": os.environ.get("TERM", "dumb"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    print(f"  [launch] {CODEX_LABEL} → codex review --base {base_sha[:8]}")
    proc = await asyncio.create_subprocess_exec(
        "codex", "--dangerously-bypass-approvals-and-sandbox",
        "review", "--base", base_sha,
        cwd=target_repo,
        env=narrow_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Own process group so the cancel path can kill codex AND its children.
        # `codex` spawns child processes that inherit the stdout/stderr pipes;
        # without this, proc.kill() reaps only the parent and the orphaned
        # children keep the pipe write-ends open, so the reap below blocks
        # forever and the whole review hangs past its budget until the runner
        # SIGKILLs the job (observed: repo-D #124 re-reviews cancelled at ~31m
        # with no review ever posted, because codex hung on the delta).
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # Outer wait_for timed out (or the watchdog fired). Kill the whole
        # process GROUP so codex's children die too and release the pipes,
        # then reap with a hard bound so a stubborn child can never re-hang us.
        _kill_process_group(proc)
        try:
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except (Exception, asyncio.TimeoutError):
            pass
        raise

    if proc.returncode != 0:
        err = stderr.decode()[:500] if stderr else "(no stderr)"
        raise SpecialistSessionError(
            CODEX_LABEL, f"exit {proc.returncode}: {err}"
        )

    output = stdout.decode().strip()
    if not output:
        raise SpecialistSessionError(CODEX_LABEL, "empty stdout")
    # Fail loud on a sandbox/inability apology returned with exit 0 (see the
    # _CODEX_*_FAIL_RE comment above) — never forward it as findings.
    if _CODEX_HARD_FAIL_RE.search(output) or (
        len(output) < _CODEX_SOFT_FAIL_MAX_LEN and _CODEX_SOFT_FAIL_RE.search(output)
    ):
        raise SpecialistSessionError(
            CODEX_LABEL,
            "sandbox/inability signature in output — codex produced no usable "
            f"findings (likely a bwrap/sandbox regression). First 200 chars: {output[:200]!r}",
        )
    print(f"  [done] {CODEX_LABEL}")
    return output


def sync_agents(review_arch: str = "full"):
    """Run setup.py to create/update agents (pinned agents skip sync)."""
    print("[1] Syncing agents with latest prompts...")
    # Narrow env to only what setup.py needs, avoiding accidental exposure of
    # unrelated secrets if the parent process has a richer environment.
    narrow_env = {
        "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"],
        "PATH": os.environ.get("PATH", ""),
        # Version pins (JSON map agent-name → version) — setup.py skips
        # prompt sync for pinned agents; run_review applies the same pins
        # to the session roster.
        "AIR_AGENT_VERSIONS": os.environ.get("AIR_AGENT_VERSIONS", ""),
        # The resolved review architecture — setup.py only creates the
        # air-solo-reviewer agent when the run actually needs it (solo/both),
        # so a full-only run never creates it (and can't be aborted by a
        # solo-agent creation failure on an at-capacity workspace).
        "AIR_REVIEW_MODE": review_arch,
        # Same conditional-create posture for the multiagent coordinator
        # (air-coordinator-ma): only synced when the run opts in.
        "AIR_MULTIAGENT": os.environ.get("AIR_MULTIAGENT", ""),
        # Opt-in cheaper MA coordinator tier — setup.py 4c creates the
        # air-coordinator-ma-<alias> agent this run will route to.
        "AIR_MA_COORDINATOR_MODEL": os.environ.get("AIR_MA_COORDINATOR_MODEL", ""),
    }
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "setup.py")],
        env=narrow_env,
    )
    if result.returncode != 0:
        print("Error: agent sync failed.", file=sys.stderr)
        sys.exit(1)


def _gha_run_url() -> str | None:
    """Best-effort GitHub Actions run URL from environment.

    Returns a useful link when invoked from a managed-review workflow.
    Returns None when invoked locally / outside Actions so callers can
    omit the `Run:` line entirely instead of rendering an awkward
    placeholder inside angle brackets in posted markdown.
    """
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


# Cap for raw-error text echoed into the run-failed PR comment. 800 chars
# captures the meaningful prefix of typical Anthropic SDK exception
# reprs (~600-1200 chars) without bloating the comment. Mirrored across
# both the billing and other-failure branches via a single constant so
# they can't drift.
_RAW_REASON_MAX_CHARS = 800


def _exit_nonzero_on_failed_run(
    pr_number: int, coordinator_failure_reason: str, posted: bool
) -> None:
    """Fail the job loudly when the run produced no usable review.

    Historically these outcomes exited 0 (green checkmark) with only a
    run-failed PR comment — the 2026-05-22 billing exhaustion sat
    invisible for 11 days, and 2026-06-02's sat ~4 hours across three
    repos. A red X + `::error::` annotation surfaces the cause in the
    Actions UI, in `gh run view`, and to anything investigating the
    failure programmatically. The annotation is single-line by GHA
    contract — newlines in the reason are flattened.
    """
    _lower = coordinator_failure_reason.lower()
    if any(h in _lower for h in _BILLING_REASON_HINTS):
        kind = "billing exhausted"
        hint = ("top up at console.anthropic.com (or rotate "
                "ANTHROPIC_API_KEY), then re-request the review")
    elif coordinator_failure_reason:
        kind = "coordinator session failed"
        hint = "see the [warn] lines above"
    else:
        kind = "unusable coordinator output"
        hint = "likely stale-cache/SHA-mismatch — see the [debug] dump above"
    reason = (coordinator_failure_reason
              or "no usable `## Code Review` block in coordinator output")
    reason = reason[:300].replace("\n", " ")
    comment_note = (f"run-failed comment posted to PR #{pr_number}"
                    if posted else "dry run — no comment posted")
    print(f"::error title=air review failed — {kind}::{reason} | {comment_note} | {hint}")
    sys.exit(1)


# --- Promote fast-path -------------------------------------------------------
# A `promote/staging-to-main-*` PR is one link in a chain: each new promote
# re-opens nearly the same staging→main changeset its predecessor carried, so
# reviewing each from scratch re-pays for code an earlier promote already
# cleared. When the current promote overlaps its last-merged, already-reviewed
# sibling by >= PROMOTE_OVERLAP_THRESHOLD, we re-review against the sibling's
# reviewed SHA (a tiny inter-diff) instead of a full re-read. Opt-in via
# AIR_PROMOTE_FASTPATH; conservative — any uncertainty falls back to full.
PROMOTE_HEAD_PREFIX = "promote/staging-to-main-"
PROMOTE_OVERLAP_THRESHOLD = 0.80
# Cap the sibling search. The closed-PR list is newest-first, so the most
# recent merged sibling is on page 1 in practice; three pages (300 PRs) is a
# generous ceiling that bounds cost on busy repos.
_PROMOTE_MAX_SIBLING_PAGES = 3


# Codex is an advisory extra pass; below this many changed inter-diff lines
# a re-review delta is well inside the specialists' easy range and not worth
# codex's wall-time leg + session.
CODEX_RE_REVIEW_MIN_LINES = 20

# Tail-cap for the <pr-conversation> block (lib default is 100). The block
# rides in EVERY context copy (~11-13× per review); the lib keeps the
# NEWEST entries and emits <conv-truncated>, so old resolved threads age
# out first. Managed-only — the CLI bash path keeps the lib default.
CONVERSATION_MAX_ENTRIES = 30


async def _start_codex_task(
    codex_repo: str, codex_base_sha: str,
) -> tuple["asyncio.Task[str]", float, "asyncio.TimerHandle", "Callable[[], bool]"]:
    """Launch codex as a background task and ensure it has actually started.

    `asyncio.create_task` alone is lazy — the coroutine's first step (which
    spawns the codex OS subprocess) only runs when the event loop next gets
    control. The task wraps `run_codex_session` DIRECTLY (no `wait_for`
    nesting — on Python ≤3.11 `wait_for` wraps the inner coroutine in its
    own task, so one yield would step only the wrapper), which makes the
    single `await asyncio.sleep(0)` step the session body to its first
    suspension on every supported Python. The wall-clock cap is armed HERE,
    at launch, as a loop timer that cancels the task — it fires even while
    the main coroutine sits in `asyncio.to_thread`, so codex can never run
    past its budget just because the overlap window ran long (cancel
    reaches run_codex_session's finally, which kills the subprocess). The
    await site re-checks the same budget from `launch_monotonic` and must
    `timer.cancel()` when the task completes.

    Returns (task, launch_monotonic, timer, watchdog_fired). The callable is
    the ONLY reliable way to attribute a CancelledError at the await site:
    `codex_task.cancelled()` is True both when the watchdog fired AND when
    an outer cancellation (SIGTERM / loop shutdown) propagated through
    `wait_for` — which cancels the inner task before re-raising on
    Python ≤3.11 — so checking the task state would swallow real shutdowns.

    The overlap window must keep the loop free for the long legs (run them
    via `asyncio.to_thread`) so the subprocess pipes keep draining.
    """
    print(
        f"\n[3] codex launched (target-repo={codex_repo}, "
        f"base={codex_base_sha[:8]}) — overlapping precomp"
    )
    task = asyncio.create_task(run_codex_session(codex_repo, codex_base_sha))
    await asyncio.sleep(0)
    fired = False

    def _watchdog() -> None:
        nonlocal fired
        fired = True
        task.cancel()

    timer = asyncio.get_running_loop().call_later(SESSION_TIMEOUT_SECS, _watchdog)
    return task, time.monotonic(), timer, lambda: fired


def _codex_skip_tiny_delta(mode: str, diff: str) -> int | None:
    """Changed-line count when a re-review delta is too small for codex.

    Returns the count (for the decision log) when codex should be skipped,
    None when it should run. Full reviews always run codex. A byte-capped
    diff never skips: real changes may live in the omitted tail, and codex
    reads the git tree rather than this diff — it's the one lens that can
    still see them. The marker check is LINE-START anchored: diff body
    lines always begin with `+`/`-`/space, so a PR author cannot forge the
    marker from file content.
    """
    if mode != "re-review":
        return None
    if _diff_is_truncated(diff):
        return None
    n = count_diff_changed_lines(diff)
    return n if n < CODEX_RE_REVIEW_MIN_LINES else None


def _detect_promote_fastpath(
    repo: str,
    pr_number: int,
    meta: dict,
    head_sha: str,
    bot_login: str | None,
    token: str,
) -> tuple[dict, str, int] | None:
    """Resolve a sibling promote PR to re-review against, or None.

    Returns `(sibling_review_comment, sibling_reviewed_sha, sibling_pr_number)`
    when the current promote PR overlaps its last-merged, already-reviewed
    sibling by at least PROMOTE_OVERLAP_THRESHOLD. Returns None — keeping the
    caller on a full review — at every gate that doesn't hold: not a promote
    branch, bot identity unknown, no merged sibling, sibling never reviewed or
    missing a Reviewed-at SHA, compare-API failure, or insufficient overlap.
    """
    head_ref = (meta.get("head") or {}).get("ref", "")
    if not head_ref.startswith(PROMOTE_HEAD_PREFIX):
        return None
    if not bot_login:
        print("  [promote] bot identity unknown — skipping fast-path", file=sys.stderr)
        return None

    # `base_ref` is attacker-influenceable (a branch name can contain &, =, %,
    # +, #), so URL-encode it rather than concatenate raw into the query.
    base_ref = (meta.get("base") or {}).get("ref", "")
    url = (
        f"https://api.github.com/repos/{repo}/pulls"
        f"?state=closed&base={quote(base_ref, safe='')}&sort=updated&direction=desc&per_page=100"
    )
    # The list API has no `sort=merged_at`; `sort=updated` is bumped by any late
    # comment/label edit, so it can't be trusted to put the last-merged sibling
    # first. Collect every merged promote candidate, then pick the one with the
    # newest merged_at (ISO-8601 UTC → lexicographic max == chronological max).
    try:
        candidates = [
            c for c in _github_paginate(url, token, max_pages=_PROMOTE_MAX_SIBLING_PAGES)
            if c.get("number") != pr_number
            and c.get("merged_at")
            and ((c.get("head") or {}).get("ref", "")).startswith(PROMOTE_HEAD_PREFIX)
        ]
    except (PartialPageError, RequestException) as e:
        print(f"  [promote] sibling search failed ({e}) — full review", file=sys.stderr)
        return None
    if not candidates:
        print(f"  [promote] {head_ref}: no merged sibling promote — full review", file=sys.stderr)
        return None
    sibling = max(candidates, key=lambda c: c["merged_at"])

    sibling_num = sibling["number"]
    try:
        sib_comments = fetch_issue_comments(repo, sibling_num, token)
    except (PartialPageError, RequestException) as e:
        print(f"  [promote] sibling #{sibling_num} comment fetch failed ({e}) — full review", file=sys.stderr)
        return None
    sib_review = find_prior_review(sib_comments, bot_login)
    if sib_review is None:
        print(f"  [promote] sibling #{sibling_num} has no air review — full review", file=sys.stderr)
        return None
    sib_sha = extract_reviewed_at_sha(sib_review["body"])
    if sib_sha is None:
        print(f"  [promote] sibling #{sibling_num} review has no Reviewed-at SHA — full review", file=sys.stderr)
        return None

    # fetch_inter_diff returns None on a non-OK response, but _gh_request now
    # RAISES RequestException after exhausting retries (timeout / connection).
    # Uncaught, that propagates out of this gate as a bare traceback, breaking
    # the "fall back to full review at every failing gate" contract. Catch it.
    try:
        inter = fetch_inter_diff(repo, sib_sha, head_sha, token)
    except RequestException as e:
        print(f"  [promote] compare {sib_sha[:8]}..{head_sha[:8]} errored ({e}) — full review", file=sys.stderr)
        return None
    if inter is None:
        print(f"  [promote] compare {sib_sha[:8]}..{head_sha[:8]} failed — full review", file=sys.stderr)
        return None
    # fetch_pr_diff sys.exit(1)s on a non-OK response AND _gh_request raises
    # RequestException on retry exhaustion; here either would break this
    # function's "fall back to full review at every failing gate" contract
    # (and a full review can't run without the PR diff either). Catch both and
    # fall back to None so a transient diff-endpoint blip doesn't kill the run
    # before the full-review path gets its own chance to fetch + report.
    try:
        full = fetch_pr_diff(repo, pr_number, token)
    except SystemExit as exc:
        # Catch ONLY fetch_pr_diff's own sys.exit(1) (non-OK response). The
        # SIGTERM handler raises sys.exit(143); that must propagate so a CI
        # job-kill actually stops the process instead of being swallowed here
        # and letting the run continue (and post) after the kill signal.
        if exc.code != 1:
            raise
        print("  [promote] PR diff fetch failed — full review", file=sys.stderr)
        return None
    except RequestException as e:
        print(f"  [promote] PR diff fetch errored ({e}) — full review", file=sys.stderr)
        return None
    # A byte-capped diff undercounts changed lines. Generated-file STUBS are
    # symmetric (same files → same stub decision on both sides, ratio holds),
    # but the CAP binds each diff independently of the other: a capped
    # inter-diff deflates the numerator and INFLATES overlap — the fast-path
    # could fire on promotes that diverged far past the threshold. Either
    # side truncated ⇒ the ratio is meaningless ⇒ full review.
    if _diff_is_truncated(inter) or _diff_is_truncated(full):
        print(
            "  [promote] diff byte-capped — overlap ratio unreliable, full review",
            file=sys.stderr,
        )
        return None
    full_lines = count_diff_changed_lines(full)
    inter_lines = count_diff_changed_lines(inter)
    overlap = 1 - (inter_lines / max(full_lines, 1))
    if overlap < PROMOTE_OVERLAP_THRESHOLD:
        # Clamp the displayed percentage: a rebase/merge-commit-inflated
        # inter-diff can exceed the PR diff, making overlap negative.
        print(
            f"  [promote] sibling #{sibling_num} @ {sib_sha[:8]}: overlap {max(0.0, overlap):.0%} "
            f"(< {PROMOTE_OVERLAP_THRESHOLD:.0%}) — full review",
            file=sys.stderr,
        )
        return None

    print(
        f"  [promote] fast-path: re-review vs sibling #{sibling_num} @ {sib_sha[:8]} "
        f"(overlap {overlap:.0%}, inter {inter_lines}/{full_lines} lines)",
        file=sys.stderr,
    )
    return sib_review, sib_sha, sibling_num


def filter_comments_after(
    comments: list[dict], after_comment_id: int
) -> list[dict]:
    """Slice an already-fetched comment list to those posted after
    `after_comment_id`. Uses the numeric comment id as the cursor rather
    than a timestamp: GitHub's `since` param filters by `updated_at` not
    `created_at`, and timestamps are second-precision so strict `>`
    would drop any comment posted in the same second as the prior
    review.

    Returns the matches in chronological (oldest-first) order regardless
    of input order. `fetch_issue_comments` returns desc-sorted (newest
    first) for partial-fetch resilience; the re-review agent classifies
    findings via developer responses' chronology, so a "fixed" then
    "actually reverted" sequence must arrive in that order — not the
    reverse.
    """
    if after_comment_id <= 0:
        return []
    matches = [c for c in comments if (c.get("id") or 0) > after_comment_id]
    # Comment IDs increase monotonically with creation time; sort ascending
    # to recover chronological order regardless of input ordering.
    matches.sort(key=lambda c: c.get("id") or 0)
    return matches


def format_developer_responses(comments: list[dict]) -> str:
    """Render PR comments as untrusted <developer-comment> blocks."""
    if not comments:
        return ""
    blocks = []
    for c in comments:
        author = html.escape(c.get("user", {}).get("login", "?"))
        body = html.escape((c.get("body") or "")[:4000])
        blocks.append(f'<developer-comment author="{author}">\n{body}\n</developer-comment>')
    return "\n\n".join(blocks)


def _git(repo_dir: str, *args: str, timeout: float = 30.0) -> str:
    """Run a git command in `repo_dir`, return stdout, "" on any failure.

    Pre-computation must never block the review — `git blame` can be slow
    on large files, and the runner's clone may be partial in unexpected
    ways. Catch everything and degrade gracefully so the review still
    runs; agents fall back to live investigation.

    Decoding is explicit utf-8 with errors="replace": `text=True` decodes
    STRICTLY, and `git blame --line-porcelain` echoes raw file CONTENT
    lines — one non-UTF-8 byte in a PR's file (0x90, production
    2026-06-12) raised UnicodeDecodeError inside subprocess.run, a
    ValueError the except below never caught, and the whole review died
    as a bare traceback. Replacement chars may appear in author names
    and raw content lines; the parsed PREFIXES ("author ",
    "author-time ") and the numeric author-time value are always ASCII
    and unaffected, so parsing stays correct either way.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


# Pre-comp caps. Bigger PRs make pre-comp expensive (60-file repo-A runs
# would do 60 git-blame calls); cap to changed-file limit and skip
# anything beyond. Specialists fall back to live blame on overflow files.
PRECOMP_FILE_LIMIT = 40
PRECOMP_BLAME_LINES = 5
PRECOMP_CHURN_MONTHS = 6
PRECOMP_HIGH_CHURN_THRESHOLD = 5

# Match `R100\told\tnew` and `R75\told\tnew` style rename entries in
# `git diff --name-status` output (the digit is similarity %). Captures
# old-path → new-path. Plain A/M/D entries are `A\tpath`.
_NAMESTATUS_RENAME_RE = re.compile(r"^R\d+\t(.+)\t(.+)$")


def compute_file_statuses(repo_dir: str, base_ref: str, head_ref: str) -> tuple[str, list[str]]:
    """Pre-compute file status classification (A/M/D/R) for changed files.

    Returns a tuple of (rendered_text, post_change_paths) — the text is
    multi-line "A: foo.py", "M: bar.py", "D: old.py", "R: from→to" lines
    suitable for inlining in PR Context, and post_change_paths is the
    list of paths that EXIST after the change (used to scope blame +
    churn). Renames keep the new path; deletions are excluded.

    Empty strings on any failure — caller treats that as "skip
    pre-comp". Run with the SHA-based ref pair so this works regardless
    of branch name (re-review base may be a prior SHA, not a branch).
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return "", []
    raw = _git(repo_dir, "diff", "--name-status", f"{base_ref}..{head_ref}")
    if not raw:
        return "", []
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    renamed: list[str] = []
    post_paths: list[str] = []
    for line in raw.strip().splitlines():
        rename_match = _NAMESTATUS_RENAME_RE.match(line)
        if rename_match:
            old, new = rename_match.group(1), rename_match.group(2)
            renamed.append(f"{old} → {new}")
            post_paths.append(new)
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        status, path = parts
        if status == "A":
            added.append(path)
            post_paths.append(path)
        elif status == "M":
            modified.append(path)
            post_paths.append(path)
        elif status == "D":
            deleted.append(path)
        else:
            modified.append(path)
            post_paths.append(path)
    sections = []
    if added:
        sections.append(f"  Added: {', '.join(added)}")
    if modified:
        sections.append(f"  Modified: {', '.join(modified)}")
    if deleted:
        sections.append(f"  Deleted: {', '.join(deleted)}")
    if renamed:
        sections.append(f"  Renamed: {', '.join(renamed)}")
    return "\n".join(sections), post_paths[:PRECOMP_FILE_LIMIT]


# --- UI-copy reviewer dispatch gate ------------------------------------------
# Decides whether the air-ui-copy-reviewer specialist is dispatched for a run.
# It only adds value (and cost) when the diff touches a user-facing surface, so
# backend-only PRs skip it entirely ($0 added). Path/extension allowlist — a
# user-facing-MARKUP extension or an i18n/copy catalog or a user-facing doc.
_UI_EXTENSIONS = (
    ".tsx", ".jsx", ".vue", ".svelte", ".html", ".htm", ".hbs", ".ejs",
    ".erb", ".astro", ".mdx", ".blade.php", ".razor", ".twig", ".liquid", ".njk",
)
# i18n / copy catalogs (user-visible string VALUES live here).
_UI_I18N_RE = re.compile(
    r"(^|/)(locales?|i18n|lang|translations?)/|"
    r"(^|/)(en|messages?)([.-][^/]*)?\.(json|ya?ml|po|pot|arb|strings|resx|ftl)$|"
    r"\.(po|pot|arb|ftl)$",
    re.IGNORECASE,
)
# User-facing help/content markdown. Deliberately does NOT match a bare
# `docs/` dir — `docs/` is overwhelmingly INTERNAL engineering material
# (specs, ADRs, design docs, plans, runbooks; e.g. billing-tool/docs/
# superpowers/plans/*.md), and matching it dispatched the copy reviewer on
# backend PRs that merely included eng docs. NOTE: `.mdx` is separately matched
# as MARKUP via _UI_EXTENSIONS (it implies a rendered doc-site page) regardless
# of directory — so `docs/*.mdx` IS in scope as user-facing markup; only a bare
# `docs/**.md` with no help/content/faq segment falls through. Opt genuinely
# user-facing `.md`-under-`docs/` in via PROJECT-PROFILE `## User-Facing Copy
# Paths`.
_UI_DOC_RE = re.compile(r"(^|/)(help|content|faq)/.*\.mdx?$", re.IGNORECASE)
# Never-trigger: air's own pattern/wiki files and styling-only changes.
_UI_EXCLUDE_RE = re.compile(r"(REVIEW|REVIEW-HISTORY|GLOSSARY|PROJECT-PROFILE|ACCEPTED-PATTERNS|SEVERITY-CALIBRATION)\.md$", re.IGNORECASE)
_DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def _path_is_ui(path: str) -> bool:
    p = path.strip()
    if not p or _UI_EXCLUDE_RE.search(p):
        return False
    low = p.lower()
    if low.endswith(_UI_EXTENSIONS):
        return True
    if _UI_I18N_RE.search(p) or _UI_DOC_RE.search(p):
        return True
    return False


def _path_matches_globs(path: str, globs: tuple | list) -> bool:
    """True if `path` matches any repo-declared copy-module glob. fnmatch
    semantics — `*` is GREEDY across `/`, so `agent-core/agents/*.py` matches at
    any depth (kept intentional so globs stay short; documented in the
    PROJECT-PROFILE `## User-Facing Copy Paths` section). Exclusions still win,
    so a repo can't glob air's own wiki/pattern files into scope."""
    p = path.strip()
    if not p or _UI_EXCLUDE_RE.search(p):
        return False
    return any(fnmatch.fnmatch(p, g) for g in globs)


# PROJECT-PROFILE section header that lists repo-declared user-facing copy
# paths (one `- <glob>` per line) — the opt-in that extends the web-only gate
# to a repo's CLI/TUI copy modules (e.g. repo-C's Python patient/agent copy).
_COPY_PATHS_HEADER_RE = re.compile(r"^#{1,6}\s+User-Facing Copy Paths\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_copy_paths_section(profile_text: str) -> list[str]:
    """Extract the glob list under a `## User-Facing Copy Paths` heading from
    PROJECT-PROFILE.md. Reads `- <glob>` bullet lines until the next heading or
    a blank-line-terminated list. Returns [] if the section is absent."""
    m = _COPY_PATHS_HEADER_RE.search(profile_text or "")
    if not m:
        return []
    globs: list[str] = []
    for line in profile_text[m.end():].splitlines():
        s = line.strip()
        if s.startswith("#"):  # next heading → section ends
            break
        if s.startswith(("- ", "* ")):
            g = s[2:].strip().strip("`").strip()
            if g:
                globs.append(g)
        elif not s and globs:
            break  # blank line after the list ends it
        # else: intro prose before the list (s and not globs) — skip, loop on
    return globs


def _user_facing_copy_globs(store_id: str | None) -> list[str]:
    """Repo-declared copy-module globs from the store's PROJECT-PROFILE.md, or
    []. Store-backed only (the dispatch gate runs pre-session, before any wiki
    clone); legacy-wiki repos fall back to the web-only gate. Fail-safe: any
    miss/error → [] (web-only), never blocks a review on store plumbing."""
    if not store_id:
        return []
    try:
        got = memory_store.read_memory(store_id, memory_store.PROJECT_PROFILE_PATH)
    except Exception as e:  # noqa: BLE001 — never fail a review on a store read
        print(f"  [ui-copy] could not read project-profile from store: {e}", file=sys.stderr)
        return []
    return _parse_copy_paths_section(got[0]) if got else []


def _diff_is_truncated(diff: str) -> bool:
    """Line-start-anchored check for the byte-cap truncation marker.

    Diff body lines always begin with `+`/`-`/space, so PR content cannot
    forge a line starting with the marker (same anchoring as the
    codex-skip guard)."""
    return any(
        line.startswith(DIFF_TRUNCATION_MARKER)
        for line in (diff or "").splitlines()
    )


def _collect_changed_paths(post_paths: list[str], diff: str) -> list[str]:
    """UNION of the pre-computed `post_paths` (capped at PRECOMP_FILE_LIMIT,
    empty without AIR_TARGET_REPO) and the `+++ b/<path>` headers parsed from
    the diff. NOTE: since the diff is hygiene-processed, the header scan is
    bounded by the byte cap — a file in a cap-omitted segment has no header
    here (callers that need cap-safety check `_diff_is_truncated`). The
    header scan is a cheap regex over `+++` lines even on a large diff."""
    return list(post_paths) + _DIFF_PATH_RE.findall(diff or "")


def _diff_touches_ui(post_paths: list[str], diff: str, extra_globs: tuple | list = ()) -> bool:
    """True when the change touches a user-facing surface and the UI-copy
    reviewer should be dispatched.

    UNION of two signals: the pre-computed `post_paths` AND the `+++ b/<path>`
    headers parsed from the diff. Both are consulted because `post_paths` is
    capped at PRECOMP_FILE_LIMIT (and empty without AIR_TARGET_REPO), so a UI
    file sorting past the cap would be invisible to it alone. The diff headers
    cover every segment that SURVIVED the byte cap — on a truncated diff with
    no checkout (`post_paths` empty), omitted segments could hide UI files
    from both signals, so that combination fails open.

    A path is in-scope if it hits the built-in WEB allowlist (`_path_is_ui`:
    markup / i18n / user-facing docs) OR matches a repo-declared copy-module
    glob (`extra_globs`, from PROJECT-PROFILE `## User-Facing Copy Paths` — the
    TUI/`.py` opt-in). CSS/SCSS-only changes and air's own wiki/pattern `.md`
    files never trigger. **Fails open**: if neither signal yields any path at
    all, or the diff is byte-cap-truncated with no precomp paths to fall back
    on, return True so an ambiguous case still gets a copy review (correctness
    over the cost saving on the rare oversized/unparseable diff).
    """
    paths = _collect_changed_paths(post_paths, diff)
    if not paths:
        return True  # fail open — couldn't determine paths
    if not post_paths and _diff_is_truncated(diff):
        return True  # fail open — omitted segments could hide UI files
    return any(_path_is_ui(p) or _path_matches_globs(p, extra_globs) for p in paths)


# Thread-pool width for the per-file precomp git calls. Blame/churn run one
# subprocess per changed file (up to ~80 on big PRs, 15s timeout each) —
# embarrassingly parallel, and `git blame`/`git log` are read-only.
PRECOMP_PARALLELISM = 8


def _map_files(fn, files: list[str]) -> list:
    """Run fn(file) across files on a small thread pool, results in INPUT
    order — so the assembled context block stays byte-identical to the old
    serial loop (ordering is the only thing concurrency could change)."""
    if len(files) <= 1:
        return [fn(f) for f in files]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(PRECOMP_PARALLELISM, len(files))) as pool:
        return list(pool.map(fn, files))


def compute_blame_summaries(repo_dir: str, files: list[str]) -> str:
    """Per-file top-N authors + most-recent commit date.

    Output line shape: `<file>: top: <author1> <N1>, <author2> <N2>; latest: <date>`

    Uses `git blame --line-porcelain HEAD -- <file>` (parallel across
    files, output in input order), parses the `author` + `author-time`
    fields, summarizes per file. Skips files where blame fails (binary,
    deleted, or anything the parser can't handle).
    """
    if not repo_dir or not files:
        return ""
    from collections import Counter

    def one(path: str) -> str | None:
        raw = _git(repo_dir, "blame", "--line-porcelain", "HEAD", "--", path, timeout=15.0)
        if not raw:
            return None
        authors: Counter[str] = Counter()
        latest_ts = 0
        for line in raw.splitlines():
            if line.startswith("author "):
                authors[line[7:].strip()] += 1
            elif line.startswith("author-time "):
                try:
                    ts = int(line[12:].strip())
                    latest_ts = max(latest_ts, ts)
                except ValueError:
                    pass
        if not authors:
            return None
        top = ", ".join(f"{a} {c}" for a, c in authors.most_common(PRECOMP_BLAME_LINES))
        latest = ""
        if latest_ts > 0:
            try:
                from datetime import datetime, timezone
                latest = datetime.fromtimestamp(latest_ts, tz=timezone.utc).strftime("%Y-%m-%d")
            except (OSError, ValueError):
                latest = ""
        if latest:
            return f"  {path}: top: {top}; latest: {latest}"
        return f"  {path}: top: {top}"

    return "\n".join(r for r in _map_files(one, files) if r)


def compute_churn_data(repo_dir: str, files: list[str]) -> str:
    """Per-file commit count over the last N months. Flags high-churn files.

    Output line shape: `<file>: <N> commits in <M> months [HIGH CHURN]?`
    Parallel across files, output in input order.

    High-churn flag fires at PRECOMP_HIGH_CHURN_THRESHOLD or above —
    these files have more surface area for regressions and warrant
    extra attention from the reviewer.
    """
    if not repo_dir or not files:
        return ""
    since = f"{PRECOMP_CHURN_MONTHS} months ago"

    def one(path: str) -> str | None:
        raw = _git(repo_dir, "log", "--oneline", f"--since={since}", "--", path, timeout=15.0)
        if not raw:
            return None
        count = len(raw.strip().splitlines())
        if count == 0:
            return None
        flag = " [HIGH CHURN]" if count >= PRECOMP_HIGH_CHURN_THRESHOLD else ""
        return f"  {path}: {count} commits in {PRECOMP_CHURN_MONTHS} months{flag}"

    return "\n".join(r for r in _map_files(one, files) if r)


def compute_diff_check_warnings(repo_dir: str, base_ref: str, head_ref: str) -> str:
    """Run `git diff --check base..head` to find conflict markers and
    whitespace errors. Returns one line per warning, or "" if clean.

    `git diff --check` exits non-zero when warnings are found; that's
    not a failure for our purposes — the warnings are what we want.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "diff", "--check", f"{base_ref}..{head_ref}"],
            capture_output=True, timeout=30.0,
            # `diff --check` quotes the offending CONTENT lines — same
            # non-UTF-8 exposure as _git's blame (see its docstring).
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        return ""
    return result.stdout.strip()


async def _upload_handoff_files(client, docs: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Upload review-input docs via the Files API for file-handoff mode.

    `docs` maps filename → content; each becomes a read-only `file`
    resource mounted at /workspace/context/<filename>. Returns
    (resources, file_ids) — the caller appends the resources to the
    session create call and deletes the file_ids after the session ends
    (Files API storage is org-shared; these are per-run scratch).

    On partial failure, already-uploaded files are deleted best-effort
    before re-raising so the caller's inline fallback doesn't leak
    orphans.
    """
    resources: list[dict] = []
    file_ids: list[str] = []
    try:
        for name, content in docs.items():
            f = await client.beta.files.upload(
                file=(name, io.BytesIO(content.encode("utf-8")), "text/plain"),
            )
            file_ids.append(f.id)
            resources.append({
                "type": "file",
                "file_id": f.id,
                "mount_path": f"/workspace/context/{name}",
            })
    except Exception:
        for fid in file_ids:
            try:
                await client.beta.files.delete(fid)
            except Exception as cleanup_err:
                print(
                    f"  [warn] file-handoff cleanup failed for {fid}: "
                    f"{type(cleanup_err).__name__}",
                    file=sys.stderr,
                )
        raise
    return resources, file_ids


async def _run_coordinator_session(
    agents, env_id, args, checkout, bot_token, store_id,
    pr_context, diff, codex_block, verifier_task, meta, mode, head_sha,
    ui_in_scope=False,
    verifier_capture: dict | None = None,
) -> tuple[str, str]:
    """Run the multi-agent coordinator session (the default 'full' path).

    One Anthropic session dispatches the 4 core specialists (+ the UI/copy
    reviewer when ui_in_scope) + verifier as
    callable_agents. Returns (output, failure_reason) for the shared
    post-review pipeline. Includes the optional (default-off) file-handoff
    path and the preflight billing-retry contract.
    """
    # File-handoff (EXPERIMENTAL — opt-in via AIR_FILE_HANDOFF=1): the three
    # input docs ride into the session as mounted Files-API resources, and
    # the coordinator user message shrinks to a pointer note. Targets the
    # ~16K output tokens / ~240s the coordinator spends re-emitting the
    # context+diff in TURN 1/2 (repo-C #216 session audit).
    #
    # OFF BY DEFAULT AND EFFECTIVELY DEAD: verified 2026-06-03 (air run
    # 26855698173) that callable-agent threads run in ISOLATED containers,
    # and probe 3 (2026-06-11, probe_multiagent_filemount.py) additionally
    # showed `file` session resources don't materialize at ALL on the
    # current runtime — not in sub-threads, not even in the PRIMARY thread,
    # not on plain sessions. Do not flip this flag. The working successor
    # is AIR_MULTIAGENT's MODE: WORKSPACE-HANDOFF (shared-workspace writes,
    # probes 1-4), which supersedes this path; the code stays only until
    # that migration is validated, then both can be removed together.
    handoff_enabled = os.environ.get("AIR_FILE_HANDOFF", "") in ("1", "true")
    handoff_docs = {
        "pr-context.md": pr_context,
        "pr.diff": diff,
        "verifier-task.md": (
            f"{codex_block}\n\n<verifier-task>\n{verifier_task}\n</verifier-task>"
        ),
    }
    # Dispatch note for file-handoff mode. Scalars only — PR title/body and
    # everything else attacker-influenced stays inside pr-context.md where
    # build_pr_context already escaped and wrapped it. The author login is
    # GitHub-validated ([A-Za-z0-9-]) and safe to interpolate.
    pattern_note = (
        "memory store (read-only at /mnt/memory/ — TURN 3 Part B is SKIPPED)"
        if store_id
        else "legacy wiki at /workspace/wiki"
    )
    # Per-run dispatch gate for the optional UI/copy specialist. The coordinator
    # dispatches the 4 core specialists ALWAYS and air-ui-copy-reviewer ONLY when
    # it appears here — keeps backend-only PRs from paying for a 6th agent.
    ui_scope_line = (
        f"Optional specialists in scope this run: {UI_COPY_AGENT}"
        if ui_in_scope
        else "Optional specialists in scope this run: none"
    )
    handoff_user_text = f"""MODE: FILE-HANDOFF — review inputs are mounted as files, not embedded here.

- PR: #{meta['number']} by {meta['user']['login']} | repo: {args.repo} | review mode: {mode} | HEAD: {head_sha}
- PR context: /workspace/context/pr-context.md
- Diff under review: /workspace/context/pr.diff
- Verifier task + codex findings: /workspace/context/verifier-task.md
- Specialist findings directory: /workspace/findings/
- Pattern source: {pattern_note}
- {ui_scope_line}

Follow your 3-turn protocol in file-handoff mode (see your system prompt). Do not paste file contents into delegations — pass the paths."""

    # Single coordinator session replaces v1.7's 4-specialist asyncio.gather +
    # sequential verifier session (5 sessions → 1). Empirical -49% cost vs the
    # prior 5-session shape on PR #40 fixture (managed/experiments/), same
    # models + same prompts, just architectural change. Anthropic's
    # `callable_agents` runtime fans the 4 specialists out concurrently within
    # the one session — see managed/api.py for the research-preview header.
    coordinator_out = ""
    coordinator_failure_reason = ""
    ma_enabled = _multiagent_enabled()
    coordinator_agent_name = COORDINATOR_MA_AGENT if ma_enabled else COORDINATOR_AGENT
    if ma_enabled and handoff_enabled:
        print(
            "  [warn] AIR_FILE_HANDOFF ignored — AIR_MULTIAGENT supersedes it "
            "(Files-API mounts don't materialize on this runtime; probe 3, 2026-06-11)",
            file=sys.stderr,
        )
        handoff_enabled = False
    try:
        async with AsyncAnthropic() as client:
            file_resources: list[dict] = []
            handoff_ids: list[str] = []
            coordinator_user_text = ""
            if ma_enabled:
                # WORKSPACE-HANDOFF: content embedded once; the coordinator
                # writes it to the SHARED /workspace in TURN 0 and delegates
                # short pointers — replacing the per-delegation re-emission
                # that is full mode's #1 structural cost. git-history stays
                # inline per coordinator.md's carve-out.
                print(f"  multiagent: WORKSPACE-HANDOFF via {coordinator_agent_name} (AIR_MULTIAGENT=1)")
                coordinator_user_text = _workspace_handoff_text(
                    pattern_note, ui_scope_line, pr_context, diff,
                    codex_block, verifier_task,
                )
            if handoff_enabled:
                try:
                    file_resources, handoff_ids = await _upload_handoff_files(
                        client, handoff_docs
                    )
                    coordinator_user_text = handoff_user_text
                    print(f"  file-handoff: {len(handoff_ids)} input files mounted under /workspace/context/ (EXPERIMENTAL)")
                except Exception as e:
                    # Never block a review on handoff plumbing — fall back to
                    # the legacy inline message shape (coordinator.md handles
                    # both).
                    print(
                        f"  [warn] file-handoff upload failed "
                        f"({type(e).__name__}: {e}) — falling back to inline context",
                        file=sys.stderr,
                    )
            if not coordinator_user_text:
                # The MODE header is load-bearing: without it the coordinator
                # defaults to file-handoff delegation (coordinator.md's former
                # "primary" framing) even on inline runs — instructing
                # specialists to read /workspace/context/ + write
                # /workspace/findings/, which aren't mounted / don't propagate
                # across threads here. It then re-delegates inline to recover,
                # burning the very output the dance was meant to save (10 of 12
                # sessions on 2026-06-03 leaked this way).
                coordinator_user_text = (
                    "MODE: INLINE — the full PR context, diff, and verifier "
                    "task are embedded below. Delegate to specialists with this "
                    "inline content; they reply with findings INLINE. Do NOT "
                    "tell any specialist to read /workspace/context/ or write "
                    "/workspace/findings/ — those paths are not mounted on this "
                    "run.\n\n"
                    f"{ui_scope_line}\n\n"
                    f"{pr_context}\n\n"
                    f"<diff>\n{diff}\n</diff>\n\n"
                    f"{codex_block}\n\n"
                    f"<verifier-task>\n{verifier_task}\n</verifier-task>"
                )
            # Direct-post: when run_review passes `verifier_capture`, run_session
            # populates it with the verifier's delivered `## Code Review` body so
            # run_review can post it verbatim instead of the coordinator's relay.
            # None on the default path → run_session captures nothing (byte-identical).
            try:
                # Billing-retry contract lives in _run_session_with_billing_retry.
                coordinator_out = await _run_session_with_billing_retry(
                    lambda: run_session(
                        client,
                        agents[coordinator_agent_name]["id"],
                        agents[coordinator_agent_name]["version"],
                        env_id, args.repo, checkout, bot_token,
                        coordinator_user_text, coordinator_agent_name,
                        store_id=store_id,
                        file_resources=file_resources,
                        capture=verifier_capture,
                        # MA sessions: per-thread accounting that excludes
                        # the primary thread (it idles between turns and
                        # re-runs — a bare counter drifts; see ThreadTracker).
                        multiagent_primary=coordinator_agent_name if ma_enabled else None,
                        # A coordinator run that never opened a sub-agent
                        # thread improvised an unverified solo review (the
                        # 2026-06-11 silent-degradation pair: delegation
                        # denied by toolset; roster dropped by RP-dialect
                        # update). Fail loud instead of posting it.
                        require_dispatch=True,
                        metadata=build_session_metadata(
                            args.repo, args.pr_number, kind="review-coordinator",
                        ),
                    ),
                    "coordinator",
                )
            finally:
                # Per-run scratch — delete after the session ends (any exit
                # path) to keep org Files storage clean. Never before: the
                # mounts belong to the session for its whole lifetime.
                for fid in handoff_ids:
                    try:
                        await client.beta.files.delete(fid)
                    except Exception as cleanup_err:
                        # Best-effort but never silent — a persistent delete
                        # failure means scratch files accumulate in shared
                        # org storage with no other signal.
                        print(
                            f"  [warn] file-handoff cleanup failed for {fid}: "
                            f"{type(cleanup_err).__name__}",
                            file=sys.stderr,
                        )
    except SpecialistSessionError as e:
        # run_session raised because terminated_reason was set and no
        # parts were captured — common cause: `session.error` event
        # carrying BetaManagedAgentsBillingError when ANTHROPIC_API_KEY
        # is out of credits, surfaced here as `e.reason`. Stash the
        # reason so the structured-fallback block can branch on it
        # (billing → actionable "top up the key" message; other → the
        # generic stale-cache message). coordinator_out stays empty so
        # the SHA-extractor falls through to that block normally.
        coordinator_failure_reason = e.reason
        coordinator_out = ""
        print(
            f"  [warn] coordinator session raised SpecialistSessionError: "
            f"{e.reason}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        # Mirror _run_solo_session: the wall-clock cap raises
        # asyncio.TimeoutError, which the billing-retry helper does NOT catch.
        # Uncaught in full mode this surfaced as a bare traceback after a
        # fully billed ~45-min session — no run-failed comment, no ::error::.
        # Coerce to the structured-fallback path instead (run-failed comment +
        # nonzero exit). SIGTERM still propagates: SystemExit is BaseException.
        coordinator_failure_reason = f"{type(e).__name__}: {e}"
        coordinator_out = ""
        print(
            f"  [warn] coordinator session failed: {coordinator_failure_reason}",
            file=sys.stderr,
        )
    return coordinator_out, coordinator_failure_reason


async def _run_solo_session(
    agents, env_id, args, checkout, bot_token, store_id,
    pr_context, diff, codex_block, verifier_task,
) -> tuple[str, str]:
    """Run ONE merged-lens agent (air-solo-reviewer) in a single session.

    The opt-in AIR_REVIEW_MODE=solo|both path: one agent applies all 6 lenses +
    self-verifies + folds Codex findings, emitting the same `## Code Review`
    (incl. the `Reviewed at:` footer the extractor validates). Mirrors the
    coordinator's preflight billing-retry + wall timeout. A single agent spawns
    no callable_agents sub-threads, so run_session breaks on first idle.
    Returns (output, failure_reason) — same shape as the coordinator block.
    """
    solo_user_text = (
        "MODE: SOLO — review this PR yourself, applying EVERY lens in your "
        "system prompt (bugs, design, security, simplification, git-history "
        "risk) and self-verifying (drop false positives / below-60 confidence). "
        "There is no separate verifier pass; the verifier lens applies to your "
        "OWN findings. The full source is mounted read-only at /workspace/repo "
        "— read surrounding files as needed.\n\n"
        f"{pr_context}\n\n"
        f"<diff>\n{diff}\n</diff>\n\n"
        "A separate reviewer (Codex) produced the candidate findings in the "
        "<codex-findings> block below. VERIFY each against the diff and the "
        "mounted source, drop false positives / below-60-confidence, dedup "
        "against your own findings, and FOLD the confirmed ones into your "
        f"`## Code Review`.\n{codex_block}\n\n"
        "The block below is your OUTPUT FORMAT SPEC — use its `## Code Review` "
        "template and rules verbatim (including the `Reviewed at:` footer, which "
        "the orchestrator validates against the head SHA). It was written for a "
        "multi-agent run, so IGNORE any reference in it to 'specialist reviewers' "
        "or reading from `/workspace/findings/` — there are none; you produced "
        "every finding yourself.\n"
        f"<verifier-task>\n{verifier_task}\n</verifier-task>"
    )
    failure_reason = ""
    out = ""
    try:
        async with AsyncAnthropic() as client:
            out = await _run_session_with_billing_retry(
                lambda: run_session(
                    client,
                    agents[SOLO_AGENT]["id"], agents[SOLO_AGENT]["version"],
                    env_id, args.repo, checkout, bot_token,
                    solo_user_text, SOLO_AGENT,
                    store_id=store_id,
                    file_resources=None,
                    metadata=build_session_metadata(
                        args.repo, args.pr_number, kind="review-solo",
                    ),
                ),
                "solo",
            )
    except SpecialistSessionError as e:
        failure_reason = e.reason
        out = ""
        print(
            f"  [warn] solo session raised SpecialistSessionError: {e.reason}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        # Degrade gracefully — a solo failure (notably asyncio.TimeoutError from
        # the wall-clock cap, which the billing-retry helper does NOT catch)
        # must NEVER crash run_review and discard an already-computed coordinator
        # review in `both` mode. Same posture as the codex degradation path.
        failure_reason = f"{type(e).__name__}: {e}"
        out = ""
        print(f"  [warn] solo session failed: {failure_reason}", file=sys.stderr)
    return out, failure_reason


def _unpack_session_result(result, label: str) -> tuple[str, str]:
    """Coerce an `asyncio.gather(return_exceptions=True)` entry to (out, reason).

    A session helper returns `(out, reason)`; a raised exception becomes
    `("", reason)` so `both` mode never crashes and the other session's
    result survives. Both helpers now catch Exception themselves, so this
    is a last line of defense for BaseException-adjacent escapes (e.g.
    CancelledError) rather than the primary TimeoutError handler.
    """
    if isinstance(result, BaseException):
        return "", f"{label} session error: {type(result).__name__}: {result}"
    return result


def _backfill_verdict_if_missing(
    args, head_sha: str, prior: dict, *,
    bot_login: str | None, pr_state: str, pr_author: str, token: str,
) -> None:
    """Repair a missing review verdict for an already-reviewed SHA.

    The post sequence (comment → verdict) is non-transactional: a kill or
    network failure between the two leaves `reviewDecision` stuck at
    REVIEW_REQUIRED, and the early skip gate (`prior_sha == head_sha`)
    used to exit without ever looking again. The posted comment is
    deterministic state — `should_request_changes` recomputes the same
    verdict from its body — so when GitHub shows no bot verdict for this
    SHA, submit it now. Best-effort: any failure leaves the skip path
    exactly as it was (exit 0, no verdict), to be retried on the next
    trigger.

    Two integrity guards (adversarial-review findings):
    - The comment must be UNEDITED (`updated_at == created_at`). The body
      is the verdict source and is collaborator-editable post-hoc — an
      edited body could otherwise mint a fresh APPROVE on an unchanged
      SHA. Session-derived verdicts (the normal path) never re-read the
      comment, so this surface exists only here.
    - A DISMISSED bot verdict for this SHA counts as "present": a human
      dismissing the bot's verdict is a governance action this best-effort
      repair must not override.
    """
    if args.dry_run or pr_state != "open" or not bot_login or bot_login == pr_author:
        return
    prior_body = prior.get("body", "")
    if prior.get("updated_at") and prior.get("updated_at") != prior.get("created_at"):
        print(
            "  [info] verdict backfill skipped — the review comment was edited "
            "after posting, so it is no longer a trusted verdict source",
            file=sys.stderr,
        )
        return
    try:
        for r in fetch_pr_reviews(args.repo, args.pr_number, token):
            if (
                (r.get("user") or {}).get("login") == bot_login
                and r.get("commit_id") == head_sha
                and r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
            ):
                return  # verdict already present (or deliberately dismissed)
        request_changes, reason = should_request_changes(prior_body, floor_exposures=_category_floor_enabled())
        print(
            f"  [backfill] no verdict found for reviewed SHA {head_sha[:8]} — "
            f"submitting {'REQUEST_CHANGES' if request_changes else 'APPROVE'} "
            f"recomputed from the posted review comment"
        )
        if request_changes:
            submit_review_verdict(
                args.repo, args.pr_number, token,
                event="REQUEST_CHANGES",
                body=f"Changes requested — {reason}. See review comment above. "
                     f"(Verdict backfilled — the original run posted the comment "
                     f"but its verdict step did not complete.)",
                commit_id=head_sha,
            )
        else:
            submit_review_verdict(
                args.repo, args.pr_number, token,
                event="APPROVE",
                body="Approved — 0 blockers found. See review comment for "
                     "medium/low/nit findings. (Verdict backfilled — the original "
                     "run posted the comment but its verdict step did not complete.)",
                commit_id=head_sha,
            )
        # Clear any cross-account stale block orphaned by PAT rotation.
        dismiss_stale_air_verdicts(args.repo, args.pr_number, token, bot_login, _air_bot_logins())
    except Exception as e:
        print(
            f"  [warn] verdict backfill failed ({type(e).__name__}: {e}) — "
            f"skip path continues unchanged; next trigger retries",
            file=sys.stderr,
        )


async def run_review(args):
    bot_token = os.environ["AIR_BOT_TOKEN"]

    # Review architecture: --mode > AIR_REVIEW_MODE > "full". Orthogonal to the
    # `mode` scope var (full/re-review) resolved later in this function — the
    # two compose (e.g. solo can run in re-review scope).
    review_arch = args.mode or os.environ.get("AIR_REVIEW_MODE", "").strip() or "full"
    if review_arch not in REVIEW_ARCH_CHOICES:
        print(f"Error: invalid review mode {review_arch!r} (expected one of {REVIEW_ARCH_CHOICES}).", file=sys.stderr)
        sys.exit(1)
    if review_arch != "full":
        print(f"  [mode] review architecture: {review_arch}")

    sync_agents(review_arch)
    agents = list_agents()
    env_id = find_environment()

    required = _required_agents(review_arch)
    missing = [n for n in required if n not in agents]
    if missing or not env_id:
        print(f"Missing agents: {missing}, env={env_id}. Run setup.py first.", file=sys.stderr)
        sys.exit(1)

    # Apply version pins to the session roster. list_agents() returns the
    # LATEST version of each agent; pinned callers (work repos passing
    # agent_versions through managed-review.yml) want sessions created
    # against the blessed version instead. The re-parse here is for the
    # VALUE only — setup.py (run as a subprocess above) already validated
    # the same env var and exited non-zero on malformed input. NOTE:
    # specialist pins are enforced solely by setup.py baking them into the
    # coordinator's callable_agents roster at sync time; only the
    # coordinator entry below is consumed at session-create time
    # (run_session). The loop still pins every named agent so a future
    # direct-session consumer can't silently float.
    for pin_name, pin_ver in parse_agent_pins().items():
        if pin_name not in agents:
            # The `missing` gate above guarantees the required roster, and
            # PINNABLE_AGENTS ⊆ required — reaching this means an archived
            # agent or truncated listing. Floating silently here would be
            # exactly what pinning exists to prevent.
            print(f"Error: pinned agent {pin_name} not in workspace roster.", file=sys.stderr)
            sys.exit(1)
        if agents[pin_name].get("version") != pin_ver:
            print(f"  [pin] {pin_name}: v{agents[pin_name].get('version')} → v{pin_ver}")
            agents[pin_name] = {**agents[pin_name], "version": pin_ver}

    print(f"[2] Fetching PR #{args.pr_number} on {args.repo}...")
    meta = fetch_pr_metadata(args.repo, args.pr_number, bot_token)
    head_sha = meta["head"]["sha"]

    # State gate: refuse to review closed/merged PRs by default. Reachable
    # via manual CLI invocation or `workflow_dispatch` with `closed: false`
    # against a merged PR, OR via `pull_request: synchronize` that races
    # with a merge (commit pushed, review queued, PR merged before the
    # queued run starts executing). --closed opts in for legitimate cases:
    # post-merge audit, wiki-pattern backfill from historical PRs,
    # dogfooding without opening a new PR.
    #
    # Exit code depends on how the refusal was triggered:
    # - `pull_request` event (race-with-merge): exit 0 — the review was
    #   auto-queued and became redundant. Showing red would alert the
    #   operator to a non-failure.
    # - `workflow_dispatch` or local CLI (user intent): exit 1 — the
    #   operator explicitly asked for a review that won't happen; red is
    #   the right signal.
    pr_state = meta.get("state", "open")
    pr_merged = bool(meta.get("merged"))
    if pr_state == "closed" and not args.closed:
        status = "merged" if pr_merged else "closed"
        event = os.environ.get("GITHUB_EVENT_NAME", "")
        if event == "pull_request":
            print(
                f"PR #{args.pr_number} was {status} before the queued review ran. "
                f"Skipping (race with merge — not an error).",
                file=sys.stderr,
            )
            sys.exit(0)
        print(
            f"PR #{args.pr_number} is {status}. Pass --closed to review anyway.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Pick the ref to check out in each Managed Agent session:
    # - Open PRs: branch name — standard flow.
    # - Closed/merged PRs: head SHA via commit-checkout — the head branch is
    #   often deleted on merge. GitHub keeps the PR's head SHA reachable via
    #   refs/pull/<N>/head forever, and Anthropic's github_repository
    #   resource accepts a commit SHA directly.
    if pr_state == "closed":
        checkout = {"type": "commit", "sha": head_sha}
        status = "merged" if pr_merged else "closed"
        print(f"  reviewing {status} PR — checking out head SHA {head_sha[:8]}")
    else:
        checkout = {"type": "branch", "name": meta["head"]["ref"]}

    # Mode detection: RE-REVIEW if a prior bot-authored review comment
    # exists and the head SHA advanced since it. SKIP if the head SHA
    # hasn't moved. Otherwise FULL review.
    #
    # Filtering by bot_login is a blocker-grade check: without it, any PR
    # participant can post a fake `## Code Review` comment to suppress
    # reviews or inject fabricated findings/classifications into the next
    # re-review. Fall back to full review if we can't determine the bot
    # identity — less secure against spoofing but still correct.
    prior = None
    prior_sha = None
    dev_comments: list[dict] = []

    # Fetch all three conversation surfaces unconditionally — fresh
    # reviews also benefit from seeing prior thread context (humans and
    # other AI bots flagged things our agents would otherwise duplicate).
    # When `bot_login` is unresolved, pr_conversation.build_pr_conversation is skipped
    # entirely (see the `if bot_login` gate below) and pr_conv_block
    # becomes "none" — better to lose the block than to emit our own
    # ## Code Review numbering as untrusted-but-unfiltered <conv-comment>s.
    #
    # Run the four sync `requests.get` calls concurrently via the
    # running loop's default executor. Without this, the bash path in
    # commands/review.md (which uses `&` + `wait`) is faster than us;
    # bot identity goes through the same gather so it doesn't block the
    # event loop ahead of the others. Total wall time ≈ slowest one fetch.
    #
    # `return_exceptions=True` because `_github_paginate` only catches
    # HTTP-level failures (`not resp.ok`); raw `requests.ConnectionError`
    # / `Timeout` / `SSLError` propagate. Without this, one transient
    # network blip cancels the gather and aborts run_review entirely —
    # losing the diff fetch + the review post we already have lined up.
    # Same posture as the SPECIALIST_AGENTS gather later in this function.
    loop = asyncio.get_running_loop()
    # ORDER COUPLING: the pre-spend abort below indexes conversation_results[0]
    # as the issue-comments slot (bot_login is stripped first). Reordering this
    # gather without updating that index silently re-points the abort.
    fetch_results = await asyncio.gather(
        loop.run_in_executor(None, fetch_bot_login, bot_token),
        loop.run_in_executor(None, fetch_issue_comments, args.repo, args.pr_number, bot_token),
        loop.run_in_executor(None, fetch_pr_reviews, args.repo, args.pr_number, bot_token),
        loop.run_in_executor(None, fetch_pr_review_comments, args.repo, args.pr_number, bot_token),
        return_exceptions=True,
    )
    bot_login_result, *conversation_results = fetch_results
    if isinstance(bot_login_result, BaseException):
        print(
            f"  [warn] fetch_bot_login failed: {bot_login_result!r} — degrading to None",
            file=sys.stderr,
        )
        bot_login = None
    else:
        bot_login = bot_login_result

    # Identity assertion (opt-in, additive). When the caller passes
    # AIR_EXPECTED_REVIEWER — the GitHub LOGIN of the human requested as
    # reviewer (not the secret stem) — confirm the AIR_BOT_TOKEN actually
    # belongs to that person before spending anything. Catches a wrong PAT
    # pasted into a reviewer's <STEM>_PAT secret, which would otherwise post
    # the review under the wrong identity, silently. Runs before codex and
    # the coordinator session, so a mismatch fails at $0. Empty/unset => no
    # assertion, so legacy single-token and SHA-pinned callers are byte-for-
    # byte unchanged.
    expected_reviewer = os.environ.get("AIR_EXPECTED_REVIEWER", "").strip()
    if expected_reviewer:
        if not bot_login:
            print(
                f"::error::AIR_EXPECTED_REVIEWER={expected_reviewer} is set but the "
                "token owner could not be resolved (GET /user failed) — refusing to "
                "post under an unverified identity.",
                file=sys.stderr,
            )
            sys.exit(1)
        if bot_login.lower() != expected_reviewer.lower():
            print(
                f"::error::token owner '{bot_login}' != requested reviewer "
                f"'{expected_reviewer}' — wrong PAT in the reviewer's secret? "
                "Refusing to post under the wrong identity.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"  [identity] token owner '{bot_login}' matches expected reviewer "
            f"'{expected_reviewer}'",
            file=sys.stderr,
        )

    # Issue comments feed re-review detection AND the early skip gate — a
    # degraded-to-empty list here means "no prior review", which posts a
    # duplicate full review on an unchanged SHA. We're pre-spend, so the
    # cheap correct move is to fail the run and let the next trigger retry
    # (PartialPageError already survived _gh_request's own retries).
    if isinstance(conversation_results[0], BaseException):
        print(
            f"::error::air: issue-comments fetch failed pre-spend "
            f"({conversation_results[0]!r}) — aborting before any session cost; "
            f"a partial comment list would risk a duplicate review.",
            file=sys.stderr,
        )
        sys.exit(1)
    fetch_labels = ("issue comments", "pr reviews", "inline comments")
    coerced: list[list[dict]] = []
    for label, result in zip(fetch_labels, conversation_results):
        if isinstance(result, BaseException):
            print(
                f"  [warn] fetch failed for {label}: {result!r} — degrading to empty",
                file=sys.stderr,
            )
            coerced.append([])
        else:
            coerced.append(result)
    all_comments, pr_reviews_raw, pr_inline_raw = coerced

    # Bot-self filter (and the conversation block as a whole) only makes
    # sense when we know who the bot is. On a transient bot-identity
    # fetch failure, render "none" rather than risk emitting our own
    # numbered findings as untrusted-but-unfiltered <conv-comment>s
    # (which the agents are then told to flag duplicates against).
    if bot_login:
        pr_conv_block = pr_conversation.build_pr_conversation(
            all_comments, pr_reviews_raw, pr_inline_raw, bot_login,
            max_entries=CONVERSATION_MAX_ENTRIES,
        )
    else:
        print(
            "  [warn] bot identity unresolved — rendering empty <pr-conversation>",
            file=sys.stderr,
        )
        pr_conv_block = "none"

    # Re-review detection (only when not --fresh). Reuses the same
    # all_comments fetch above so we don't re-paginate the endpoint.
    if not args.fresh and bot_login:
        prior = find_prior_review(all_comments, bot_login)
        if prior:
            prior_sha = extract_reviewed_at_sha(prior["body"])
            if prior_sha is None:
                print(
                    f"Prior review by {bot_login} found (id={prior['id']}) "
                    f"but no 'Reviewed at:' SHA in body — falling back to full review.",
                    file=sys.stderr,
                )
    elif not args.fresh and not bot_login:
        print(
            "Could not determine bot identity — skipping re-review detection, "
            "running full review.",
            file=sys.stderr,
        )

    if prior and prior_sha == head_sha:
        print(
            f"Already reviewed at {prior_sha[:8]}. No changes since; skipping. "
            f"Pass --fresh to force a full review."
        )
        # A kill between the comment POST and the verdict POST used to lose
        # the verdict for this SHA permanently — this gate refused to look
        # again. The posted comment is deterministic state: recompute the
        # verdict from it and backfill if GitHub has none for this SHA.
        _backfill_verdict_if_missing(
            args, head_sha, prior,
            bot_login=bot_login,
            pr_state=meta.get("state", ""),
            pr_author=(meta.get("user") or {}).get("login", ""),
            token=bot_token,
        )
        return

    mode = "re-review" if (prior and prior_sha) else "full"

    # Promote fast-path (opt-in, AIR_PROMOTE_FASTPATH). A fresh
    # promote/staging-to-main-* PR has no prior review of its own, so it would
    # fall to a full re-read — but it almost entirely overlaps its last-merged
    # sibling promote, which air already reviewed. Re-review against the
    # sibling's reviewed SHA instead. Only when there's no genuine same-PR
    # prior (a real prior always wins — never overridden by a sibling).
    promote_sibling_pr = None
    if prior is None and os.environ.get("AIR_PROMOTE_FASTPATH", "") in ("1", "true"):
        fp = _detect_promote_fastpath(
            args.repo, args.pr_number, meta, head_sha, bot_login, bot_token
        )
        if fp:
            prior, prior_sha, promote_sibling_pr = fp
            mode = "re-review"

    print(f"  mode: {mode}")

    if mode == "re-review":
        # fetch_inter_diff returns None on non-OK, but _gh_request raises
        # RequestException on retry exhaustion — coerce that to None so the
        # block below falls back to full review instead of crashing.
        try:
            inter_diff = fetch_inter_diff(args.repo, prior_sha, head_sha, bot_token)
        except RequestException as e:
            print(f"Inter-diff fetch errored ({e}) — falling back to full review.", file=sys.stderr)
            inter_diff = None
        if inter_diff is None:
            # API error (404 / 5xx / rate limit). We can't tell whether
            # there's code to review, so fall back to full review rather
            # than silently skip.
            print(
                f"Inter-diff fetch failed for {prior_sha[:8]}..{head_sha[:8]} — "
                f"falling back to full review.",
                file=sys.stderr,
            )
            mode = "full"
            # Reverted to full — clear all re-review state so nothing stale
            # escapes (symmetric with the empty-inter-diff fast-path branch).
            promote_sibling_pr = None
            prior, prior_sha = None, None
            diff = fetch_pr_diff(args.repo, args.pr_number, bot_token)
            dev_context = ""
        elif not inter_diff.strip():
            if promote_sibling_pr is not None:
                # Fast-path with an empty inter-diff: this promote's tree
                # already matches the sibling's reviewed tree. Unlike a same-PR
                # re-review (where a review comment already exists on THIS PR),
                # a fast-path PR has no review of its own yet — skipping here
                # would let it merge entirely unreviewed. Fall back to a full
                # review so the PR still gets covered.
                print(
                    f"  [promote] empty inter-diff vs sibling #{promote_sibling_pr} — "
                    f"PR has no review of its own; falling back to full review.",
                    file=sys.stderr,
                )
                mode = "full"
                promote_sibling_pr = None
                prior, prior_sha = None, None
                diff = fetch_pr_diff(args.repo, args.pr_number, bot_token)
                dev_context = ""
            else:
                # Commits landed but the tree is unchanged — empty commits,
                # force-push to the same tree, or merge-only commits that
                # shift parent pointers. The PR already has a review; nothing
                # new to review.
                print(
                    f"No inter-diff between {prior_sha[:8]} and {head_sha[:8]}. Skipping."
                )
                return
        else:
            diff = inter_diff
            if promote_sibling_pr is not None:
                # Promote fast-path: prior["id"] is the SIBLING promote PR's
                # review comment, which is NOT in this PR's all_comments. Its
                # id predates every comment here, so filter_comments_after
                # would return this PR's ENTIRE thread as "developer responses
                # to the prior review" — a context leak. A sibling's review has
                # no genuine dev replies on this PR, so emit none.
                dev_comments = []
                dev_context = ""
            else:
                dev_comments = filter_comments_after(all_comments, prior["id"])
                dev_context = format_developer_responses(dev_comments)
    else:
        diff = fetch_pr_diff(args.repo, args.pr_number, bot_token)
        dev_context = ""

    # PR 7: build the carry-forward ledger ONCE, here, where the inter-diff
    # (`diff` in re-review mode) + the prior body + prior_sha are all resolved.
    # Re-review only; `sibling=True` on the promote fast-path (the prior is a
    # different PR's tree, so line evidence is discarded — number-identity pin
    # only). Kill switch / fresh mode → empty ledger → every consumer no-op.
    carry_forward_ledger = []
    if mode == "re-review" and _ledger_pin_enabled():
        carry_forward_ledger = build_carry_forward_ledger(
            (prior or {}).get("body", ""), diff, prior_sha or "",
            sibling=(promote_sibling_pr is not None),
        )

    # Codex enablement is resolved BEFORE precomp so the codex session can
    # launch as a background task and overlap the per-file blame/churn git
    # calls + store I/O + context build (~1-4 min hidden inside codex's
    # ≤5-min leg). Everything it needs — mode, diff, prior_sha, meta — is
    # already resolved here. See the launch site below (after the promote
    # sibling fetch, which codex's git ops depend on).
    codex_repo = os.environ.get("AIR_TARGET_REPO", "")
    # For re-review mode the base is the prior Reviewed-at SHA (from the
    # review comment body); for full review, it's the PR's base branch SHA
    # (from meta["base"]).
    codex_base_sha = (prior_sha if mode == "re-review" else meta["base"]["sha"]) or ""
    codex_enabled = bool(
        not args.no_codex
        and codex_repo
        and codex_base_sha
        and shutil.which("codex") is not None
        and os.environ.get("OPENAI_API_KEY")
    )
    if codex_enabled:
        tiny = _codex_skip_tiny_delta(mode, diff)
        if tiny is not None:
            print(
                f"  codex: skipped — re-review delta {tiny} lines "
                f"(< {CODEX_RE_REVIEW_MIN_LINES})"
            )
            codex_enabled = False
    codex_task: "asyncio.Task[str] | None" = None
    codex_timer = None
    codex_watchdog_fired: "Callable[[], bool]" = lambda: False
    t_codex = 0.0

    # Client-side pre-computation. Skipped when AIR_TARGET_REPO is unset
    # (e.g. local CLI runs of review.py without the workflow's checkout
    # step). When populated, all four blocks land in the PR Context for
    # every specialist, which means git-history-reviewer (now on Haiku)
    # gets blame/churn pre-summarized and other specialists skip the
    # tool-round-trip cost of re-deriving file statuses themselves.
    target_repo = os.environ.get("AIR_TARGET_REPO", "")
    file_statuses = ""
    blame_summaries = ""
    churn_data = ""
    diff_check_warnings = ""
    post_paths: list[str] = []
    if target_repo and os.path.isdir(target_repo):
        # Promote fast-path: the sibling's reviewed SHA lived on a now-merged
        # (often deleted) promote branch. Under squash/rebase merges it isn't an
        # ancestor of the checked-out head, so the precomp + codex `git … <sha>`
        # calls below would silently return nothing — losing exactly the context
        # this feature reuses. Best-effort fetch the sibling PR head (GitHub
        # retains refs/pull/<n>/head post-merge) so the SHA resolves locally; if
        # it still can't, log so the degradation is visible (the review diff
        # itself uses the GitHub compare API and is unaffected either way).
        if promote_sibling_pr is not None and mode == "re-review":
            if not _git(target_repo, "rev-parse", "--verify", "--quiet", f"{prior_sha}^{{commit}}"):
                _git(target_repo, "fetch", "origin", f"pull/{promote_sibling_pr}/head", timeout=60.0)
                if not _git(target_repo, "rev-parse", "--verify", "--quiet", f"{prior_sha}^{{commit}}"):
                    print(
                        f"  [promote] sibling SHA {prior_sha[:8]} unreachable in local "
                        f"checkout — precomp/codex context degraded (review diff unaffected)",
                        file=sys.stderr,
                    )
        # Launch codex NOW so its ≤5-min leg overlaps the precomp below.
        # Must come after the sibling fetch above (codex's git ops need
        # the prior SHA resolvable). _start_codex_task yields once so the
        # subprocess is actually spawned, and the LONG legs of the window
        # (precomp here, the store lookups below) run in worker threads
        # via asyncio.to_thread so the event loop stays free to drain
        # codex's stdout/stderr pipes — a blocked loop would stall codex
        # once its pipe buffer filled. The remaining on-loop tail
        # (build_pr_context and friends) is millisecond-scale formatting.
        if codex_enabled and codex_task is None:
            codex_task, t_codex, codex_timer, codex_watchdog_fired = await _start_codex_task(codex_repo, codex_base_sha)
        precomp_t0 = time.monotonic()
        precomp_base = prior_sha if mode == "re-review" else f"origin/{meta['base']['ref']}"

        def _precomp_all() -> tuple[str, list[str], str, str, str]:
            statuses, paths = compute_file_statuses(target_repo, precomp_base, head_sha)
            return (
                statuses,
                paths,
                compute_blame_summaries(target_repo, paths),
                compute_churn_data(target_repo, paths),
                compute_diff_check_warnings(target_repo, precomp_base, head_sha),
            )

        try:
            (
                file_statuses, post_paths, blame_summaries, churn_data,
                diff_check_warnings,
            ) = await asyncio.to_thread(_precomp_all)
        except BaseException:
            # Don't orphan a live codex subprocess if precomp dies (incl.
            # CancelledError from SIGTERM): cancelling the task triggers
            # run_codex_session's finally, which kills the process. The
            # seconds-long sync tail after this block is covered by
            # asyncio.run's cancel-pending-tasks shutdown.
            if codex_task is not None and not codex_task.done():
                codex_task.cancel()
            raise
        precomp_secs = time.monotonic() - precomp_t0
        precomp_signals = sum(bool(x) for x in (file_statuses, blame_summaries, churn_data, diff_check_warnings))
        print(f"  pre-computation: {precomp_signals}/4 sections populated in {precomp_secs:.1f}s")

    # Pattern-store rollout flag: a repo with a store has migrated (mount
    # it read-only, write via pattern_writer post-review, counter via the
    # store); a repo without one keeps the wiki path end-to-end. Lookup
    # failures fall back to the wiki — never block a review on store plumbing.
    # Resolved BEFORE the ui-copy gate, which reads the store's PROJECT-PROFILE
    # for repo-declared copy paths. Off-loop via to_thread: this is a
    # paginated network call inside the codex overlap window, and the loop
    # must stay free to drain the codex pipes.
    store_id = await asyncio.to_thread(memory_store.get_store_id, args.repo, flow="review")
    if store_id:
        print(f"  pattern store: {store_id} (wiki mount skipped)")

    # UI-copy reviewer dispatch gate: dispatch the 6th specialist when the diff
    # touches a user-facing surface. Web markup/i18n/docs match the built-in
    # allowlist; a repo can ALSO declare CLI/TUI copy modules in PROJECT-PROFILE
    # `## User-Facing Copy Paths` (store-backed). Read those globs ONLY when the
    # web check misses, so web PRs and store-less repos pay nothing extra.
    # Backend-only PRs skip it ($0 added). Solo/both's merged prompt always
    # includes the UI lens regardless — it self-scopes there.
    changed_paths = _collect_changed_paths(post_paths, diff)  # built once, shared by both checks
    if not changed_paths:
        ui_in_scope, ui_scope_reason = True, "fail-open (no paths)"
    elif any(_path_is_ui(p) for p in changed_paths):
        ui_in_scope, ui_scope_reason = True, "web markup/i18n/docs"
    else:
        ui_in_scope, ui_scope_reason = False, ""
        # Store read only when the web check missed; off-loop (network)
        # so the codex overlap window keeps draining pipes.
        copy_globs = await asyncio.to_thread(_user_facing_copy_globs, store_id)
        if copy_globs and any(_path_matches_globs(p, copy_globs) for p in changed_paths):
            ui_in_scope, ui_scope_reason = True, "declared copy paths"
    print(f"  ui-copy: {f'in scope ({ui_scope_reason})' if ui_in_scope else 'skipped (no user-facing files)'}")

    pr_context = build_pr_context(
        meta, args.repo,
        mode=mode,
        # build_pr_context already ignores prior_review_body when
        # mode != "re-review"; no caller-side guard needed.
        prior_review_body=(prior or {}).get("body", ""),
        prior_sha=prior_sha,
        prior_pr_number=promote_sibling_pr,
        dev_context=dev_context,
        pr_conv_block=pr_conv_block,
        file_statuses=file_statuses,
        blame_summaries=blame_summaries,
        churn_data=churn_data,
        diff_check_warnings=diff_check_warnings,
        store_mounted=bool(store_id),
    )

    print(f"  {meta['title']} | +{meta['additions']}/-{meta['deletions']} | {meta['changed_files']} files")
    if mode == "re-review":
        print(f"  inter-diff: {len(diff.splitlines())} lines (since {prior_sha[:8]})")
        if dev_comments:
            print(f"  developer comments since last review: {len(dev_comments)}")

    # Codex: opt-in 5th specialist, launched as a background task at the
    # top of the precomp block (Pattern B + overlap). Sonnet coordinator
    # with codex inside doesn't parallelize reliably (it serializes bash →
    # specialists, ~13 min wall); Opus coordinator parallelizes but costs
    # ~2.5× the Sonnet equivalent. GHA-side codex → coordinator-user-message
    # keeps clean parallelism for the 4 Claude specialists, and the overlap
    # hides the precomp/context-build minutes inside codex's leg. When the
    # precomp block was skipped (target repo dir missing), launch here —
    # identical to the old sequential behavior.
    codex_findings = ""
    if codex_enabled:
        if codex_task is None:
            codex_task, t_codex, codex_timer, codex_watchdog_fired = await _start_codex_task(codex_repo, codex_base_sha)
        overlapped = time.monotonic() - t_codex
        try:
            # Cap measured FROM LAUNCH: the budget already spent in the
            # overlap window counts against it, so overlapping never
            # extends codex's allowance. The launch-armed loop timer is the
            # ACTIVE enforcer (it fires even while the main coroutine sits
            # in to_thread); this wait_for re-checks the same budget at the
            # await. Either path cancels the task → run_codex_session's
            # finally kills the subprocess.
            codex_findings = await asyncio.wait_for(
                codex_task,
                timeout=max(0.0, SESSION_TIMEOUT_SECS - overlapped),
            )
            print(
                f"  codex complete in {time.monotonic() - t_codex:.1f}s "
                f"(launched {overlapped:.1f}s before this await)"
            )
        except asyncio.TimeoutError:
            print(
                f"  [warn] codex timed out after {SESSION_TIMEOUT_SECS}s — proceeding without it",
                file=sys.stderr,
            )
        except asyncio.CancelledError:
            # Attribution must come from the watchdog's own flag, NOT
            # codex_task.cancelled(): when run_review itself is cancelled
            # (SIGTERM / loop shutdown) while suspended in wait_for, the
            # inner task is ALSO cancelled before the error propagates (on
            # Python ≤3.11 always; timing-dependent on 3.12) — task state
            # cannot distinguish the two, and misreading a shutdown as a
            # codex timeout would swallow the cancellation and keep the
            # run executing past SIGTERM.
            if not codex_watchdog_fired():
                raise
            print(
                f"  [warn] codex hit its {SESSION_TIMEOUT_SECS}s budget during "
                f"the overlap window — proceeding without it",
                file=sys.stderr,
            )
        except SpecialistSessionError as e:
            print(f"  [warn] codex failed: {e.reason} — proceeding without it", file=sys.stderr)
        except Exception as e:
            print(
                f"  [warn] codex error: {type(e).__name__}: {e} — proceeding without it",
                file=sys.stderr,
            )
        finally:
            if codex_timer is not None:
                codex_timer.cancel()

    verifier_task = build_verifier_task(
        mode, args.repo, head_sha, prior_sha, (prior or {}).get("body", ""),
        ledger=carry_forward_ledger,
    )

    # Coordinator inputs: PR Context + diff + codex findings + verifier task.
    # The coordinator dispatches the specialists in parallel via callable_agents
    # in TURN 1, points the verifier at the specialist findings + codex findings
    # + this verifier_task in TURN 2, then outputs the verifier's response
    # verbatim in TURN 3 (see plugins/air/agents/coordinator.md).
    #
    # Codex output is UNTRUSTED — codex's review prompt processes the raw
    # diff, so a prompt-injection payload buried in the diff can shape the
    # codex output text. That text fans out to all 5 sub-agents in this
    # multi-agent path. Match build_pr_context's defense-in-depth: HTML-
    # escape (so an injected `</codex-findings><evil-instruction>...` can't
    # close the wrapper and inject a sibling tag) and cap length to bound
    # blast radius (PRIOR_REVIEW_MAX_CHARS = 8000 chars is the same cap
    # used for prior reviews in re-review mode).
    if codex_findings:
        safe_codex = html.escape(codex_findings)[:PRIOR_REVIEW_MAX_CHARS]
        codex_block = f"<codex-findings>\n{safe_codex}\n</codex-findings>"
    else:
        codex_block = "<codex-findings>(codex unavailable or disabled)</codex-findings>"

    # ===== Review architecture branch (full / solo / both) =====
    # full → multi-agent coordinator (default); solo → one merged-lens agent;
    # both → run both, with the COORDINATOR review as the gating output (drives
    # the verdict + pattern_writer + counter) and the SOLO review posted
    # alongside, labeled and non-gating, for comparison (testing). Every path
    # produces (coordinator_out, coordinator_failure_reason) for the shared
    # post-review pipeline below; `both` additionally keeps (solo_out, ...).
    print(f"\n[4] Running review session(s) [mode: {review_arch}]...")
    t0 = time.monotonic()
    coordinator_out = ""
    coordinator_failure_reason = ""
    solo_out = ""
    solo_failure_reason = ""
    # Direct-post (AIR_POST_VERIFIER_BODY): a dict the coordinator session fills
    # with the verifier's delivered body, so we post it verbatim instead of the
    # coordinator's relay. None when disabled → run_session captures nothing.
    session_capture = {} if _post_verifier_body_enabled() else None

    if review_arch == "both":
        # Run the two independent sessions CONCURRENTLY so wall-clock ≈
        # max(coordinator, solo), not the sum — a sequential layout could hit
        # codex + 45m coordinator + 45m solo and blow the 95-min GHA cap.
        # return_exceptions so a crash in one (e.g. a coordinator
        # asyncio.TimeoutError, which _run_coordinator_session does not catch)
        # can't cancel the other — the surviving review still posts.
        print("  Running coordinator + solo sessions concurrently (both mode)...")
        _coord_res, _solo_res = await asyncio.gather(
            _run_coordinator_session(
                agents, env_id, args, checkout, bot_token, store_id,
                pr_context, diff, codex_block, verifier_task, meta, mode, head_sha,
                ui_in_scope=ui_in_scope,
                verifier_capture=session_capture,
            ),
            _run_solo_session(
                agents, env_id, args, checkout, bot_token, store_id,
                pr_context, diff, codex_block, verifier_task,
            ),
            return_exceptions=True,
        )
        coordinator_out, coordinator_failure_reason = _unpack_session_result(_coord_res, "coordinator")
        solo_out, solo_failure_reason = _unpack_session_result(_solo_res, "solo")
        if not solo_out and solo_failure_reason:
            print(
                f"  [warn] both-mode: solo review unavailable ({solo_failure_reason[:200]}) "
                f"— no comparison comment will be posted",
                file=sys.stderr,
            )
    elif review_arch == "full":
        coordinator_out, coordinator_failure_reason = await _run_coordinator_session(
            agents, env_id, args, checkout, bot_token, store_id,
            pr_context, diff, codex_block, verifier_task, meta, mode, head_sha,
            ui_in_scope=ui_in_scope,
            verifier_capture=session_capture,
        )
    else:  # solo
        print("  Running solo session (single merged-lens agent)...")
        solo_out, solo_failure_reason = await _run_solo_session(
            agents, env_id, args, checkout, bot_token, store_id,
            pr_context, diff, codex_block, verifier_task,
        )
        # Solo IS the review — feed it through the shared post-review pipeline
        # exactly like a coordinator output (extract / post / verdict / learn).
        coordinator_out, coordinator_failure_reason = solo_out, solo_failure_reason

    coordinator_secs = time.monotonic() - t0
    print(f"  Review session(s) complete in {coordinator_secs:.1f}s")

    # Surface wiki-push silent failures. The coordinator's TURN 3 bash
    # has a one-shot rebase-retry on push (see coordinator.md); when both
    # attempts fail, it echoes the AIR_WIKI_PUSH_FAILED token so this
    # detection loop can warn the operator without aborting (the review
    # comment was already posted before the wiki step ran).
    if "AIR_WIKI_PUSH_FAILED" in coordinator_out:
        print(
            "  [warn] coordinator's wiki push failed after rebase retry — "
            "pattern learning will catch up on the next review",
            file=sys.stderr,
        )

    # Direct-post: prefer the VERIFIER's delivered body over the coordinator's
    # relay when enabled and one was captured. It runs through the SAME
    # SHA-validating extractor (same anti-spoof footer check), so a missing or
    # spoofed verifier body fails validation and falls back to the coordinator
    # relay — the feature can only ADD a faithful body, never post an
    # unvalidated one. Only the source string differs; pin/gate/post downstream
    # are unchanged.
    body_source, _direct_status = _select_review_source(
        coordinator_out, session_capture, head_sha, review_arch
    )
    if _direct_status != "off":
        if _direct_status.startswith("direct"):
            print(f"  [direct] {_direct_status} — coordinator relay bypassed")
        else:
            print(f"  [direct] keeping coordinator relay ({_direct_status})", file=sys.stderr)

    # Extract the SHA-validated `## Code Review` body from the session output.
    # See _extract_review_body for the segmentation + anti-spoof rationale.
    # (Shared by full/solo here and by the `both`-mode solo comment below.)
    review_body, review_extracted = _extract_review_body(body_source, head_sha)

    # PR 7: the deterministic guard. ONLY on a successfully-extracted re-review
    # body (a run-failed fallback below has no status block — never pin/
    # resurrect into it). Pins prior severities to max(prior, emitted) on
    # unchanged code, rewrites illegitimate retirements, and resurrects any
    # silently-dropped prior finding — BEFORE posting (2454) and the gate
    # (2487), so the posted comment and the verdict both reflect it.
    if review_extracted and carry_forward_ledger:
        review_body, _pin_log = pin_and_resurrect(review_body, carry_forward_ledger)
        for _line in _pin_log:
            print(f"  {_line}")

    if not review_extracted:
        # Diagnostic dump — log the actual coordinator output so we can
        # see WHY the SHA-validation refused it. repo-D #39 (the
        # fresh-PR retry of #37) hit the same 92.4s failure on a fresh
        # mode (no `prior_review_body`), refuting the regurgitation
        # hypothesis. Without seeing what the coordinator actually
        # emitted, we can't distinguish between (a) content-policy
        # refusal, (b) Anthropic-side throttling/cached error response,
        # (c) session-level swallowed error, (d) some other model
        # behavior. Truncate to 2000 chars to keep CI logs readable;
        # most refusal/error messages are <500 chars, real review
        # bodies start with `## Code Review` and would be caught by the
        # extractor above.
        _coord_preview = coordinator_out[:2000].replace("\n", "\\n")
        print(
            f"  [debug] coordinator_out (first 2000 chars on SHA-mismatch): "
            f"{_coord_preview!r}",
            file=sys.stderr,
        )
        print(
            f"  [debug] coordinator_out total length: {len(coordinator_out)} chars",
            file=sys.stderr,
        )

        # Fallback — no candidate had a head_sha-matching footer. The
        # coordinator either returned no `## Code Review` block at all,
        # or all blocks had wrong-SHA footers (likely causes: verifier
        # sub-agent emitting prior bot review's footer SHA, or some
        # interaction with Anthropic's session caching layer). svc-
        # transcribe PR #37 reproduced this with a 92.5s coordinator
        # (vs typical 1500-2400s), with output near-identical to the
        # prior bot review — making the previous "post raw" path 422
        # against GitHub's near-duplicate detection.
        #
        # New behavior: post a STRUCTURED run-failed comment with the
        # diagnostic context, so the developer sees signal (not silence)
        # and can decide whether to push a small commit to bust the
        # cache or wait for the next push to retrigger. Skip verdict
        # because we have no findings list to gate on.
        #
        # Heading INTENTIONALLY does NOT start with `## Code Review` —
        # that prefix is matched by `startswith("## Code Review")`
        # checks in plugins/air/lib/pr_conversation.py and several CLI
        # bash flows (review.md smart-default, review-respond.md, learn
        # .md). A failure notice with that prefix would be picked up as
        # if it were a real review. Use `## air review (run failed)`
        # so the failure body is unambiguously distinct downstream.
        # Intentionally NO `Reviewed at:` footer either — prevents
        # `find_prior_review` from anchoring on this diagnostic body
        # (belt-and-suspenders; the prefix check already filters).
        coord_secs_str = f"{coordinator_secs:.1f}s"
        run_url = _gha_run_url()
        run_link_line = (
            f"\nRun: <{run_url}> (the job is marked failed with an "
            f"`::error::` annotation carrying this reason)\n"
            if run_url else ""
        )
        # Branch the structured-fallback body on the failure shape so the
        # developer sees an actionable cause + workaround, not generic
        # stale-cache prose:
        #
        # 1. Billing exhausted: `terminated_reason` from `run_session`
        #    contains the Anthropic SDK's `BetaManagedAgentsBillingError`
        #    repr — observed on a real repo-D run when the repo's
        #    `ANTHROPIC_API_KEY` ran out of credits. The error message also
        #    embeds the literal phrase "credit balance is too low".
        # 2. Other coordinator failures (run_session raised for non-billing
        #    reasons): show the reason verbatim so the operator knows what
        #    to look at.
        # 3. Empty output without an exception (the original SSE/REST race
        #    failure mode): generic stale-cache prose.
        _failure_lower = (coordinator_failure_reason or "").lower()
        _is_billing = any(
            hint in _failure_lower for hint in _BILLING_REASON_HINTS
        )
        # Truncate the raw error consistently across branches with a
        # truncation marker only when truncation actually occurred —
        # otherwise readers can't tell whether they're seeing the full
        # error or a tail-cut.
        _raw = coordinator_failure_reason or ""
        _raw_for_post = (
            _raw[:_RAW_REASON_MAX_CHARS] + "…(truncated)"
            if len(_raw) > _RAW_REASON_MAX_CHARS
            else _raw
        )
        if _is_billing:
            review_body = (
                f"## air review (run failed)\n\n"
                f"The bot's coordinator session aborted with an "
                f"**Anthropic billing-related error** — most likely "
                f"cause: credits on the `ANTHROPIC_API_KEY` secret "
                f"used by this repo are exhausted. No verdict will be "
                f"submitted.\n\n"
                f"**Fix:** top up the account at "
                f"<https://console.anthropic.com/> OR rotate the "
                f"`ANTHROPIC_API_KEY` secret to a key with available "
                f"credits:\n"
                f"```\n"
                f"gh secret set ANTHROPIC_API_KEY --repo {args.repo}\n"
                f"```\n"
                f"After topping up / rotating, retrigger with:\n"
                f"```\n"
                f"gh workflow run air-review.yml --repo {args.repo} "
                f"-f pr_number={args.pr_number}\n"
                f"```\n\n"
                f"**Raw error:**\n"
                f"```\n{_raw_for_post}\n```"
                f"{run_link_line}"
            )
        elif coordinator_failure_reason:
            review_body = (
                f"## air review (run failed)\n\n"
                f"The bot's coordinator session aborted with an error. "
                f"No verdict will be submitted.\n\n"
                f"**Reason:**\n"
                f"```\n{_raw_for_post}\n```\n\n"
                f"**Workaround:** check the GHA run log for the full "
                f"context, then push any commit to retrigger. If the "
                f"error recurs, file an issue against "
                f"[VorobiovD/air](https://github.com/VorobiovD/air) "
                f"with the run URL and the reason text above."
                f"{run_link_line}"
            )
        else:
            review_body = (
                f"## air review (run failed)\n\n"
                f"The bot's coordinator session returned without a `## Code "
                f"Review` block whose `Reviewed at:` footer matched the "
                f"current HEAD SHA `{head_sha[:8]}`. No verdict will be "
                f"submitted for this run.\n\n"
                f"**Likely cause:** the coordinator session was unusually "
                f"short ({coord_secs_str}) — typical successful runs take "
                f"1500-2400s. Short runs with unusable output usually "
                f"indicate a cached prior-thread response from Anthropic's "
                f"session layer or a verifier sub-agent that emitted a "
                f"stale footer SHA from a prior round.\n\n"
                f"**Workaround:** push any small commit (whitespace, "
                f"comment, etc.) to invalidate the prefix cache and "
                f"retrigger the review.{run_link_line}"
            )
        print(
            "  [warn] coordinator output had no `## Code Review` block whose "
            f"`Reviewed at:` footer matched head_sha {head_sha[:8]} — posting "
            "structured run-failed comment, verdict will be skipped",
            file=sys.stderr,
        )
        if coordinator_secs < 300:
            # Soft-failure telemetry signal. <300s coordinator on a real
            # PR is impossibly fast (typical: 1500-2400s); paired with
            # unusable output, this is likely a stale-cache signal —
            # framed as "likely" not "almost certainly" because we have
            # 2 production occurrences and no Anthropic-side telemetry
            # to confirm cause. Logged separately so dashboards can
            # correlate frequency over time.
            print(
                f"  [warn] coordinator complete in {coord_secs_str} (typical "
                f"1500-2400s) AND output unusable — likely stale-cache signal",
                file=sys.stderr,
            )

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN — not posting. Review comment below:")
        print("=" * 60 + "\n")
        print(review_body)
        if review_arch == "both" and solo_out:
            _solo_body, _solo_ok = _extract_review_body(solo_out, head_sha)
            print("\n" + "=" * 60)
            print("DRY RUN — solo (experimental) review below:" if _solo_ok
                  else "DRY RUN — solo output (no valid `## Code Review` extracted):")
            print("=" * 60 + "\n")
            print(_solo_body or solo_out[:4000])
        if not review_extracted:
            _exit_nonzero_on_failed_run(args.pr_number, coordinator_failure_reason, posted=False)
        return

    # Pre-post dedup re-check (TOCTOU guard). The early skip gate only saw
    # the comments as of session start; our coordinator session then ran for
    # minutes. A double-trigger — `review_requested` and `synchronize` firing
    # together on one push — can spawn two runs for the same head SHA, and the
    # job-level concurrency group doesn't reliably collapse same-second
    # siblings (both can begin before either is cancelled). Without this check
    # both runs post a full review on the same commit (observed on repo-C
    # #219: two `## Code Review (Re-review)` comments at one SHA, ~30 min
    # apart). Re-fetch now and skip posting if a bot review for THIS head SHA
    # already exists — a concurrent run beat us to it while we were busy.
    # Best-effort: shrinks the duplicate window from minutes to the ms between
    # this check and the POST; never fatal. Honored only when not --fresh (an
    # explicit fresh run is a deliberate re-post and the early gate is skipped
    # for it).
    if not args.fresh and bot_login:
        try:
            recheck_comments = fetch_issue_comments(args.repo, args.pr_number, bot_token)
        except (PartialPageError, RequestException) as e:
            print(
                f"  [warn] pre-post dedup re-check fetch failed ({e}) — "
                f"posting without it (its comment says best-effort, never fatal)",
                file=sys.stderr,
            )
            recheck_comments = []
        concurrent = find_prior_review(recheck_comments, bot_login)
        if concurrent and extract_reviewed_at_sha(concurrent.get("body", "")) == head_sha:
            print(
                f"  [skip] a concurrent run already posted a review for "
                f"{head_sha[:8]} (comment {concurrent.get('html_url') or concurrent['id']}). "
                f"Not stacking a duplicate."
            )
            return

    # both-mode: post the solo review as a SEPARATE, clearly-labeled, non-gating
    # comment for comparison. Re-headered so the body does NOT start with
    # "## Code Review\n" → invisible to the cooldown/dedup/re-review detectors
    # (they anchor on that exact prefix), so it never collides with the gating
    # review. No verdict — only the coordinator review drives the gate. NOT
    # gated on review_extracted: if the coordinator failed but solo succeeded,
    # the solo review is the only useful output and must still be posted —
    # which is also why this runs BEFORE the gating-comment post below (a
    # failed gating POST `sys.exit`s, and the good solo review must survive it).
    if review_arch == "both" and solo_out:
        solo_body, solo_extracted = _extract_review_body(solo_out, head_sha)
        if solo_extracted:
            # Drop solo's own `## Code Review...` header line; the experimental
            # banner becomes the new (dedup-safe) leading header.
            _, _, solo_rest = solo_body.partition("\n")
            solo_comment = (
                "## Code Review (solo — experimental)\n\n"
                "_Single-agent advisory review (`review_mode=both`), posted for "
                "comparison. Not merge-blocking; the gating verdict comes from "
                "the 6-agent review._\n\n"
                f"{solo_rest}"
            )
            try:
                solo_resp = _post_review_comment_with_retry(
                    args.repo, args.pr_number, solo_comment, bot_token
                )
            except RequestException as e:
                print(
                    f"  [warn] solo comparison comment post failed ({e}) — "
                    f"continuing to the gating post",
                    file=sys.stderr,
                )
                solo_resp = None
            if solo_resp is not None and solo_resp.ok:
                print(f"  Posted solo comparison comment: {solo_resp.json()['html_url']}")
            elif solo_resp is not None:
                print(
                    f"  [warn] solo comparison comment post failed: "
                    f"{_github_error_message(solo_resp)}",
                    file=sys.stderr,
                )
        else:
            print(
                "  [warn] both-mode: solo output had no valid `## Code Review` "
                "footer — skipping the solo comparison comment",
                file=sys.stderr,
            )

    print(f"\n[5] Posting review comment to PR #{args.pr_number}...")
    try:
        resp = _post_review_comment_with_retry(args.repo, args.pr_number, review_body, bot_token)
    except RequestException as e:
        print(
            f"::error::air: review comment POST failed after retries ({e}) — "
            f"the review was generated (session paid) but could not be posted. "
            f"Re-run the workflow to repost.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not resp.ok:
        print(f"Error posting comment: {_github_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    print(f"  Posted: {resp.json()['html_url']}")

    # Submit the formal review verdict so `reviewDecision` updates and
    # branch-protection rules see this review. The issue comment above
    # makes the body discoverable for re-review detection but does NOT
    # affect the protection state — that's a separate API call (POST
    # /pulls/{n}/reviews). The CLI plugin (commands/review.md Step 12)
    # has always done both; managed mode used to skip the verdict, so
    # repo-A #595 stayed at REVIEW_REQUIRED with 0 blockers — operator
    # had to approve manually. Skip the verdict only when the bot IS the
    # PR author (GitHub 422s self-review) or the PR is closed/merged
    # (state-gate above already caught --closed=false; if we're here on
    # a closed PR via --closed, also skip — verdicts on closed PRs 422).
    own_pr = bool(bot_login) and bot_login == meta["user"]["login"]
    if not review_extracted:
        print("  [info] coordinator output was malformed (raw fallback) — skipping verdict (would parse template snippets as findings)")
    elif own_pr:
        print(f"  [info] bot is the PR author ({bot_login}) — skipping verdict (GitHub disallows self-review)")
    elif pr_state == "closed":
        print("  [info] PR is closed/merged — skipping verdict (GitHub 422s verdicts on those)")
    else:
        request_changes, reason = should_request_changes(review_body, floor_exposures=_category_floor_enabled())
        # Deterministic conflict-marker gate (CLAUDE.md: "conflict markers =
        # automatic blocker"). Don't trust the model to have emitted the
        # blocker — if `git diff --check` or the diff itself shows an
        # unresolved merge marker, FORCE REQUEST_CHANGES even on an otherwise
        # clean review body. `diff` and `diff_check_warnings` are in scope here.
        if not request_changes and has_conflict_markers(diff, diff_check_warnings):
            request_changes = True
            reason = "unresolved merge conflict marker(s) in the diff"
            print("  [gate] conflict markers detected — forcing REQUEST_CHANGES regardless of model verdict", file=sys.stderr)
        try:
            if request_changes:
                submit_review_verdict(
                    args.repo, args.pr_number, bot_token,
                    event="REQUEST_CHANGES",
                    body=f"Changes requested — {reason}. See review comment above.",
                    commit_id=head_sha,
                )
            else:
                submit_review_verdict(
                    args.repo, args.pr_number, bot_token,
                    event="APPROVE",
                    body="Approved — 0 blockers found. See review comment for medium/low/nit findings.",
                    commit_id=head_sha,
                )
            # Multi-PAT gate-orphan: clear any stale CHANGES_REQUESTED air left
            # under a DIFFERENT bot account (PAT rotation), which GitHub's
            # reviewDecision would otherwise keep gating on despite this verdict.
            dismiss_stale_air_verdicts(
                args.repo, args.pr_number, bot_token, bot_login, _air_bot_logins()
            )
        except RequestException as e:
            # Comment is posted; the verdict is repairable — the skip-gate
            # backfill recomputes it from the comment on the next trigger.
            print(
                f"  [warn] verdict submission errored ({e}) — comment posted; "
                f"the next run on this SHA backfills the verdict",
                file=sys.stderr,
            )

    # Epilogue: bump the shared wiki-backed counter and trigger /air:learn if
    # the threshold fires. All-best-effort — never fail the overall review if
    # any of this has a hiccup. Skipped entirely when the run produced no
    # usable review: there's nothing to learn from, the bump would count a
    # phantom review toward the cadence, and on a billing-dead key the learn
    # session would just spawn into the same wall (2026-05-22 did exactly
    # that — bumped the counter and launched learn after the coordinator
    # died to BetaManagedAgentsBillingError).
    if review_extracted:
        # Store-backed repos: apply the deterministic pattern lifecycle
        # (strengthen matched + advance clean counters) in code — the
        # review session mounted the store read-only, so this is the only
        # write path (replaces coordinator TURN 3 Part B for these repos).
        if store_id:
            try:
                pattern_writer.apply_review_to_store(
                    store_id, meta["user"]["login"], args.pr_number,
                    review_body,
                )
            except Exception as e:
                print(f"  [warn] pattern write failed: {e}", file=sys.stderr)
            # Refresh the git-wiki mirror from the store, THROTTLED (meta.py
            # mirror-due — a cheap meta read most reviews; a git push at most
            # ~1×/hr). Keeps the human/CLI wiki within an hour of the store.
            # Never fail the review; a miss self-heals on the next render.
            try:
                _maybe_render_mirror(args.repo, store_id, bot_token)
            except Exception as e:
                print(f"  [warn] mirror render failed: {e}", file=sys.stderr)
        try:
            _update_learn_counter(args.repo, args.pr_number, bot_token,
                                  store_id=store_id)
        except Exception as e:
            print(f"  [warn] counter update failed: {e}", file=sys.stderr)
    else:
        print(
            "  [skip] learn epilogue + wiki counter skipped — run failed, "
            "nothing to learn from",
            file=sys.stderr,
        )
        _exit_nonzero_on_failed_run(args.pr_number, coordinator_failure_reason, posted=True)


def _run_meta(meta_script: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke plugins/air/lib/meta.py as a subprocess. Shared by the mirror
    throttle check and the learn-counter epilogue (each resolves its own
    meta_script path)."""
    return subprocess.run([sys.executable, str(meta_script), *args],
                          capture_output=True, text=True)


def _maybe_render_mirror(repo: str, store_id: str, bot_token: str) -> None:
    """Throttled deterministic store→wiki mirror render (store-backed repos).

    Checks meta.py `mirror-due` first (one cheap meta read, NO git op); only
    when due (≥ MIRROR_INTERVAL_HOURS since the last render, or never) does it
    render the store + push the wiki + stamp `mirror-rendered`. Best-effort —
    the caller wraps this, and a missed/failed render self-heals on the next
    one (the store is the source of truth). Managed-only; the CLI has no store
    render. The authoritative post-curation render runs in managed/learn.py.
    """
    meta_script = _AIR_LIB_DIR / "meta.py"
    if not meta_script.is_file():
        return

    due = _run_meta(meta_script, "mirror-due", "--store-id", store_id)
    sys.stderr.write(due.stderr)
    if due.returncode != 1:
        return  # within the throttle window (0) or a store error (skip)
    render_store_to_wiki.render_push_and_stamp(store_id, repo, bot_token)


def _update_learn_counter(repo: str, pr_number: int, bot_token: str,
                          store_id: str | None = None) -> None:
    """Bump the shared counter, trigger learn subprocess on threshold.

    Store-backed repos mutate `/meta/air-meta.json` in the memory store
    (sha256-preconditioned — no clone, no push, no rebase-retry). Legacy
    repos keep the wiki clone + commit_meta path. Isolated so callers can
    wrap with a broad try/except.

    Uses subprocess invocations of `plugins/air/lib/meta.py` so CLI and
    managed share one implementation. `managed/review.py` runs alongside
    a checked-out air repo, so the lib path is relative.
    """
    import tempfile

    air_root = _AIR_LIB_DIR.parents[2]
    lib_dir = _AIR_LIB_DIR
    meta_script = _AIR_LIB_DIR / "meta.py"
    if not meta_script.is_file():
        print(f"  [warn] meta.py not found at {meta_script}", file=sys.stderr)
        return

    def _meta(*meta_args: str) -> subprocess.CompletedProcess:
        return _run_meta(meta_script, *meta_args)

    if store_id:
        # Atomic bump + learn-slot claim (replaces the bump+check pair). The
        # single CAS write prevents a busy repo from firing N concurrent learns
        # while the first is still running — exactly one review wins the lock;
        # the rest still count their review but exit 0 (the 2026-06-20 learn-storm
        # cluster fix). Exit 1 = this review claimed → run learn.
        claim = _meta("claim", "--store-id", store_id,
                      "--pr-number", str(pr_number))
        sys.stderr.write(claim.stderr)
        if claim.returncode == 1:
            _run_learn_sync(air_root, repo)
        return

    sys.path.insert(0, str(lib_dir))
    import wiki_git  # type: ignore

    wiki_url = f"https://x-access-token:{bot_token}@github.com/{repo}.wiki.git"
    with tempfile.TemporaryDirectory(prefix="air-wiki-") as tmp:
        wiki_dir = Path(tmp) / "wiki"
        if not wiki_git.clone_wiki(wiki_url, wiki_dir):
            return
        wiki_git.configure_identity(wiki_dir, "air-machine", "air-machine@users.noreply.github.com")

        # 1. Atomic bump + learn-slot claim (replaces the bump+check pair).
        #    Exit 1 == this review claimed the learn slot.
        claim = _meta("claim", "--wiki-dir", str(wiki_dir),
                      "--pr-number", str(pr_number))
        sys.stderr.write(claim.stderr)
        if claim.returncode == 1:
            _run_learn_sync(air_root, repo)

        # 2. Push the meta change (the bump + any lock stamp). learn.py's reset
        #    will push a follow-up commit that clears the lock + zeroes the count.
        wiki_git.commit_meta(wiki_dir, f"meta: bump counter for PR #{pr_number}")


def _run_learn_sync(air_root: Path, repo: str) -> None:
    """Threshold fired — run managed/learn.py SYNCHRONOUSLY in this same
    GitHub Actions job (a detached Popen would get torn down when the
    runner VM stops). learn.py typically takes 3-5 min; the review comment
    has already posted, so we're just extending the CI job's tail.

    learn.py calls `meta.py reset` on success (see
    managed/learn.py::_reset_learn_counter). If it errors, the counter
    stays elevated and the next review retriggers it.

    Output handling: capture and re-emit so the failure mode "learn.py
    exited 1" surfaces an actionable reason (repo-A #635 — diagnostics
    invisible until log archive with direct streaming); stdout streams
    through immediately, stderr dumps only on failure.
    """
    learn_script = air_root / "managed" / "learn.py"
    if not learn_script.is_file():
        print(f"  [warn] learn.py not found at {learn_script}", file=sys.stderr)
        return
    print(f"  [learn] running synchronously: {learn_script} {repo}", file=sys.stderr)
    learn_result = subprocess.run(
        [sys.executable, str(learn_script), repo, "--poll"],
        capture_output=True, text=True,
        # No check=True — we want to finish this review cleanly even if
        # learn errors out.
    )
    sys.stdout.write(learn_result.stdout)
    sys.stdout.flush()
    if learn_result.returncode != 0:
        sys.stderr.write(learn_result.stderr)
        sys.stderr.flush()
        print(
            f"  [warn] learn.py exited {learn_result.returncode} — "
            f"counter not reset (stderr above)",
            file=sys.stderr,
        )


def _billing_preflight() -> None:
    """1-token ping (well under a cent) before any session spawns.

    A dry ANTHROPIC_API_KEY otherwise surfaces mid-coordinator-session
    AFTER real spend — repo-A #969 burned a full partial session over
    28 minutes before dying to the 2026-06-02 exhaustion. With the
    canary, a billing-dead key fails the job red at near-zero cost, and
    retries during a dry spell stay free; after a top-up the canary
    passes and runs proceed with no manual unblocking. Any NON-billing
    canary failure (network blip, model rename, model-access
    restriction) proceeds with a warning — the canary must never block
    a review on its own flakiness. timeout/max_retries mirror
    `_interrupt_live_sessions_sync`'s client so a slow API can't stall
    the job toward the GHA SIGKILL.
    """
    try:
        Anthropic(timeout=10.0, max_retries=0).messages.create(
            model=MODEL_ALIASES["haiku"],
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
    except Exception as e:
        msg = str(e).lower()
        if any(hint in msg for hint in _BILLING_REASON_HINTS):
            print(
                f"::error title=air review failed — billing exhausted (preflight)::"
                f"{str(e)[:300]} | no session was started, nothing spent | "
                f"top up at console.anthropic.com (or rotate ANTHROPIC_API_KEY), "
                f"then re-request the review"
            )
            sys.exit(1)
        print(
            f"  [warn] billing preflight inconclusive ({str(e)[:200]}) — proceeding",
            file=sys.stderr,
        )


def main():
    # CI log streams interleave stdout and stderr; piped stdout is
    # block-buffered, so decision-log prints lag minutes behind stderr —
    # or vanish entirely on truncation/SIGKILL. Line-buffer it so the
    # decision trail ([launch]/mode lines; [promote] already goes to
    # stderr) is real-time.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Trigger an air review for a PR (single multi-agent coordinator)")
    parser.add_argument("repo", help="owner/repo (e.g., myorg/myrepo)")
    parser.add_argument("pr_number", type=int, help="PR number to review")
    parser.add_argument("--dry-run", action="store_true", help="Print the review comment to stdout, don't post to GitHub")
    parser.add_argument("--fresh", action="store_true", help="Force a full review even if a prior review exists (ignore re-review auto-detect)")
    parser.add_argument("--closed", action="store_true", help="Allow review of closed/merged PRs (default: refuse and exit). Useful for post-merge audits or backfilling wiki patterns from historical PRs.")
    parser.add_argument("--no-codex", action="store_true", help="Skip the Codex review pass even if OPENAI_API_KEY + AIR_TARGET_REPO are set. Codex otherwise runs automatically when both are available.")
    parser.add_argument("--mode", choices=REVIEW_ARCH_CHOICES, default=None, help="Review architecture: 'full' (default 6-agent coordinator), 'solo' (one merged-lens agent — ~70%% cheaper/faster, NOT gate-safe), or 'both' (run both; full gates, solo posted alongside for comparison). Falls back to AIR_REVIEW_MODE, then 'full'.")
    args = parser.parse_args()

    if not REPO_ARG_RE.match(args.repo):
        print(f"Error: invalid repo format {args.repo!r} (expected owner/name).", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("AIR_BOT_TOKEN"):
        print("Error: AIR_BOT_TOKEN not set.", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    _install_shutdown_handlers()
    _billing_preflight()
    asyncio.run(run_review(args))


if __name__ == "__main__":
    main()
