"""Managed-session lifecycle: run_session, REST drains, billing retry,
SIGTERM/atexit interruption of live sessions.

Extracted verbatim from review.py (module split).
"""
import asyncio
import atexit
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from anthropic import (
    Anthropic,
    AuthenticationError, NotFoundError, PermissionDeniedError,
)


# Tracks live session IDs so cleanup handlers can send interrupts to
# anything still running when the driver dies unexpectedly (CI job killed,
# Ctrl-C, uncaught exception). Without this, an orphan session keeps running
# on Anthropic's side until its own idle timeout — burning tokens and
# blocking DELETE /sessions/{id} for ~5 minutes.
#
# Lifecycle rule (enforced by run_session): only remove from this set when
# the session has clearly reached idle on Anthropic's side. Any exception,
# error event, timeout, or unknown-idle stop reason leaves the id tracked
# so the cleanup handlers interrupt it.
LIVE_SESSIONS: set[str] = set()


# Single source for the interrupt event payload — if Anthropic renames the
# event type, there's one string to update.
INTERRUPT_EVENT = {"type": "user.interrupt"}


def _interrupt_live_sessions_sync() -> None:
    """Best-effort sync interrupt of any still-tracked sessions.

    Registered via atexit in main(). Uses the sync Anthropic client because
    atexit runs after the asyncio loop has been torn down.
    """
    if not LIVE_SESSIONS:
        return
    sids = list(LIVE_SESSIONS)
    print(f"  [shutdown] interrupting {len(sids)} live session(s)", file=sys.stderr)
    # Tight per-request timeout + no retries so a slow/unreachable API can't
    # block atexit for minutes while the CI runner's grace window ticks down
    # to SIGKILL. Parallelize via raw daemon threads — ThreadPoolExecutor
    # refuses to schedule work during interpreter shutdown (atexit fires
    # after concurrent.futures' own shutdown hook), so a pool.map() here
    # raises "cannot schedule new futures after interpreter shutdown".
    client = Anthropic(timeout=10.0, max_retries=0)

    def _interrupt_one(sid: str) -> None:
        try:
            client.beta.sessions.events.send(sid, events=[INTERRUPT_EVENT])
            LIVE_SESSIONS.discard(sid)
        except Exception as e:
            print(f"  [shutdown] interrupt failed for {sid}: {e}", file=sys.stderr)

    threads = [threading.Thread(target=_interrupt_one, args=(sid,), daemon=True) for sid in sids]
    for t in threads:
        t.start()
    # Bound total wait to the per-request timeout — slow tails shouldn't
    # starve CI's SIGKILL grace. Each interrupt itself has timeout=10.0, so
    # after ~12s any surviving thread is either making progress or wedged;
    # we give up rather than block shutdown.
    deadline = time.monotonic() + 12.0
    for t in threads:
        remaining = max(0.0, deadline - time.monotonic())
        t.join(timeout=remaining)


def _install_shutdown_handlers() -> None:
    """Register shutdown hygiene. Called from main() — not at import time —
    so test harnesses and other importers don't inherit the SIGTERM handler
    or atexit registration.

    Design:
    - SIGTERM (CI job kill): install a handler that raises SystemExit, which
      lets asyncio cancel pending tasks and run their finally blocks.
      Without a handler, Python's default terminates the process without
      running atexit, leaving every session orphaned.
    - SIGINT (Ctrl-C): do NOT override — Python's default raises
      KeyboardInterrupt which asyncio already converts to CancelledError,
      propagating through `async with stream_cm` cleanup paths.
    - atexit fires after asyncio shuts down and is our last-resort sync
      cleanup for anything that leaked past async teardown.
    """
    atexit.register(_interrupt_live_sessions_sync)

    def _sigterm_to_systemexit(signum, _frame):
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _sigterm_to_systemexit)
    # SIGHUP covers parent-shell death and some container stop sequences.
    # Guarded for Windows (no SIGHUP on that platform).
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _sigterm_to_systemexit)


