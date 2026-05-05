# air — Improvement Roadmap (post-shipped phases)

_Last updated 2026-05-05 (post-v1.12.0, after svc-transcribe #37 stale-coordinator-cascade evidence). Phase 4 shipped in v1.12.0. Reframed against telemetry from the last 20 production runs (qai-be PRs #41/#593/#595/#617/#635, qai-fe PRs #239/#246, svc-transcribe #37, plus dogfood runs)._

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
| **Stale-coordinator-output cascade** (svc-transcribe #37 run 25367689850) | 1 | **NEW failure mode** — coordinator returned in 92.4s (vs typical 1500-2400s) with `Reviewed at:` footer pointing at the PRIOR head SHA (`73272866`) instead of current (`c1825198`). Orchestrator's SHA-validation correctly refused the verdict; raw-post fallback then hit 422 (likely body near-duplicate of the existing bot comment at that SHA). Two-failure cascade: stale output → defensive abort → fallback path 422'd. |
| Pre-PR-#46 stuck runs (45+ min) | 0 in May | gone after pre-comp + tier swap |

### Hidden observability gap

`managed/review.py` prints phase markers (`[1] Syncing... [2] Fetching... [3] codex... [4] coordinator...`) to a buffered stdout that only flushes when the script exits. The GHA log shows all of these timestamped at **the same instant** — script-exit time, ~30 min after they printed. If a run hangs at minute 35, the live log shows nothing past `[1] Syncing...` until the watchdog kills it. This is currently working around itself by luck.

---

## Reframed improvement priorities (Phase 5 candidates)

Sorted by **value × evidence × cost-to-ship**. Each has a concrete trigger. Phase 4 just shipped in v1.12.0; the remaining priorities are unchanged in shape but their relative ranking shifted now that the verdict-gate problem is closed.

**Recommended next ship:** P0 + P1 + P7 as a single bundle (~3 days, all low-risk, transforms operator UX). P2 (re-review fast path) is the biggest user-perceived latency win but warrants its own PR with a backtest against PR #635 / svc-transcribe #37 history.

---

### P0 — Live progress flush (1-day fix) ⟵ **next**

**Problem:** stdout is block-buffered; users can't tell if a 30-min run is making progress or hung. We added `sys.stdout.flush()` in only one place (after learn.py). All other phase markers are buffered. Today this gets confused with actual hangs (e.g. the false report on run 25237782482 looked stuck for 42 min).

**Fix:** add `flush=True` to all `print()` calls in `managed/review.py`'s phase markers, plus run with `python -u` in `managed-review.yml`. Zero risk, zero compute cost, transforms debuggability.

**Evidence:** every recent log we inspected has all phase markers timestamped within 1 second of each other, at the script-exit time.

### P1 — Post-failure recovery (1-2 day fix, **bumped after 2026-05-05 evidence**)

Two distinct failure paths now have production cases:

**Path A — `422 Validation Failed` on the comment post** (qai-be PR #635 run `25264529652`, 2026-05-02): coordinator finished cleanly (266s), the body was extracted, but `gh pr comment` POST returned 422. No retry, no body inspection. Root cause unknown because we discard the response body. Subsequent same-SHA run worked fine.

**Path B — Stale-coordinator-output cascade** (svc-transcribe #37 run `25367689850`, 2026-05-05): coordinator returned in 92.4s with a `Reviewed at:` footer pointing at the PRIOR head SHA (`73272866`) instead of current (`c1825198`). Orchestrator's SHA-validation correctly refused to submit a verdict (good). But the raw-post fallback then hit 422 — likely because the body was a near-duplicate of the existing bot comment at the prior SHA (GitHub may reject identical-body comments). The fallback's "post raw, skip verdict" semantics did MORE harm than just aborting: it produced a 422 cascade and lost the option to retry from scratch.

**Fix (composite):**

1. **422 retry with body capture** — in `post_review_comment`, on 422 capture the response body, log it, retry once after a 2s backoff. If body indicates `"body too long"` (>65KB), truncate intelligently and retry. If it's a near-duplicate-detection error, abort cleanly with a structured warning (don't keep hammering).
2. **Replace "post raw, skip verdict" with "abort with a status comment"** — when the SHA-validation extractor refuses the coordinator output, do NOT post the raw body. Post a short structured comment instead: `## Code Review (run failed)` with the run URL and a one-liner ("coordinator output didn't match head SHA — likely race-with-push during the coordinator session"). This gives the developer signal AND lets the next push trigger a clean run.
3. **Detect impossibly-fast coordinator runs as a stale-cache signal** — if `coordinator complete in <300s` AND the orchestrator can't extract a head-SHA-matching block, treat it as a soft failure. Log a structured event so we can correlate stale-cache incidents with Anthropic-side caching behavior. (300s is well below the 90th percentile of any successful run we've measured.)

**Evidence:** 2 of last 50 production runs lost work to one of these paths. Both failures left the developer with a stale CHANGES_REQUESTED verdict and no signal that the bot's mechanic was broken — they had to dismiss the bot review manually to merge (svc-transcribe #37 today).

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
