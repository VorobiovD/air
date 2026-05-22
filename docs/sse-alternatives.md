# SSE Alternatives — Decision Notes

**Status:** Reference / not-yet-decided. Linked from master roadmap §Phase 6 P10.
**Captured:** 2026-05-22
**Context:** Intermittent SSE-degradation incidents on Anthropic's managed-agents API (2026-05-19, 2026-05-20, 2026-05-21 mornings, partial 2026-05-22 morning) drove the question of what alternatives exist to SSE for waiting on coordinator sessions. This doc captures the option analysis so it isn't lost.

---

## Important framing

**SSE isn't actually the bottleneck during degraded periods.** The events are delayed at Anthropic's *publication* layer — they're not landing in REST polls either until session termination. SSE is just our reading channel. Disabling SSE doesn't speed anything up when the events aren't being produced.

The real question is: given that event delivery is unreliable, what's the cleanest way to wait for sessions and extract output? Three options below.

---

## Option A — Drop SSE entirely, use pure REST polling

**How:** Skip the `/events/stream` connection. From session creation, poll `/events` every 30-60s using a `since` cursor.

**Code change:** Small. The REST poll loop already exists (it's our fallback). Delete the SSE event-handler code (~150 LOC in `managed/review.py`) and run the REST loop from the start.

**Pros:**
- Simpler code (single path instead of SSE-with-REST-fallback)
- Predictable behavior — no "SSE went quiet, switching modes" transition
- Removes ~200 LOC of v1.12.3–v1.12.6 race-handling code

**Cons:**
- **Doesn't actually speed anything up** during degraded periods (same events, same delays)
- Adds ~20-30 API calls per session even when SSE was healthy (lost the free push delivery)
- Slightly worse on healthy days (events arrive when the next poll fires, not in real-time)

**Net:** Modest code-cleanliness win. Zero speed win. Probably not worth shipping for that tradeoff alone.

**Variant (worth considering if Option A is pursued):** adaptive polling cadence — 5s intervals for the first 60s (catches small re-reviews that complete quickly), then back off to 30s. ~50 LOC change. Loses ~200 LOC of SSE handling. Same speed as today on degraded days; slightly more API calls on healthy days; predictable on both.

---

## Option B — Webhooks (master roadmap P10)

**How:** Subscribe to Anthropic's session lifecycle webhooks (`session.status_idled`, `session.thread_idled`, `session.status_terminated`, `session.outcome_evaluation_ended`). Anthropic pushes a notification to your HTTPS endpoint when the session reaches terminal. Your code does one REST `events.list` drain on receipt.

**Code change:** Medium-to-large.
- Need a publicly-reachable HTTPS endpoint (the GHA workflow is ephemeral, no inbound)
- Two viable architectures:
  - **Thin webhook receiver** (Cloudflare Worker / Lambda / Vercel function) writing event payload to S3/KV
  - **Polling-fallback retained** alongside webhooks (more code, not less)
- Receiver writes "session X reached terminal at time Y" to durable storage
- GHA job polls THAT durable storage instead of polling Anthropic

**Pros:**
- **Structural fix** — entire SSE/REST race class goes away
- Webhooks bypass the SSE delivery layer entirely (different code path on Anthropic's side; may not be affected by the same outage)
- One drain call at the end vs. ~30 polls during the session
- Cheaper API calls

**Cons:**
- Requires standing infrastructure (the HTTPS receiver)
- Webhook deliveries can be missed (Anthropic retries ~once on non-2xx, auto-disables after ~20 consecutive failures) — still need a polling fallback as defense-in-depth
- Adds a third moving part to the system (Anthropic + GHA + webhook receiver)

**Net:** The right long-term answer. **Trigger from roadmap:** ship when the next SSE/REST class bug appears, OR when Anthropic deprecates SSE, OR when we want notifications outside the GHA job's lifetime.

---

## Option C — "Sleep and drain" — no event watching at all

**How:** Create the session, send the user message, then `sleep(20 min)`. After timeout, single `GET /events` to drain everything. Done.

**Code change:** Smallest of all. Delete almost all of `managed/review.py`'s session-watching logic.

**Pros:**
- Trivial code (no events, no SSE, no REST polling)
- Predictable — every review takes exactly 20 min, no surprises
- No false-positive cancellations (operator-driven thrash disappears)

**Cons:**
- **Slow on small PRs** — re-reviews that could finish in 5 min now take 20
- **No live progress** — operator sees nothing for 20 min then "Posted!"
- **Wastes wall time on success** — pay for the worst case every time
- Can't detect early failures (session crashes go unnoticed for 20 min)

**Net:** Tempting for its simplicity but bad UX. The "wall time floor" problem makes re-reviews painful.

---

## Current recommendation (2026-05-22)

| Horizon | Action |
|---|---|
| Now | **Don't touch SSE.** Hybrid SSE+REST works; pure REST loses healthy-day responsiveness for zero degraded-day win. |
| Soon | **Ship Phase 0 A2** (informational log when SSE-quiet → REST-poll fires). Solves the operator-perception problem at <10 LOC. |
| Structural | **Webhooks (Option B)** is the right answer. 1-2 week project requiring HTTPS receiver infrastructure. |

## Triggers to re-evaluate

Switch to **Option A** if:
- SSE degradation becomes the rule, not the exception (>20% of reviews hit REST-poll mode)
- We want to simplify the codebase and accept slightly slower healthy-day responsiveness

Switch to **Option B (webhooks)** if:
- SSE/REST race produces a new failure class our current handling can't recover from
- Anthropic announces SSE deprecation
- We want notifications outside the GHA job's lifetime (e.g. async retry, monitoring)
- Infrastructure investment in a webhook receiver becomes justifiable for other reasons (multi-tenant air, dashboard, etc.)

Switch to **Option C** only as an emergency fallback if Anthropic deprecates both SSE and REST event polling.

---

## Related

- Master roadmap §Phase 6 P10 (Webhooks for session lifecycle)
- Master roadmap §Phase 0 A1 (Capture full coordinator_out on SHA-mismatch)
- Master roadmap §Phase 0 A2 (SSE-degraded informational log)
- `feedback_carlos_bot_cache_buster_playbook` (separate failure mode — prefix-cache poisoning, not SSE-related)