# Per-session cap so one hung stream can't stall the whole review until the
# GitHub Actions job timeout (default 30 min) kills it. asyncio.wait_for()
# wraps each call; on expiry the coroutine raises TimeoutError. Used for
# codex (subprocess) which usually finishes in ~5 min.
SESSION_TIMEOUT_SECS = 600


# Coordinator runs 4 specialists in parallel + verifier sequentially in
# ONE session. Empirical wall times observed so far:
#   - PR #40 (~3K lines):   ~10 min
#   - PR #41 (5648 lines):  24 min
#   - qai-be #593 (~3.5K):  28 min 13 sec   ← timed out our 1680s cap
# qai-be #593 finished server-side just 13s past the 1680s ceiling, with
# the full review present in the final agent.message — but our Python
# wait_for() had already raised TimeoutError, sending an interrupt that
# crossed the wire as the session was naturally idling. Wall time is
# weakly correlated with diff size; PR shape (file count, language mix,
# re-review classification overhead) drives most of the variance.
#
# Set well above the observed worst case so we don't lose work on a
# near-miss. GHA `timeout-minutes` is bumped in lockstep — the Python
# timeout must remain less than the GHA cap so the script's shutdown
# hook gets a chance to interrupt the live session cleanly before GHA
# SIGKILLs the runner (a SIGKILL leaves the coordinator orphan-running
# and burning tokens until its own server-side idle).
COORDINATOR_TIMEOUT_SECS = 2700


class SpecialistSessionError(Exception):
    """Raised when a specialist session terminates without producing findings."""

    def __init__(self, label: str, reason: str):
        super().__init__(f"{label}: {reason}")
        self.label = label
        self.reason = reason


# Heuristic strings that indicate the coordinator's terminated_reason
# describes an Anthropic billing exhaustion (BetaManagedAgentsBillingError
# or equivalent). Anchored on the strong signals — the SDK class name and
# the stable error-body phrase. The looser bare "billing" was dropped
# during self-review (false-positives on unrelated SDK errors that mention
# "billing system" in passing). Expand only with confirmed production
# strings.
_BILLING_REASON_HINTS: tuple[str, ...] = (
    "betamanagedagentsbillingerror",
    "credit balance is too low",
)


# Transient billing_error retry. Anthropic's "credit balance is too low" is a
# well-documented FALSE-POSITIVE (fires despite funded accounts; clears on
# retry within minutes) and is rejected at PREFLIGHT — sub-second, ~0 tokens
# billed. So re-attempting a FAST billing failure is ~free and usually
# succeeds. Hard guard: only retry when the failed attempt died fast (preflight
# window) — a billing_error that surfaces AFTER the session did real work
# (mid-session: cache-written context / specialist output, e.g. qai-fe
# 2026-06-03 ~9 min in) must NOT be retried, or we re-spend that work. Non-
# billing failures never retry.
BILLING_RETRY_MAX_ATTEMPTS = 3        # 1 initial + 2 retries


BILLING_RETRY_BACKOFF_SECS = 90       # wait between attempts (within "a few minutes")


BILLING_RETRY_PREFLIGHT_SECS = 30     # retry only if the attempt failed faster than this (≈ no tokens burned)


async def _list_events_paged(
    client, session_id: str, *, label: str,
    page_limit: int = 200, max_pages: int = 25,
) -> list:
    """Fetch a session's events via REST, walking cursor pages.

    A full coordinator run can exceed one 200-event page; the pre-pagination
    drain read a single page and could miss the final `agent.message`. Page
    shape is probed defensively (`has_more` / `last_id` / `after_id` are the
    SDK's standard cursor-page surface): if the SDK rejects the cursor kwarg
    we keep the first page — the exact pre-pagination behavior — and say so.
    `max_pages` is a runaway bound, far above any observed session (25 pages
    = 5,000 events).
    """
    events: list = []
    after_id = None
    for _ in range(max_pages):
        kwargs: dict = {"limit": page_limit}
        if after_id is not None:
            kwargs["after_id"] = after_id
        try:
            page = await client.beta.sessions.events.list(session_id, **kwargs)
        except TypeError as e:
            # Only treat this as "SDK lacks cursor kwargs" when the error is
            # actually about the kwarg — a TypeError raised from inside the
            # SDK on a later page must surface, not silently truncate the
            # drain (the exact missed-final-message bug paging exists to fix).
            if after_id is None or "after_id" not in str(e):
                raise
            print(
                f"  [warn] {label}: SDK rejects events cursor kwargs — "
                f"single-page drain only ({len(events)} events)",
                file=sys.stderr,
            )
            break
        page_events = getattr(page, "data", None) or []
        events.extend(page_events)
        if not getattr(page, "has_more", False):
            break
        after_id = getattr(page, "last_id", None)
        if after_id is None:
            print(
                f"  [warn] {label}: events page reports has_more but no "
                f"last_id cursor — stopping drain at {len(events)} events",
                file=sys.stderr,
            )
            break
    return events


