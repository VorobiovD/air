# air — Improvement Roadmap (post-shipped phases)

_Last updated 2026-05-05 (post-v1.12.5). Phase 4 shipped in v1.12.0; v1.12.1-v1.12.5 closed the SSE/REST race + stream-close + billing-error cascade. Reframed against telemetry from production runs (qai-be PRs #41/#593/#595/#617/#635/#666, qai-fe PRs #239/#246, svc-transcribe #37/#39, plus dogfood runs)._

## What's already shipped

| Phase | Change | Where | Impact |
|---|---|---|---|
| 1 | Multi-agent coordinator (`callable_agents` research-preview) replacing 5 separate sessions | v1.9.0 (PR #45) | -49% cost on large PRs |
| 2 | Pre-computation of blame/churn/file-status/diff-check on the runner | v1.11.0 (PR #46) | -33% wall time on Laravel cross-repo bench |
| 2 | Verifier on Sonnet (was Opus); git-history-reviewer on Haiku (was Sonnet) | v1.11.0 (PR #46) | **3× faster verifier** — qai-fe PR #239 dropped 39 min → 13 min on same diff |
| 3 | Severity-aware verdict gate + DEFERRED status | v1.12.0 (PR #49) | Lows/nits don't block; defense-in-depth rejects `[blocker] — DEFERRED` |
| 3 | SSE delivery latency mitigation — REST events fallback at 90s quiet timeout | v1.12.0 (PR #49) | Caps stuck-stream tail latency |
| 3 | Extractor narration anchor (`(?<!\`)## Code Review`) | v1.12.0 (PR #49) | Fixes qai-be #635 narration leak |
| 3 | learn.py stderr capture | v1.12.0 (PR #49) | qai-be #635 debugging gap |
| 3 | `air_ref` input parameter for cross-repo benchmarking | v1.11.0 (PR #46) | Lets a feature branch run against real consumer PRs without touching their main config |
| 4 | Re-review gate narrowed to blocker-only (mediums = warnings) | v1.12.0 (PR #51) | svc-transcribe #37 — would have flipped all 13 CHANGES_REQUESTED re-review rounds to APPROVED |
| 4 | Carry-forward suppression — auto-DEFER 2nd consecutive NOT FIXED on non-blockers (managed mode only) | v1.12.0 (PR #51) | Eliminates the perpetual-loop pattern (svc-transcribe #37 finding #2: 13 NOT FIXED rounds in a row) |
| 4 | Workflow concurrency — coalesce rapid-fire pushes per PR (`cancel-in-progress: true`) | v1.12.0 (PR #51) | Prevents overlapping reviews; the latest push runs to completion |
| 4 | Legacy missing-severity default flipped to `blocker` (conservative-gating) | v1.12.0 (PR #51) | Pre-v1.12 prior bodies (no `[severity]` tags) keep gating instead of silently un-gating |
| 5 | Structured `## air review (run failed)` fallback comment + 422 retry on review post | v1.12.1 (PR #54) | Replaces 422 cascade with actionable signal; sanitizes response-body diagnostics |
| 5 | Debug log of `coordinator_out[:2000]` on SHA-mismatch | v1.12.2 (PR #57) | Diagnostic instrumentation that confirmed the SSE/REST race hypothesis |
| 5 | SSE/REST race fix: retry drain on eventually-consistent events (per-attempt delta tracking) | v1.12.3 (PR #61) | qai-be #635-style failures (cached coordinator finishes in ~92s, REST events lag) recover |
| 5 | REST polling until session terminal — handles SSE stream-close mid-session | v1.12.4 (PR #62) | qai-be #666 went from 92s empty-output to 1432s + real review (validated in production) |
| 5 | Billing-aware structured-fallback: detects `BetaManagedAgentsBillingError`, posts top-up snippet | v1.12.5 (PR #64) | svc-transcribe billing exhaustion now surfaces as actionable comment instead of stack trace |

The 5-session → 1-coordinator + Haiku/Sonnet tiering combination has bought us most of the **cost** win projected in `cost-optimization-plan.md`. Empirical numbers (next section) show the **latency and reliability** bottlenecks have moved.

## What recent runs actually show

### Wall time (10 successful runs, May 1-2 2026)

| Repo | Avg | Range | Notes |
|---|---|---|---|
| qai-be | 31.7 min | 24.6 – 42.3 min | All on PR #635 (5-collection mongo import, 1380-line diff) |
| qai-fe | 18.9 min | 13.0 – 26.3 min | Spread of PR sizes |
| qai-fe with Opus verifier (pre-tier) | 39.4 min | n=1 | **Same PR diff, Sonnet ran 3× faster** |

### Coordinator session decomposition

```
codex (GHA-side)          ~35-45s     bounded; rarely the bottleneck
setup.py + checkout       ~5-15s      flat overhead
coordinator session       18-40 min   ⟵ dominant
  ├── 4 specialists in parallel (~slowest stragger ~5-15 min)
  ├── verifier (sonnet)   ~3-8 min
  ├── wiki bash update    ~10-30s
post review + verdict     ~2-5s
learn.py epilogue (5/PR)  ~3-10 min   only every 5 reviews
```

### Re-review density is the new norm

PR #635 received **9 reviews over 2 days**. Comment sizes converged but not monotonically: 8.2KB → 6.7KB → 4.1KB → 6.0KB → 9.5KB → 5.7KB → 6.3KB → 4.6KB → 2.4KB. Specialists flag *different* things on similar diffs — variance, not just convergence.

### Failure profile (last 50 runs, updated 2026-05-05)

| Mode | Count | Status |
|---|---|---|
| Successful | 30+ | normal |
| `Skipped` (own-PR, closed-PR, race-with-merge) | many | working as intended |
| Cancelled (race-with-push) | ~5 | mitigated via `commit_id` pinning + supersede check; v1.12.0 added `cancel-in-progress: true` so this is now expected (not a failure) |
| **`422 Validation Failed` posting comment** | 2 | **no retry — single point of failure (see P1)** |
| **Stale-coordinator-output cascade** (svc-transcribe #37 runs 25367689850, 25368789413, 25369351035) | 3 | **REPRODUCIBLE on long re-review chains** — coordinator returns in 92.4-92.5s (vs typical 1500-2400s) with `Reviewed at:` footer pointing at the PRIOR head SHA. Orchestrator's SHA-validation refuses the verdict; pre-v1.12.1 raw-post fallback 422'd against GitHub's near-duplicate detection. v1.12.1 ships the structured `## air review (run failed)` fallback as the orchestrator-side defense; the underlying coordinator-side root cause is unaddressed (see "Coordinator regurgitation hypothesis" below). **Cache-bust workaround (whitespace commit on README) DID NOT recover** — disproves the prefix-cache theory. **PR-restart workaround DID recover** — closing the failing PR and reopening identical branch as a fresh PR avoids the re-review codepath entirely (no `prior_review_body` → coordinator must dispatch real specialist work). |
| Pre-PR-#46 stuck runs (45+ min) | 0 in May | gone after pre-comp + tier swap |

### Coordinator regurgitation hypothesis (root cause for stale-coordinator failure)

After three reproductions on svc-transcribe #37 (15+ re-review rounds on the same PR) and a failed cache-bust attempt, the strongest remaining hypothesis for the stale-coordinator failure mode:

**The coordinator model is regurgitating `prior_review_body` from its user-message context instead of dispatching specialists.** The 92.5s wall time is enough for ~one model turn — not the 3-turn protocol the coordinator is supposed to follow. For re-review mode, `build_pr_context` inlines the full prior bot review (with its `Reviewed at: <prior-sha>` footer) so specialists can do FIXED/NOT FIXED classification. On long re-review chains the prior body is heavily PR-specific; the coordinator likely "recognizes" it and short-circuits TURN 1's parallel dispatch, emitting a TURN 3 that copies the prior body — including the prior SHA in the footer.

Evidence supporting the hypothesis:
- Coordinator wall time on failures: 92.4s, 92.5s, 92.5s (highly consistent, ~one model turn)
- Failures are PR-specific: only svc-transcribe #37 hit it; PRs with shorter re-review chains haven't reproduced
- The `Reviewed at:` SHA in the broken outputs always matches the most recent prior bot review's SHA (pattern: most-recent prior body copied)
- Cache-bust commits (whitespace changes to README) DID NOT recover — eliminates Anthropic prefix-cache as the primary cause
- Closing the failing PR and reopening from the same branch (which removes the re-review codepath entirely) DID recover

This is a **model behavior issue**, not a caching issue. The fix is on the prompt side and/or the orchestrator side. See P0 below.

### Hidden observability gap

`managed/review.py` prints phase markers (`[1] Syncing... [2] Fetching... [3] codex... [4] coordinator...`) to a buffered stdout that only flushes when the script exits. The GHA log shows all of these timestamped at **the same instant** — script-exit time, ~30 min after they printed. If a run hangs at minute 35, the live log shows nothing past `[1] Syncing...` until the watchdog kills it. This is currently working around itself by luck.

---

## Reframed improvement priorities (Phase 5 candidates)

Sorted by **value × evidence × cost-to-ship**. Each has a concrete trigger. Phase 4 just shipped in v1.12.0; the remaining priorities are unchanged in shape but their relative ranking shifted now that the verdict-gate problem is closed.

**Recommended next ship:** P0-NEW (coordinator regurgitation diagnostic + retry) is now the highest priority — it's the only item with active production impact. P0 (progress flush), P1 (post-failure recovery — partially shipped in v1.12.1), and P7 (wiki epilogue dispatcher) form a 2-3 day bundle as a follow-up.

---

### P0-NEW — Coordinator regurgitation diagnostic + retry ⟵ **next**

**Problem:** confirmed reproducible on svc-transcribe #37 across three runs. Coordinator returns in ~92.5s (vs typical 1500-2400s) with output whose `Reviewed at:` footer matches the prior bot review's SHA, not the current head. v1.12.1 ships orchestrator-side defense (structured run-failed comment) but doesn't address the model behavior. The user's escape hatch is to close the failing PR and reopen the same branch as a fresh PR — which works but is a manual workaround, not a fix.

**Two-step fix:**

1. **Debug logging step (ship first, ~10 LOC).** When the SHA-mismatch fallback fires, log the first 1000 chars of `coordinator_out` to stderr so the next failure gives us the actual coordinator output. Confirms or refutes the regurgitation hypothesis before we spend effort on the larger fix:
   ```python
   if not review_extracted:
       print(
           f"  [debug] coordinator_out (first 1000 chars on SHA-mismatch): "
           f"{coordinator_out[:1000]!r}",
           file=sys.stderr,
       )
       # ...existing structured fallback...
   ```
   Trivial, zero-risk, ships as a hotfix in v1.12.2.

2. **Detection + retry mechanic (ship after step 1 confirms).** If the first coordinator session returns in <300s AND the output's footer SHA matches `prior_sha` (i.e., regurgitation pattern confirmed), automatically retry the coordinator session WITHOUT `prior_review_body` in the user message — degrading to a fresh-review codepath for that retry. One retry, structured logging, costs ~1 extra coordinator session in the failure case (free in the happy path).

   Tradeoff on retry path: the fresh-review fallback loses re-review's prior-finding classification (FIXED / NOT FIXED / PARTIALLY FIXED / DEFERRED). Acceptable — a fresh-review-style finding list with the current diff is strictly better than a stale-SHA copy of the prior round.

**Alternative considered (and rejected):**

- **Strengthen verifier_task prompt with imperative SHA instruction.** Adding "MUST end with `Reviewed at: <THIS-EXACT-SHA>`. Copy this SHA verbatim — do NOT use any SHA from the prior review body." Hypothesis: the coordinator's TURN 3 isn't reaching the verifier in regurgitation mode at all (92.5s isn't enough for the 4-specialist + verifier dispatch). Strengthening the verifier prompt won't help if the verifier never runs. Keep this on the list as a secondary mitigation paired with #2 above.

**Evidence:** 3 production failures on svc-transcribe #37 (runs 25367689850, 25368789413, 25369351035), all at ~92.5s, all with prior-SHA footer. Cache-bust commit failed to recover. PR-restart workaround succeeded.

---

### P0 — Live progress flush (1-day fix)

**Problem:** stdout is block-buffered; users can't tell if a 30-min run is making progress or hung. We added `sys.stdout.flush()` in only one place (after learn.py). All other phase markers are buffered. Today this gets confused with actual hangs (e.g. the false report on run 25237782482 looked stuck for 42 min).

**Fix:** add `flush=True` to all `print()` calls in `managed/review.py`'s phase markers, plus run with `python -u` in `managed-review.yml`. Zero risk, zero compute cost, transforms debuggability.

**Evidence:** every recent log we inspected has all phase markers timestamped within 1 second of each other, at the script-exit time.

### P1 — Post-failure recovery (~partial; v1.12.1 shipped Path A+B, Path C deferred)

Three distinct failure paths now have production cases:

**Path A — `422 Validation Failed` on the comment post** ✅ **shipped in v1.12.1** (`_post_review_comment_with_retry`): on 422, parse the GitHub `message` field (scrubbed — avoids leaking PR snippets that GitHub may echo). If the message indicates near-duplicate detection, skip retry. Otherwise retry once after 2s. Both POSTs use `timeout=30`.

**Path B — Replace "post raw" fallback with structured comment** ✅ **shipped in v1.12.1**: when SHA-validation extractor refuses the coordinator output, post `## air review (run failed)` with the run URL, coordinator wall-time, the regurgitation/cache hypothesis, and a "push a small commit" workaround suggestion. Heading deliberately does NOT start with `## Code Review` to avoid colliding with `startswith("## Code Review")` checks in `pr_conversation.py` and the bash CLI flows.

**Path C — Coordinator regurgitation root cause** ⟶ **see P0-NEW above**. Production-confirmed on svc-transcribe #37 across 3 runs. Cache-bust workaround failed; PR-restart workaround succeeded. v1.12.1's defense is signal-without-fix; the root cause is on the model-behavior side and needs the diagnostic-then-retry approach in P0-NEW.

**Evidence (cumulative, 2026-05-05):** 4 of last 50 production runs hit one of these paths. v1.12.1's defense converts silence to signal; v1.12.2's diagnostic logging (P0-NEW step 1) gives us the data to confirm the regurgitation hypothesis; v1.13.0's retry mechanic (P0-NEW step 2) closes the loop.

### P2 — Re-review fast path (3-day fix, biggest user-perceived win)

**Problem:** PR #635's 9 re-reviews each ran the full 4-specialist + verifier loop on the entire diff. Re-reviews on a 50-line inter-diff don't need code-reviewer, simplify, and security-auditor all reasoning over the full PR Context — they need *prior-finding classification* (FIXED / NOT FIXED / PARTIALLY FIXED / DEFERRED) plus *new findings on the inter-diff only*. Current implementation reuses the standard coordinator path.

**Fix:** introduce a re-review-specific coordinator prompt that:
1. Fans out specialists on the **inter-diff only** (not the full PR)
2. Skips git-history-reviewer (its blame/churn data didn't change since last review)
3. Runs verifier with prior-findings classification as primary task, new findings as secondary
4. Targets 5-10 min total instead of 30 min

**Evidence:** PR #635's last review (2.4KB output, all FIXED + 2 nits) took 23.6 min — the same as its first review of the full diff. The work was 5× simpler; the cost was unchanged.

**Risk:** quality drift if specialists miss something the inter-diff hides via context they had before. Mitigation: validate against PR #635 history (we have 9 reviews of ground truth to backtest against).

### P3 — Cache stable context across re-reviews (1-week fix, compounds with P2)

**Problem:** every coordinator session re-loads PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, REVIEW.md, and SEVERITY-CALIBRATION.md fresh. These files change slowly (often unchanged across 5+ consecutive reviews on the same repo).

**Fix:** put the wiki content + PROJECT-PROFILE in a stable prefix at the start of the user message and add `cache_control: ephemeral` breakpoints. The 4 specialists' shared coordinator-context already benefits from cache via `callable_agents`, but we currently don't pin the breakpoints intentionally.

**Evidence:** re-review on PR #635 with identical wiki content theoretically gets ~70% of its tokens from cache. Per Anthropic docs, prompt caching reduces input cost 10× and saves latency. We've never measured the actual cache hit rate on coordinator sessions.

**Action:** instrument `usage` per turn (already accessible via `/sessions/<id>/threads`) and report cache_read vs cache_create ratios. If <50% on stable prefixes, restructure the user message.

### P4 — Auth-handler / config-only PR fast path (1-week fix, niche but high signal)

**Problem:** trivial PRs (a single config file edit, a typo fix, a 5-line dependency bump) get the full 30-min treatment. We have data: PR #41 was reviewed in <5 min by an early prototype because the diff was tiny.

**Fix:** at `build_pr_context` time, if `additions + deletions < 50` AND no security-sensitive paths touched (extracted from PROJECT-PROFILE), skip security-auditor and git-history-reviewer. Run code-reviewer + simplify only. Verifier on Sonnet finishes quickly with 2 specialist outputs.

**Evidence:** today the smallest PR we've reviewed in production took 13 min (qai-fe #239, fewer changes). Coordinator floor seems to be ~10 min regardless of input size.

**Risk:** missing a security issue in a tiny PR. Mitigation: define "security-sensitive paths" conservatively in PROJECT-PROFILE — auth, env, deploy, secrets paths always trigger full panel.

### P5 — Move codex into the coordinator (eval-only)

**Problem:** codex runs sequentially before the coordinator session (Pattern B). It's bounded at 35-45s, but those seconds are pure latency. Pattern A (codex inside the coordinator as a sub-agent) would parallelize codex with the 4 specialists.

**Fix:** evaluate Opus coordinator with codex as a 5th sub-agent. Earlier testing showed Sonnet coordinator with codex inside doesn't parallelize (serializes tool calls — 13 min wall, $5.63 vs current $4.14). Opus coordinator parallelizes but costs 2.5× the Sonnet equivalent.

**Decision:** **probably don't do this.** 35-45s wall savings isn't worth 2.5× coordinator cost. Re-evaluate only if Anthropic ships parallel-tool support on Sonnet.

### P6 — Codex prompt tightening (1-day fix, modest)

**Problem:** codex runs in 35-45s but its output is rarely cited in the verifier's findings. We treat it as a low-weight third-party reviewer; the verifier dispatches to it only when it confirms a Claude finding.

**Fix:** measure codex finding citation rate over 20 runs. If <10%, evaluate whether to drop codex (saves the 35s + the OpenAI API cost). If >30%, tighten codex prompt to reduce noise.

**Evidence:** anecdotally, codex's findings overlap heavily with security-auditor's. Need quantitative data before deciding.

### P7 — Wiki epilogue dispatcher (1-day fix, latency-only win)

**Problem:** learn.py runs synchronously inside review.py's main job. On every 5th review it adds 3-10 min to wall time, blocking the GHA runner well past the time the review comment was posted.

**Fix:** dispatch learn.py as a separate `workflow_dispatch` job triggered via `RemoteTrigger` after the review posts. The user-visible review latency stops at "Posted: ..." instead of dragging through learn.py.

**Risk:** learn.py needs the same wiki credentials and target-repo checkout. Easier to ship the trigger as a sibling job in the same workflow file (`needs: [review]`) than as a separate dispatch — same outcome.

**Evidence:** the 42-min run (`25237782482`) was 30 min coordinator + 10 min learn.py. Splitting them = users see "Posted!" at minute 31 instead of minute 42.

### P8 — Cross-repo benchmark publication (~1-day fix, infrastructure now in place)

**Status:** `air_ref` input parameter shipped in v1.11.0. The infrastructure exists; the recurring run does not. Set up a weekly cross-repo run on a known-good qai-be PR fixture and publish the results (cost, wall time, finding count parity) to a wiki page. **Builds the empirical loop we've been doing manually.**

**Trigger:** when we propose Phase 5 work that touches the coordinator prompt, the verifier task template, or model tiers, the cross-repo benchmark is the first thing that fires to catch quality regressions.

### P9 — Self-review deferrals from the v1.12.x stack

Findings flagged by self-review on PRs #61–#64 that we deliberately deferred. Each is a "tighten on next pass" item — none block production today, but they're the next-in-line debt if any of the surrounding code changes again.

**P9-a — `terminated_reason` format is the consumer-coupled contract for the billing matcher** (security-auditor self-review on PR #64). The billing-error fallback in `run_review` matches `_BILLING_REASON_HINTS` substrings against `coordinator_failure_reason`, which is `run_session`'s `terminated_reason` (built as `f"session error: {error!r}"` at the SSE event handler). If a future refactor changes that f-string format — e.g. to `f"session error type={type(error).__name__}, msg={error!s}"` — the substring match silently stops finding `"BetaManagedAgentsBillingError"` and the billing-aware comment regresses to the generic "other failure" branch. **Fix:** at `run_session`'s `terminated_reason` assignment site, add a comment pointing back to the consumer ("billing matcher in run_review reads this string"). Ideal long-term: surface `error.type + error.message` as a structured pair instead of a repr, so the matcher checks a stable schema instead of inferring from a repr. Cost: ~30 LOC + signature change to `SpecialistSessionError`. Trigger: any PR that changes how `run_session` formats `terminated_reason`.

**P9-b — REST poll loop API-call amplification under universal failure** (security-auditor F1 on PR #62). `_poll_rest_until_done`'s `POLL_INTERVAL_S=30s` over `POLL_BUDGET_S=COORDINATOR_TIMEOUT_SECS*0.9 ≈ 2430s` produces up to ~80 outer-loop iterations × 2 API calls (`events.list` + `sessions.retrieve`) ≈ **~160 Anthropic API calls per stuck session** worst case. Fine when the failure mode is exceptional. Becomes a problem if a region-wide Anthropic incident or a class of cache-heavy coordinator runs causes every consumer's review to enter this loop simultaneously — could trip per-org rate limits. **Fix (when triggered):** circuit breaker on consecutive empty drains (5 in a row → abandon early) and/or exponential backoff (`30s → 60s → 120s → 240s`, capped). **Trigger:** instrument `_poll_rest_until_done` invocations per day; if it fires on >20% of reviews, this becomes the rule not the exception and the polling cadence needs revisiting.

**P9-c — `_drain_via_rest` no-progress observability gap on stuck-running sessions** (code-reviewer on PR #62). The poll loop only drains REST events when the session has reached terminal state. A session that sits in `running` for the full 36-min budget produces ~72 liveness lines (`status=running agent_msgs=0`), all with the same `agent_msgs` count, with NO drain telemetry. Operators reading logs to diagnose "is the session making progress?" see only the iteration count. **Fix:** drain non-terminal events every Kth iteration (say K=5 ≈ 2.5 min) just to surface event-stream progress, OR include `getattr(sess, "open_threads", "?")` in the liveness line so operators see whether sub-agents are still being spawned. **Trigger:** the next time someone needs to debug a long-running session that never reaches terminal.

**P9-d — `error!r` in `terminated_reason` may regress to leak fields** (security-auditor on PR #64). The Anthropic SDK 0.93.0 typed-error schemas for `BetaManagedAgents*Error` don't currently expose API keys, session UUIDs, or PR-diff content in declared fields. **But** the `terminated_reason` posts `error!r`, which trusts the SDK to never add an internal-correlation field, account ID, or session UUID to the error union schema. A future SDK release could add `request_id` or `account_id` and the repr would silently start posting that to public PR comments. **Fix:** post structured fields explicitly — `f"{error.type}: {error.message}"` — instead of `repr()`. Couples to P9-a (same site). **Trigger:** SDK version bump that touches the error schema.

**P9-e — Stale `## Code Review` substring in the else-branch fallback prose** (security-auditor on PR #64, P9). The third (no-exception) fallback branch contains the literal `"## Code Review"` in narrative prose. Currently safe — `pr_conversation.py::BOT_REVIEW_PREFIXES` requires a trailing newline anchor that the prose mention lacks. **Risk:** if any future bash flow scans comments with un-anchored `grep '^## Code Review'`, this fallback body could be misclassified as a real review. **Fix:** rephrase to "without a recognised header" so the literal disappears from prose. **Trigger:** when next touching `pr_conversation.py` or any of `commands/*.md` flows that match on `## Code Review`.

**P9-f — Self-referential "VorobiovD/air" link in dogfood reviews** (code-reviewer on PR #64). The non-billing failure branch posts "file an issue against [VorobiovD/air](https://github.com/VorobiovD/air)". On dogfood (`air-review.yml` running on this repo's PRs), the developer reading the comment IS in VorobiovD/air — the link is a self-reference. Minor cognitive friction. **Fix:** conditionally suppress the link when `args.repo == "VorobiovD/air"`, OR rephrase to "the air plugin repo" without the link. **Trigger:** if dogfood failures become noisy enough to be confusing.

**P9-g — Duplicate `## air review (run failed)` header across three branches** (simplify on PR #64). Each branch in the structured-fallback opens with `"## air review (run failed)\n\n"` and ends with `{run_link_line}`. Today three call sites; if a fourth failure shape appears, a small `_run_failed_body(cause, fix, raw=None)` helper would consolidate. **Trigger:** adding a fourth branch.

---

## Deferred / explicit non-goals

- **Phase 3 from `cost-optimization-plan.md` (parallel_sessions_haiku, $0.63/round).** Skipping multi-agent saves $1.7K/year but loses architectural parity with the local CLI. Not worth the divergence.
- **Memory stores.** Tested in Phase 0 experiments; net-zero on cost, adds complexity.
- **Outcomes (self-eval loop).** Quality feature, not cost. Adds an entire grader iteration. No production case.
- **GitLab managed agent.** CLI plugin already supports GitLab via `commands/platform-gitlab.md`. Managed agent stays GitHub-only until a GitLab consumer asks.
- **Switching specialists to Haiku** (Phase 2 from cost plan, $4K/year). Quality watchpoint stalled — we never set up the structured A/B that compares Haiku-specialist findings to Opus-specialist findings on the same PRs. Until that A/B exists, the savings are speculative against documented quality risk.

---

## Decision rule for future phases

Add a row to this doc with: trigger, evidence, fix, risk, expected impact. Ship only when evidence is ≥3 production occurrences. Don't preemptively optimize against synthetic fixtures — the empirical learning loop is the comparative advantage we have over the cost-optimization-plan's experiment harness.

## What svc-transcribe #37 taught us (Phase 4 retrospective)

A single PR with 14 review rounds, 13 consecutive CHANGES_REQUESTED, and an eventual two-failure cascade became the dominant data source for Phase 4 AND Phase 5 priorities. Five lessons that should bias the next phases:

1. **Asymmetric gates are a usability trap.** Fresh review and re-review used to gate on different severity sets (blocker vs blocker+medium). The asymmetry meant a PR could go from APPROVED → CHANGES_REQUESTED on a re-review with no new blockers — purely because medium prior findings now counted. Fix was structural, not parameter-tuning.

2. **The verifier "knows" enough to break loops, but the prompt didn't ask.** The prior review body has been in context since the carry-forward feature shipped — we just didn't tell the verifier to do anything with the repetition signal. Cheap to add a rule; very effective.

3. **Self-review with --dry-run on a PR catches structural bugs that synthetic test fixtures miss.** Codex caught the legacy-missing-severity regression that all four Claude reviewers missed because they read the new code from the perspective of new bodies, not legacy ones. Cross-model review at the self-review step is high-leverage when the change touches a default that's load-bearing for backward compatibility.

4. **Defensive aborts must produce a signal, not silence.** The orchestrator's SHA-validation refused to submit a verdict on stale coordinator output (correct) but then fell back to raw-posting that same stale output (wrong). The 422 cascade left the developer with a frozen CHANGES_REQUESTED verdict and no in-PR signal that the bot's machinery had broken. Whenever we add a defensive check, we need to add a structured "this is what went wrong" comment in parallel — silence looks identical to "the bot is still working on it."

5. **Coordinator wall-time is a stale-cache signal.** A 92s coordinator run on a real PR is impossibly fast (typical: 1500-2400s). When the run is short AND output is unusable, it's almost certainly a cached prior-thread response — not a hung session. Surface this as a soft-failure event so we can correlate with Anthropic-side caching and decide whether to retry-with-cache-bust.

6. **"Cached output" was the wrong frame.** The cache-bust commit (whitespace change to README) DID NOT recover svc-transcribe #37. The coordinator returned the same 92.5s + prior-SHA output on the next run with a different prefix. This rules out Anthropic prefix-cache as the primary cause and points at a model-behavior issue: the coordinator is regurgitating `prior_review_body` from its user-message context. Future debugging should distinguish "framework caching" from "model self-recognition" early — the former is fixable by varying the prefix, the latter needs a prompt or codepath change.

7. **PR-restart is a valid escape hatch.** On long re-review chains where the coordinator has degraded, closing the failing PR and reopening from the same branch as a fresh PR avoids the re-review codepath entirely. No `prior_review_body`, no `<prior-round-statuses>` block — coordinator must dispatch real specialist work. The workaround loses comment history but recovers the merge path. Worth documenting in the bot's run-failed comment so users have a path forward when the regurgitation pattern triggers.
