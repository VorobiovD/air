# air Managed Agents — Cost Optimization Plan

_Last updated 2026-04-27 with empirical results from 14 test variants run on real PRs (#38 and #40)._

## TL;DR

Today's managed-review architecture (`production_clone` shape — 5 separate sessions) costs **~$3-8 per review round** and **~$12,300/year at 6 rounds/day**. We tested a matrix of architectural variants on two real PRs. Three actionable paths emerged:

| Phase | Change | Annual cost | Savings | Quality risk |
|---|---|---|---|---|
| Today | 5 separate sessions, Opus+Sonnet mix | $12,300 | — | — |
| **Phase 1** (ship now) | Migrate to multi-agent matching local CLI | **$7,200** | -$5,100 | **Zero** |
| **Phase 2** (5-10 PR validation) | + Haiku for specialists, prod prompts kept | **$3,100** | -$9,200 | Medium |
| **Phase 3** (only if Phase 2 holds) | Drop multi-agent (it's overhead at this scale) | **$1,400** | -$10,900 | Low (incremental) |

The biggest finding: **multi-agent's value depends entirely on model choice.** With Opus+Sonnet specialists, multi-agent saves up to 49% on big PRs. With Haiku specialists, multi-agent's coordinator overhead becomes a *net cost* — separate sessions are cheaper.

## Methodology

We built a comparison harness (kept local-only, gitignored under `managed/experiments/`) that runs the same review work across 14 architectural variants, capturing:
- Per-thread token usage (cache_read, cache_create, output, input)
- Active session-hour compute time
- Wall-clock time
- Findings produced (manually compared for quality)

Tested on:
- **PR #38** (178 lines, 4 files) — small-to-medium feature PR
- **PR #40** (936 lines, 18 files) — large feature PR with multiple files

Each variant performs the SAME review+verify work — differences are purely orchestration, models, or prompts. Apples-to-apples comparison.

Verified pricing (2026-04-27):
- Token rates: standard Claude API (no Managed Agents markup)
- Compute: $0.08/session-hour (active runtime only)
- Web search: $10/1000 (we don't use it)
- Cache: read at 10% input, 5-min write at 1.25×, 1-hour write at 2×

## What today's architecture actually does

`production_clone` (the variant that mirrors today's `managed/review.py`):

```
4 specialists in parallel via asyncio.gather():
  ├── code-reviewer (Opus 4.7) — full PR Context + diff + 6 wiki files (~150k context)
  ├── simplify (Sonnet 4.6) — same context loaded fresh
  ├── security-auditor (Opus 4.7) — same context loaded fresh
  └── git-history-reviewer (Sonnet 4.6) — same context loaded fresh
1 verifier session sequentially:
  └── review-verifier (Opus 4.7) — full PR Context + diff + ALL 4 specialists' findings
```

Five independent sessions, each loading and re-reading its own ~150k prefix. The same wiki files get cached and re-read 5×.

## What the local CLI does

The local CLI plugin (`plugins/air/commands/review.md` Step 7) uses Claude Code's Agent tool to spawn the same 5 agents — but they all run within ONE Claude Code orchestrator session. Subagents share cached context efficiently. This is structurally identical to a managed multi-agent session with `callable_agents`.

**Production_clone's separate-sessions architecture exists ONLY because `callable_agents` was research-preview and inaccessible.** With research-preview access (granted 2026-04-25), managed should match local CLI architecture.

## Empirical results — costs by variant

### Cost per round (real PRs)

| Variant | PR #38 cost | PR #40 cost | Architecture | Models |
|---|---|---|---|---|
| **production_clone** (today) | **$3.15** (n=2) | **$8.08** | 5 separate sessions | Opus+Sonnet (3 Opus + 2 Sonnet) |
| **multiagent_production** | **$2.45** (n=2) | **$4.14** | Multi-agent (1 session) | Same Opus+Sonnet mix |
| tiered_v2_real_prodprompts | $1.27 | $1.55 | Multi-agent | Sonnet coord + Haiku specialists + Sonnet verifier |
| tiered_v2_real | $0.68 | $0.90 | Multi-agent | Sonnet coord + Haiku + stripped prompts |
| **parallel_sessions_haiku** | **$0.62** | **$0.63** | 5 separate sessions | Haiku specialists + Sonnet verifier |
| multiagent_production_2turn | $5.77 | (skipped) | Multi-agent, Opus coord absorbs verifier | Opus+Sonnet | (LOSER — verifier-as-coordinator-turn explodes cost) |

### Cost decomposition (PR #40, the bigger fixture)

How does going from $8.08 (today) → $0.63 (parallel_sessions_haiku) break down?

| Step | Variant transition | Δ cost | % of total savings |
|---|---|---|---|
| Multi-agent architecture | production_clone → multiagent_production | -$3.94 | 53% |
| Haiku swap on specialists | multiagent_production → tiered_v2_real_prodprompts | -$2.59 | 35% |
| Drop multi-agent (overhead now) | tiered_v2_real_prodprompts → parallel_sessions_haiku | -$0.92 | 12% |
| **Total** | $8.08 → $0.63 | **-$7.45** | **100%** |

Both multi-agent (with current models) and Haiku contribute substantially. They are not redundant.

### Multi-agent savings AMPLIFY with PR size

| | PR #38 | PR #40 | Why |
|---|---|---|---|
| production_clone | $3.15 | $8.08 (2.6×) | Each session reloads full context independently — scales linearly with diff size |
| multiagent_production | $2.45 (-22%) | $4.14 (-49%) | Coordinator's shared context is reused — cache hit rate scales better than cache_create |

The verifier in production_clone has to load the full diff + wiki + ALL 4 specialists' findings as a fresh prompt. On a 1300-line diff, that's massive cache_create. The multi-agent verifier sub-agent inherits cache from the coordinator's setup. **The bigger the PR, the more this matters.**

### Annual scaling at 6 rounds/day

| Variant | Avg cost/round | $/year | Savings |
|---|---|---|---|
| production_clone (today) | $5.61 | **~$12,300** | — |
| multiagent_production | $3.29 | ~$7,200 | -$5,100 |
| tiered_v2_real_prodprompts | $1.41 | ~$3,100 | -$9,200 |
| tiered_v2_real | $0.79 | ~$1,700 | -$10,600 |
| parallel_sessions_haiku | $0.63 | ~$1,400 | -$10,900 |

At 10× team adoption: **savings scale to $50K-110K/year**.

## Quality comparison (real PR #38)

Both `production_clone` and the alternatives caught the most-important security finding (`persist-credentials: false`). Detailed findings comparison:

| Quality axis | production_clone | tiered_v2_real_prodprompts (Haiku, full prompts) |
|---|---|---|
| Total findings | 10 confirmed + 12 explicit FP-drops | 11 findings (3 medium + 6 low + 2 special) |
| Cross-agent corroboration | "raised by code-reviewer + security-auditor" tags | Per-agent attribution lost in synthesis |
| FP-drop reasoning visible | Yes (12 explicit drops with reasoning) | No (filtering happened, trace lost) |
| "Is not malware" preamble noise | Yes — ~17% of output is safety self-narration | No — clean output from line 1 |
| Most-important finding caught | ✅ persist-credentials | ✅ persist-credentials (downgraded with reasoning) |
| Unique findings (production missed) | — | DRY violation, format validation, n_specialists count, base_sha validation, docstring count |
| Unique findings (tiered missed) | force-push edge case, empty stdout, stdout unbounded, doc drift, wall-time inaccuracy | — |

**Verdict: roughly equivalent quality with different strengths.** Production has more rigorous verifier output (cross-corroboration, explicit FP filtering trace). Tiered is more concise and finds different real bugs.

## Recommended phased rollout

### Phase 1 — In progress (low risk, ~1 day, $5K/yr saved)

**Migrate managed to multi-agent matching the local CLI architecture.** Same models, same prompts, same agent definitions — just replace 5 separate sessions with 1 multi-agent session using `callable_agents`.

**Status (2026-04-28):** Implementation in progress on `feat/phase-1-multiagent`. Coordinator agent + setup wiring + review.py refactor complete; PR pending.

**Implementation:**
- New beta header: `managed-agents-2026-04-01-research-preview` (granted 2026-04-25) — `managed/api.py`
- New `air-coordinator` agent registered via `managed/setup.py` with `callable_agents=[code-reviewer, simplify, security-auditor, git-history-reviewer, review-verifier]`
- `managed/review.py::run_review` collapsed: create coordinator session, send PR Context + diff + codex findings + verifier_task as user message, wait for idle. Replaces asyncio.gather over 4 specialists + sequential verifier.
- Codex stays GHA-side (Pattern B): runs sequentially BEFORE the coordinator session, output injected into the user message as `<codex-findings>...</codex-findings>`. Sonnet coordinator with codex inside doesn't parallelize reliably; Opus coordinator costs 2.5×.
- Coordinator system prompt (`plugins/air/agents/coordinator.md`): strict 3-turn protocol (TURN 1 dispatch 4 specialists in parallel, TURN 2 delegate to verifier with all findings + verifier_task, TURN 3 output verifier's result verbatim + bash wiki update)
- Bonus fix bundled: verifier-narration partition leak at `managed/review.py:1166` — anchor on `\n## Code Review` instead of bare `## Code Review`
- All other code (managed/learn.py, agent prompts, etc.) unchanged

**Why zero quality risk:**
- Same models (Opus+Sonnet mix exactly as today)
- Same agent prompts (loaded from `plugins/air/agents/*.md` unchanged)
- Same architecture as the local CLI which we run via `/air:review` daily and trust

**Expected impact:**
- 22-49% cost reduction depending on PR size (49% on bigger PRs)
- Wall time roughly equivalent (multi-agent has marginal overhead but saves cache_create)
- ~$5,100/year saved at current cadence

### Phase 2 — Layer Haiku on specialists after Phase 1 validates (medium risk, ~2 days, $4K/yr more saved)

**Switch the 4 specialist sub-agents to Haiku 4.5. Keep Sonnet coordinator and Sonnet verifier. Keep production prompts.**

**Implementation:**
- Update `managed/setup.py` to set `model: claude-haiku-4-5` on the 4 specialist sub-agents
- Update `plugins/air/agents/{code-reviewer,simplify,security-auditor,git-history-reviewer}.md` frontmatter `model:` field
- No prompt changes
- A/B behind a feature flag (`AIR_SPECIALISTS_HAIKU=true`) for first 5-10 PRs
- Compare findings against Phase 1 baseline; ship if quality holds

**Quality watchpoints:**
- Does Haiku catch security issues as well as Opus on security-auditor role?
- Does Haiku cite wiki patterns and apply project-profile rules correctly?
- Does the verifier's FP-drop rate change significantly?

**Expected impact:**
- 60-80% additional cost reduction
- Wall time may increase (Haiku is slower per turn on long prompts)
- ~$4,100/year saved beyond Phase 1

### Phase 3 — Re-evaluate multi-agent only if Phase 2 ships (low marginal risk, $1.7K/yr more)

**Surprising finding:** once specialists are on Haiku, multi-agent's coordinator overhead becomes a net cost. `parallel_sessions_haiku` ($0.63/round) beats `tiered_v2_real_prodprompts` ($1.55/round) by 60%.

**Decision:** if Phase 2 ships and quality holds, evaluate whether to drop multi-agent and revert to separate sessions (with Haiku). The architectural simplification might be worth ~$1.7K/year, but it's not urgent.

**Don't do this preemptively** — Phase 1's multi-agent gives architectural clarity (matches local CLI) which is its own value. Only revisit if/when Phase 2 quality is fully validated.

## Variants tested (full matrix for reference)

| Variant | Description | PR #38 | PR #40 | Verdict |
|---|---|---|---|---|
| split | 2 sessions: reviewer + verifier (synthetic fixture) | $0.17 | — | tiny fixture only |
| split_memory | + Memory store | — | — | not tested on real PR |
| solo | 1 agent does both | — | — | quality risk too high |
| solo_memory | solo + memory | — | — | not pursued |
| multiagent | coordinator + reviewer + verifier sub-agents | $0.34-0.50 | — | beaten by tiered_v2 |
| multiagent_memory | + memory | $0.50 | — | memory worse than expected |
| multiagent_parallel | 3 indep sub-agents (parallelism test) | $0.34 | — | confirmed partial parallelism |
| multiagent_tiered | Opus coord + Haiku sub-agents | $0.38 | — | Opus coord too expensive |
| **multiagent_tiered_v2** | Sonnet coord + Haiku, 2-turn protocol | **$0.19** | — | best on synthetic, scales to real |
| multiagent_outcomes | + Outcomes self-eval loop | not run | — | API needed `user.define_outcome` event |
| all | multiagent + memory + outcomes | $0.64 | — | over-engineered |
| **production_clone** | Today's architecture | $3.15 | $8.08 | baseline |
| **multiagent_production** | Today's models, multi-agent | $2.45 | $4.14 | **Phase 1 target** |
| **tiered_v2_real_prodprompts** | Multi-agent + Haiku + prod prompts | $1.27 | $1.55 | **Phase 2 target** |
| tiered_v2_real | Multi-agent + Haiku + stripped prompts | $0.68 | $0.90 | quality unvalidated |
| **parallel_sessions_haiku** | Separate sessions + Haiku | $0.62 | $0.63 | **Phase 3 candidate** |
| multiagent_production_2turn | Opus coord absorbs verifier | $5.77 | (skip) | LOSER |

## Research-preview features investigated

### Multi-agent (callable_agents) — works, recommended for Phase 1

API surface verified:
- `agents.create({callable_agents: [...]})` — registers sub-agents the coordinator can call
- Coordinator session uses `tools: [{type: "agent_toolset_20260401"}]`
- Sub-agent threads are visible at `/sessions/<id>/threads`
- Per-thread `usage` is queryable for accurate per-model cost calculation
- Beta header: `managed-agents-2026-04-01-research-preview`

### Memory stores — works but doesn't help us

Tested in `multiagent_memory` and `tiered_v2_real_memory`. Memory content lives at `/mnt/memory/` and is read via Read tool — counts toward tokens just like inline content. **Net effect: no significant savings** because wiki content gets read into context anyway, just from a different mount path.

### Outcomes (user.define_outcome) — works, but COSTS money, doesn't save it

Outcomes is a self-evaluation loop where Claude iterates against a rubric until satisfied. **It's a quality-improvement feature, not a cost feature.** Adds an entire grader iteration cycle. Skipped from production recommendations.

API verified:
- Send `user.define_outcome` event AFTER session create (NOT a session-creation field — that's why our first attempt 400'd)
- Event includes `description`, `rubric: {type: "text" | "file", content/file_id}`, `max_iterations`
- Beta header: `managed-agents-2026-04-01-research-preview`

## Test harness

Lives at `managed/experiments/` on developer machines (gitignored, not checked in — see commit `0103446`). Reusable for future cost evaluation work; rebuild fresh fixtures from a recent PR when re-running.

```
managed/experiments/        (local-only, gitignored)
├── cost_test.py             # 14-variant test harness
├── README.md                # Usage docs
├── fixtures/
│   ├── test_pr.diff         # Synthetic 80-line auth handler (3 deliberate bugs)
│   ├── real_pr38/           # Real PR #38 fixtures (diff + wiki snapshot)
│   └── real_pr40/           # Real PR #40 fixtures
└── results.jsonl            # Append-only result log
```

To run a variant: `python3 managed/experiments/cost_test.py --variant <name>`. Override fixture: `EXPERIMENTS_PR_FIXTURE=real_pr40 python3 ...`. Generate report: `... --report`. Cleanup: `... --cleanup`.

## Caveats

1. **Two PR samples** — PR #38 (small) and PR #40 (large). Cost scales nonlinearly with PR complexity. Real production PRs may distribute differently.
2. **PR #40 production_clone wall time** wasn't captured (script timed out at 900s; sessions kept running). Compute time is accurate.
3. **Quality comparison** is qualitative — no programmatic FP/FN labeling. Phase 2 should add structured quality gates before full rollout.
4. **Verifier-narration leakage** (the "is not malware" preamble issue) was present in production and visible in production_clone tests. The `managed/review.py` partition logic now anchors on `\n## Code Review` instead of bare `## Code Review` (fixed in Phase 1 implementation).
5. **`tiered_v2_real`'s stripped prompts** were never quality-validated against a human reviewer. The 75% savings claim from earlier rounds depends on this — ship Phase 2 with full prod prompts (`tiered_v2_real_prodprompts`) instead.

## Files to modify (Phase 1)

- **MODIFY:** `managed/setup.py` — add `air-coordinator` agent definition with `callable_agents` list referencing existing 5 specialist agents
- **MODIFY:** `managed/review.py::run_review` — replace separate-sessions `asyncio.gather` with single coordinator session creation + user message + wait_for_idle
- **MODIFY:** `managed/api.py` — bump beta header to `managed-agents-2026-04-01-research-preview`
- **NEW:** `plugins/air/agents/coordinator.md` — coordinator system prompt with strict 3-turn protocol
- **MODIFY:** `managed/review.py:1163` — fix verifier-narration partition leak (`\n## Code Review\n` anchor)

## Verification approach

1. **Cost validation**: re-run `multiagent_production` 5 times on PR #40 to bound variance more tightly than the n=2 sample we have today.
2. **Quality validation**: run Phase 1 in `--dry-run` mode against the next 3-5 real PRs in parallel with current production. Compare findings sets manually.
3. **Latency validation**: measure end-to-end wall time on Phase 1 vs production_clone for same PRs.
4. **Cutover**: ship Phase 1 behind feature flag `AIR_USE_MULTIAGENT=true` initially. Monitor for 1-2 weeks. Then make it default.

## What this PR does NOT do

- Does not switch any models (Phase 2)
- Does not modify agent prompts
- Does not change `managed/learn.py` (separate concern)
- Does not change CLI plugin behavior (already correct architecture)