class ThreadTracker:
    """Open-sub-agent-thread accounting for the session drain loop.

    Two runtimes, two event vocabularies, two semantics:

    - callable_agents (research-preview, the default coordinator):
      `session.thread_created` opens a thread, `session.thread_idle`
      closes it, threads never re-run. A +/- counter is exact.
    - multiagent roster (GA, `multiagent_primary` set): threads emit
      `session.thread_status_running` / `session.thread_status_idle` /
      `session.thread_status_terminated`, can idle and RE-RUN when the
      coordinator sends a follow-up, and the PRIMARY thread (the
      coordinator itself) emits the same lifecycle — so a bare counter
      drifts. Track per-thread open/closed state keyed by agent name and
      exclude the primary (probed 2026-06-11: see
      probe_multiagent_width.py).
    """

    def __init__(self, multiagent_primary: str | None = None):
        self.primary = multiagent_primary
        self._counter = 0          # legacy callable_agents accounting
        self._open: set[str] = set()  # multiagent per-thread state

    def on_event(self, event_type: str, agent_name: str = "") -> None:
        if self.primary is None:
            if event_type == "session.thread_created":
                self._counter += 1
            elif event_type == "session.thread_idle":
                self._counter = max(0, self._counter - 1)
            return
        if agent_name == self.primary or not agent_name:
            return
        if event_type in ("session.thread_created", "session.thread_status_running"):
            self._open.add(agent_name)
        elif event_type in (
            "session.thread_idle",
            "session.thread_status_idle",
            "session.thread_status_terminated",
        ):
            self._open.discard(agent_name)

    @property
    def open_count(self) -> int:
        return self._counter if self.primary is None else len(self._open)


async def run_session(
    client,
    agent_id: str,
    agent_version: int,
    env_id: str,
    repo: str,
    checkout: dict,
    bot_token: str,
    user_text: str,
    label: str,
    store_id: str | None = None,
    file_resources: list[dict] | None = None,
    multiagent_primary: str | None = None,
) -> str:
    """Create a session, send the user prompt, stream events, return collected agent text.

    Mounts the PR source at /workspace/repo (per the supplied `checkout`
    dict — branch name for open PRs, commit SHA for closed/merged PRs) plus
    the pattern source: the repo's memory store (read-only, /mnt/memory/)
    when `store_id` is set, otherwise the legacy wiki git mount at
    /workspace/wiki. `file_resources` carries the file-handoff mounts
    (PR context / diff / verifier task under /workspace/context/) built
    by _upload_handoff_files. Auth tokens go in the resource config (API
    request body), never in the session transcript or agent message text.
    The wiki resource mounts empty if the repo has no wiki (Managed Agents
    treats a 404 on push-only wikis as an empty mount).
    """
    # try/finally narrows the race window between sessions.create() returning
    # and LIVE_SESSIONS.add() running: if SystemExit (from SIGTERM) fires
    # after `await` resumes but before `LIVE_SESSIONS.add`, finally still
    # runs. It can't eliminate the window (a signal between the `await`
    # resuming and STORE_FAST `session` leaves session=None in finally),
    # but it narrows it to a handful of bytecodes.
    # Pattern source: migrated repos mount the per-repo memory store
    # READ-ONLY (PR content is untrusted — a prompt injection must not be
    # able to poison the pattern store every future review trusts; writes
    # happen post-session in pattern_writer.py). Non-migrated repos keep
    # the legacy wiki git mount.
    resources: list[dict] = [
        {
            "type": "github_repository",
            "url": f"https://github.com/{repo}",
            "authorization_token": bot_token,
            "checkout": checkout,
            "mount_path": "/workspace/repo",
        },
    ]
    if store_id:
        resources.append({
            "type": "memory_store",
            "memory_store_id": store_id,
            "access": "read_only",
            "instructions": (
                "air review patterns (read-only). Per-author pattern files "
                "under authors/<login>.md; shared pattern files at the "
                "root (common-findings.md, service-patterns.md, "
                "accepted-patterns.md, severity-calibration.md, "
                "glossary.md, project-profile.md). Do NOT attempt writes "
                "— pattern updates are applied by the orchestrator after "
                "the review."
            ),
        })
    else:
        resources.append({
            "type": "github_repository",
            "url": f"https://github.com/{repo}.wiki",
            "authorization_token": bot_token,
            "mount_path": "/workspace/wiki",
        })
    if file_resources:
        resources.extend(file_resources)

    session = None
    try:
        session = await client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id, "version": agent_version},
            environment_id=env_id,
            title=f"{label} — {repo}",
            resources=resources,
        )
    finally:
        if session is not None:
            LIVE_SESSIONS.add(session.id)

    print(f"  [launch] {label} → {session.id}")

    await client.beta.sessions.events.send(
        session.id,
        events=[{"type": "user.message", "content": [{"type": "text", "text": user_text}]}],
    )

    # Stop reasons we treat as a clean end-of-turn. Anything else (explicit
    # error, cancelled, unknown future types) is surfaced via
    # SpecialistSessionError so the caller can decide how to fail.
    TERMINAL_SUCCESS = {"end_turn", "stop_sequence", "max_tokens"}

    # Multi-agent sessions emit `session.status_idle stop=end_turn` BETWEEN
    # the coordinator's turns while sub-agents are still running — the
    # coordinator's main turn ends, sub-agent threads continue, then the
    # session goes running again when results arrive. The TRUE terminal
    # is end_turn idle when no sub-agent threads are still open. The first
    # run on PR #41 broke on the first intermediate end_turn idle, returned
    # an empty output, and posted `422 Validation Failed` to GitHub.
    # ThreadTracker owns the per-runtime accounting (callable_agents
    # counter vs multiagent per-thread state — see its docstring).
    threads = ThreadTracker(multiagent_primary)

    parts: list[str] = []
    terminated_reason: str | None = None
    # SSE / REST events backend can lag in EITHER direction relative to the
    # other:
    #
    # 1. SSE delivery delay (qai-be #635, 2026-04 era): REST commits
    #    `session.status_idle` and the trailing final `agent.message` minutes
    #    before our SSE stream consumer receives them.
    # 2. REST delivery delay (qai-be #666, svc-tx #39, 2026-05-05 era): SSE
    #    goes quiet ahead of `events.list` having the final coordinator
    #    `agent.message` on cache-heavy runs that complete in ~SSE_QUIET_S.
    #
    # Both directions converge on the same recovery: when SSE sits quiet
    # past `SSE_QUIET_S`, retry the REST events endpoint with backoff,
    # exiting as soon as a drain attempt produces fresh content or the
    # session reports terminal completion. Dedupe by event id so we
    # don't double-count thread_created/thread_idle on cross-source replay.
    SSE_QUIET_S = 90.0
    # Backoff schedule used inside `_poll_rest_until_done` once the
    # session has reached any terminal state (idle / terminated / error)
    # to give the eventually-consistent REST `events.list` backend time
    # to surface the final `agent.message`. The 0.0 first delay = drain
    # immediately, then 5/10/15s backoff. Total worst-case wait:
    # sum(REST_RETRY_DELAYS) = 30s + REST RTTs.
    REST_RETRY_DELAYS = (0.0, 5.0, 10.0, 15.0)
    # Polling cadence + total budget for the REST fallback when SSE is
    # unavailable (quiet OR closed). The 0.9 multiplier leaves ~10%
    # headroom for the outer asyncio.wait_for(..., COORDINATOR_TIMEOUT_SECS)
    # to fire BEFORE the inner budget exhausts — otherwise we'd silently
    # exit run_session before the wrapper could enforce the wall timeout.
    POLL_INTERVAL_S = 30.0
    POLL_BUDGET_S = COORDINATOR_TIMEOUT_SECS * 0.9
    # Session.status values that indicate the session is finished (no more
    # events will arrive). `idle` is the happy path; `terminated` and
    # `error` cover the documented failure shapes from `_process_event`'s
    # SSE-side handling (lines that handle `session.status_terminated`
    # and `session.error` events). `requires_action` is intermediate and
    # treated as still-running.
    TERMINAL_SESSION_STATUSES = frozenset({"idle", "terminated", "error"})
    seen_event_ids: set[str] = set()

    def _process_event(event) -> str | None:
        """Apply one event to local state. Returns:
          - "break"          terminal success — caller should break the loop
          - "break-error"    terminal error — caller should break, terminated_reason set
          - "continue"       event was a duplicate (already in seen_event_ids); skip
          - None             event was processed successfully; caller continues

        Reads/mutates closure state:
          - `parts` (list)       — appended via `.append()`, no nonlocal needed
          - `seen_event_ids`     — added via `.add()`, no nonlocal needed
          - `threads` (tracker)  — mutated via `.on_event()`, no nonlocal needed
          - `terminated_reason`  — rebound, requires nonlocal
        """
        nonlocal terminated_reason
        eid = getattr(event, "id", None)
        if eid:
            if eid in seen_event_ids:
                return "continue"
            seen_event_ids.add(eid)
        t = getattr(event, "type", "")
        if t == "agent.message":
            for block in getattr(event, "content", None) or []:
                text = getattr(block, "text", None)
                # Multi-agent runtime emits agent.message events with the
                # literal text "[empty message]" (15 chars) between tool-
                # dispatch turns. Skip them.
                if text and text.strip() != "[empty message]":
                    parts.append(text)
            return None
        if t.startswith("session.thread_"):
            threads.on_event(t, getattr(event, "agent_name", "") or "")
            return None
        if t == "session.status_idle":
            stop_reason = getattr(event, "stop_reason", None)
            stop_type = getattr(stop_reason, "type", None) if stop_reason else None
            if stop_type == "requires_action":
                # Transient idle waiting for client-side events; keep going.
                return None
            if stop_type in TERMINAL_SUCCESS:
                if threads.open_count > 0:
                    # Intermediate idle — sub-agents still running.
                    return None
                return "break"
            terminated_reason = f"idle with stop_reason={stop_type!r}"
            return "break-error"
        if t == "session.status_terminated":
            terminated_reason = "session terminated"
            return "break-error"
        if t == "session.error":
            # `session.error` with retry_status='retrying' means Anthropic
            # is recovering server-side; the session is still alive and
            # breaking here would cancel its in-flight retry (PR #41
            # incident). Only abort on truly terminal errors.
            error = getattr(event, "error", None)
            retry_status = getattr(error, "retry_status", None) if error else None
            retry_type = getattr(retry_status, "type", None) if retry_status else None
            if retry_type == "retrying":
                print(
                    f"  [warn] {label}: transient session error (Anthropic retrying): "
                    f"{getattr(error, 'message', '?')}",
                    file=sys.stderr,
                )
                return None
            terminated_reason = f"session error: {error!r}"
            return "break-error"
        return None

    async def _drain_via_rest() -> str | None:
        """Pull session events via the REST endpoint, process any not yet
        seen via SSE. Returns "break" / "break-error" / None.

        Uses the same SDK client to keep auth / beta headers consistent.
        Calls `events.list` if available; otherwise yields None and the
        caller continues waiting on SSE. Walks cursor pages — a full
        coordinator run (6 sub-agent threads + tool calls) can exceed one
        200-event page, and a single-page drain could permanently miss the
        final `agent.message`, converting a successful billed review into
        a false run-failure.
        """
        try:
            events = await _list_events_paged(client, session.id, label=label)
        except AttributeError:
            # Older SDK: no events.list. Fall back to letting SSE keep
            # streaming; we'll just keep paying the latency.
            print(
                f"  [warn] {label}: SDK lacks beta.sessions.events.list; "
                f"can't drain via REST",
                file=sys.stderr,
            )
            return None
        except Exception as e:
            print(
                f"  [warn] {label}: REST events drain failed ({e}); "
                f"continuing with SSE",
                file=sys.stderr,
            )
            return None
        new_count = 0
        for ev in events:
            res = _process_event(ev)
            if res in ("break", "break-error"):
                return res
            # `_process_event` returns "continue" when it short-circuited
            # via the seen_event_ids dedupe; only count newly-processed
            # events (returning None) toward the drain telemetry.
            if res is None:
                new_count += 1
        if new_count:
            print(
                f"  [info] {label}: drained {new_count} unseen event(s) via REST",
                file=sys.stderr,
            )
        return None

    async def _poll_rest_until_done(reason: str) -> str:
        """Fall back to REST polling when SSE is unavailable. Polls
        session.status + drains REST events until the session reaches
        any terminal state, then does an eventually-consistent retry
        drain to pick up final agent.message events.

        Two production failure modes converge here:
        - SSE silent past `SSE_QUIET_S` (PR #61 era)
        - SSE stream closes mid-session via `StopAsyncIteration` while
          sub-agents are still running (PR #62 era — observed with
          5 threads status=running when the stream ended at ~92s)

        Returns one of two sentinels — never returns None:
          - "break"        — session reached `idle` AND we captured
                             agent.message text (happy path); caller
                             continues normally with the captured `parts`.
          - "break-error"  — any failure: poll budget exhausted, session
                             reached non-idle terminal (terminated/error),
                             session reached idle but no agent.message
                             text was captured, or auth/permission/not-
                             found error from the API. Caller must set
                             terminated_reason so LIVE_SESSIONS tracking
                             is correct and SpecialistSessionError is
                             raised by the outer flow.

        The "break-error" guarantee is load-bearing: any silent success
        with empty `parts` would let the orchestrator discard a hung
        session from LIVE_SESSIONS as if the review completed cleanly,
        masking a real failure mode.
        """
        # Skip the initial sleep; check status immediately so a session
        # that closed SSE just because it finished doesn't pay a 30s tax.
        first_iter = True
        print(
            f"  [info] {label}: SSE {reason}; entering REST polling mode",
            file=sys.stderr,
        )
        poll_t0 = time.monotonic()
        while time.monotonic() - poll_t0 < POLL_BUDGET_S:
            if not first_iter:
                await asyncio.sleep(POLL_INTERVAL_S)
            first_iter = False

            # Status check FIRST: if session has reached terminal state we
            # don't need a separate drain — the inner retry loop below
            # picks up everything (including any final agent.message that
            # only just landed). Status check is also the only signal
            # that distinguishes "still running" from "finished".
            try:
                sess = await client.beta.sessions.retrieve(session.id)
            except (AuthenticationError, PermissionDeniedError, NotFoundError) as e:
                # Permanent failures — never recovers via retry. Use
                # typed Anthropic SDK exceptions for precision (the
                # exported hierarchy is stable since SDK v0.20+; matched
                # against `requirements.txt`'s `anthropic>=0.93.0` pin).
                msg = str(e)[:200]
                cls = type(e).__name__
                print(
                    f"  [warn] {label}: REST poll permanent failure "
                    f"({cls}: {msg}); aborting",
                    file=sys.stderr,
                )
                return "break-error"
            except Exception as e:
                # Transient: network blips, 5xx, rate limits — retry
                # after the standard POLL_INTERVAL_S sleep at the next
                # iteration's top.
                msg = str(e)[:200]
                cls = type(e).__name__
                print(
                    f"  [warn] {label}: session retrieve in poll loop "
                    f"failed ({cls}: {msg}); retrying after backoff",
                    file=sys.stderr,
                )
                continue
            sess_status = getattr(sess, "status", None)

            if sess_status in TERMINAL_SESSION_STATUSES:
                # Run the eventually-consistent retry drain to pick up
                # any final agent.message events that haven't propagated
                # to events.list yet.
                drain_res = None
                for _delay in REST_RETRY_DELAYS:
                    if _delay > 0:
                        await asyncio.sleep(_delay)
                    parts_before = len(parts)
                    drain_res = await _drain_via_rest()
                    if drain_res in ("break", "break-error"):
                        break
                    if len(parts) > parts_before:
                        break
                if drain_res == "break-error":
                    return "break-error"
                if sess_status != "idle":
                    # Non-idle terminal (terminated/error/failed/completed):
                    # surface as error so caller raises. Even if we got
                    # SOME agent.message text, the session didn't end
                    # cleanly and the verdict path shouldn't run.
                    print(
                        f"  [warn] {label}: session reached "
                        f"terminal status {sess_status!r}; treating as error",
                        file=sys.stderr,
                    )
                    return "break-error"
                if not parts:
                    # Idle but no captured text — the session ended but
                    # produced nothing usable. Also a failure — the
                    # orchestrator's review-extractor will hit the
                    # structured-fallback path AND we want
                    # terminated_reason set so LIVE_SESSIONS tracking
                    # is correct.
                    print(
                        f"  [warn] {label}: session idle with no "
                        f"agent.message text; treating as error",
                        file=sys.stderr,
                    )
                    return "break-error"
                # Idle + content captured = happy path.
                return "break"

            # Still running. Log liveness, then sleep before next iter.
            elapsed = time.monotonic() - poll_t0
            print(
                f"  [info] {label}: REST poll t={elapsed:.0f}s "
                f"status={sess_status} agent_msgs={len(parts)}",
                file=sys.stderr,
            )

            # Thread-stall visibility (every ~3rd poll): a specialist
            # parked on one long tool call shows as running with a stale
            # updated_at — the ai-relay #216 session lost ~10 min to a
            # silent grep timeout with zero operator signal. Diagnostic
            # only; the prompt-side `timeout 30` guidance attacks the
            # root cause. Best-effort: thread listing failures never
            # disturb the poll loop.
            if int(elapsed) // int(POLL_INTERVAL_S) % 3 == 2:
                try:
                    tpage = await client.beta.sessions.threads.list(session.id)
                    now_utc = datetime.now(timezone.utc)
                    stalls = []
                    for th in getattr(tpage, "data", None) or []:
                        if getattr(th, "status", "") != "running":
                            continue
                        upd = getattr(th, "updated_at", None)
                        if upd is None:
                            continue
                        age = (now_utc - upd).total_seconds()
                        if age > 300:
                            name = getattr(th, "agent_name", None) or th.id[-8:]
                            stalls.append(f"{name} ({age/60:.0f}m)")
                    if stalls:
                        print(
                            f"  [stall] {label}: thread(s) running with no "
                            f"state change >5m: {', '.join(stalls)}",
                            file=sys.stderr,
                        )
                except Exception:
                    pass

        # Budget exhausted — session never reached terminal state. Surface
        # as error so the caller sets terminated_reason.
        print(
            f"  [warn] {label}: REST polling budget exhausted "
            f"({POLL_BUDGET_S:.0f}s) — session never reached terminal state",
            file=sys.stderr,
        )
        return "break-error"

    # AsyncAnthropic's beta.sessions.events.stream is an `async def` that
    # returns an AsyncStream — must await it before using as a context
    # manager (it isn't itself the context manager).
    stream_cm = await client.beta.sessions.events.stream(session.id)
    async with stream_cm as stream:
        stream_iter = stream.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(
                    stream_iter.__anext__(), timeout=SSE_QUIET_S,
                )
            except asyncio.TimeoutError:
                # SSE silent past SSE_QUIET_S. Hand off to the unified
                # REST polling loop — handles both still-running AND
                # already-idle cases. On "break-error" return, set
                # terminated_reason so LIVE_SESSIONS tracking is
                # correct and the orchestrator raises
                # SpecialistSessionError instead of posting an empty
                # body as if it were a clean review.
                drain_res = await _poll_rest_until_done(
                    f"quiet for {SSE_QUIET_S}s"
                )
                if drain_res == "break-error" and terminated_reason is None:
                    terminated_reason = "REST poll fallback failed (SSE quiet)"
                break
            except StopAsyncIteration:
                # SSE stream closed before session reported terminal
                # state — observed on a production run where 5 threads
                # were still status=running while our SSE iterator
                # raised StopAsyncIteration at ~92s. Hand off to REST
                # polling. Same break-error handling as above.
                drain_res = await _poll_rest_until_done("stream closed")
                if drain_res == "break-error" and terminated_reason is None:
                    terminated_reason = "REST poll fallback failed (SSE closed)"
                break
            res = _process_event(event)
            if res in ("break", "break-error"):
                break

    # Only drop from LIVE_SESSIONS on clean success. Error events, unknown
    # idle states, timeouts, cancellations, and any exception during the
    # stream all leave the id tracked so the cleanup handlers can interrupt
    # it. Anthropic docs don't guarantee `session.error` implies server-side
    # termination, and an extra interrupt on an already-idle session is a
    # cheap no-op.
    if terminated_reason is None:
        LIVE_SESSIONS.discard(session.id)

    output = "".join(parts).strip()
    if terminated_reason and not output:
        raise SpecialistSessionError(label, terminated_reason)
    print(f"  [done] {label}")
    return output


async def _run_session_with_billing_retry(make_session_coro, label: str) -> str:
    """Run a managed session under the preflight billing-retry contract.

    `make_session_coro` is a zero-arg callable returning a FRESH `run_session`
    coroutine on each call (retries must re-create the awaitable). Each attempt
    is wrapped in `asyncio.wait_for(COORDINATOR_TIMEOUT_SECS)`. A FAST (preflight,
    ~0-token) billing_error is retried `BILLING_RETRY_MAX_ATTEMPTS` times with
    backoff; a slow / mid-session billing error is NOT retried (it would re-spend
    real work), and non-billing failures propagate immediately. Returns the
    session output; raises `SpecialistSessionError` on exhaustion / non-billing /
    mid-session billing. Shared by `_run_coordinator_session` and
    `_run_solo_session` so the retry contract has one definition.
    """
    for _attempt in range(1, BILLING_RETRY_MAX_ATTEMPTS + 1):
        _attempt_t0 = time.monotonic()
        try:
            return await asyncio.wait_for(make_session_coro(), timeout=COORDINATOR_TIMEOUT_SECS)
        except SpecialistSessionError as _e:
            _elapsed = time.monotonic() - _attempt_t0
            _is_billing = any(h in _e.reason.lower() for h in _BILLING_REASON_HINTS)
            _preflight = _elapsed < BILLING_RETRY_PREFLIGHT_SECS
            if _is_billing and _preflight and _attempt < BILLING_RETRY_MAX_ATTEMPTS:
                print(
                    f"  [retry] {label} billing_error after {_elapsed:.1f}s "
                    f"(preflight, ~0 tokens — likely the transient credit-balance "
                    f"false-positive). Attempt {_attempt}/{BILLING_RETRY_MAX_ATTEMPTS} "
                    f"failed; retrying in {BILLING_RETRY_BACKOFF_SECS}s.",
                    file=sys.stderr,
                )
                await asyncio.sleep(BILLING_RETRY_BACKOFF_SECS)
                continue
            if _is_billing and not _preflight:
                print(
                    f"  [warn] {label} billing_error after {_elapsed:.1f}s — past the "
                    f"preflight window (real work already done); not retrying (would "
                    f"re-spend). Failing loud.",
                    file=sys.stderr,
                )
            raise  # non-billing, slow billing, or attempts exhausted
    # Unreachable: the loop returns on success or raises on the final attempt.
    # Present only so every path satisfies the `-> str` contract.
    raise SpecialistSessionError(label, "billing retry exhausted")  # pragma: no cover
