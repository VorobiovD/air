# air — Improvement Roadmap (Master)

**Single source of truth for all air planning.** Previous planning docs have been folded into this file. Originals preserved with banners pointing here:

- `~/Documents/air-improvements-plan.md` — managed-agents capability uptake plan (Items A-Q)
- `~/Documents/air-improvements-plan-review.md` — review notes + Phase 0 derivations
- `~/Documents/air-improvements-inventory.md` — full source inventory (~50 items × 8 themes)
- `docs/legacy/cost-optimization-plan.md` — 14-variant cost matrix (Phase 1 shipped v1.11.0; Phase 2/3 deferred)
- `docs/legacy/air-expansion-plan.md` — three-phase team-rollout + Cowork plan
- `docs/legacy/architecture-review.md` — early architecture concerns (most addressed in v1.7+)

**External commitments** tracked separately (internal notes).

_Last updated: 2026-06-09 (post-v1.29.0 — promote fast-path + caller-variable toggle, the UI/business-audience copy reviewer (6th specialist) with CLI/TUI-copy coverage, $0 auth preflight, and the `AIR_REVIEW_MODE` caller variable. Earlier arc, post-v1.22.0: memory-store migration complete fleet-wide, deterministic wiki-mirror render, solo/both review mode, multi-reviewer PAT model). See "Since v1.13.0" below for the delivery summary._

---

## TL;DR

**Current shipped:** v1.29.0 (2026-06-08). The arc since v1.22.0: the **promote fast-path** (delta-review sibling promotes, opt-in via a caller variable — v1.25/v1.26), the **UI / business-audience copy reviewer** (6th specialist, team request — v1.27, extended to CLI/TUI copy v1.28), **$0 auth preflight** (v1.23), and the **`AIR_REVIEW_MODE` caller variable** (v1.29 — flip full/solo/both from Settings). The earlier arc since v1.13.0: the **memory-store migration is complete fleet-wide** (all 4 repos store-backed), which addressed the dominant cache-read cost lever; **solo/both review mode** ships the cheaper-review experiment; the **multi-reviewer PAT model** is live on all callers. Full delivery list in the Shipped table and "Since v1.13.0 — delivered + active" below.

**Active fronts (where attention goes next):**
1. **Solo/both routing decision** — the routing call is still open (full for initial/large, solo for re-review/small). **Caveat found 2026-06-08:** `both` had been silently NOT running on the work-repo callers (they forced `review_mode: || 'full'`), so no comparison data was actually collected; v1.29.0 (#138) made `review_mode` resolve from the `AIR_REVIEW_MODE` caller variable, and `both` is now set on repo-A + repo-B — collection starts now (revert by deleting the variable after the window). Solo is ~3× cheaper/faster but NOT gate-safe on substantial PRs (severity downgrade observed). See §Since-v1.13.0 C.
2. **Coordinator output cost (60–150K tokens/review)** — the #1 structural spend; the fix (file-handoff) is built but **runtime-blocked** (isolated callable-agent threads). Solo-routing is the live lever instead.
3. **Rotation/ops hardening** — the `rotate-air-pat.sh` fan-out skips repo-C + never refreshes `AIR_BOT_TOKEN` (caused the 06-03→06-05 repo-C outage). Add repo-C to the fan-out for all `<STEM>_PAT` + the bot token. See §Since-v1.13.0 D.

**Phase 5** carries the P0-P9 priorities (existing) with status updates.
**Phase 6** integrates the managed-agents platform features (P10-P13). C10/P12 (drop `-research-preview`) is still OPEN — `managed/api.py` still sends the research-preview suffix.

---

## Shipped (in version order)

| Version | Change | PR | Impact |
|---|---|---|---|
| v1.9.0 | Multi-agent coordinator (`callable_agents` research-preview) replacing 5 separate sessions | #45 | -49% cost on large PRs |
| v1.11.0 | Pre-computation of blame/churn/file-status/diff-check on the runner | #46 | -33% wall time on Laravel cross-repo bench |
| v1.11.0 | Verifier on Sonnet (was Opus); git-history-reviewer on Haiku (was Sonnet) | #46 | **3× faster verifier** — repo-B PR #239 dropped 39 min → 13 min on same diff |
| v1.11.0 | `air_ref` input parameter for cross-repo benchmarking | #46 | Feature branches run against real consumer PRs without touching their main config |
| v1.12.0 | Severity-aware verdict gate + DEFERRED status | #49 | Lows/nits don't block; defense-in-depth rejects `[blocker] — DEFERRED` |
| v1.12.0 | SSE delivery latency mitigation — REST events fallback at 90s quiet timeout | #49 | Caps stuck-stream tail latency |
| v1.12.0 | Extractor narration anchor `(?<!\`)## Code Review` + learn.py stderr capture | #49 | Fixes repo-A #635 narration leak |
| v1.12.0 | Re-review gate narrowed to blocker-only (mediums = warnings) | #51 | repo-D #37 — would have flipped all 13 CHANGES_REQUESTED rounds to APPROVED |
| v1.12.0 | Carry-forward suppression — auto-DEFER 2nd consecutive NOT FIXED on non-blockers | #51 | Eliminates perpetual-loop pattern (svc-tx #37 finding #2: 13 NOT FIXED rounds in a row) |
| v1.12.0 | Workflow concurrency — `cancel-in-progress: true` | #51 | Prevents overlapping reviews; latest push runs to completion |
| v1.12.0 | Legacy missing-severity default flipped to `blocker` | #51 | Pre-v1.12 prior bodies keep gating instead of silently un-gating |
| v1.12.1 | Structured `## air review (run failed)` fallback comment + 422 retry on review post | #54 | Replaces 422 cascade with actionable signal |
| v1.12.2 | Debug log of `coordinator_out[:2000]` on SHA-mismatch | #57 | Diagnostic instrumentation that confirmed the SSE/REST race hypothesis |
| v1.12.3 | SSE/REST race fix: retry drain on eventually-consistent events (per-attempt delta tracking) | #61 | repo-A #635-style failures (~92s coordinator + REST lag) recover |
| v1.12.4 | REST polling until session terminal — handles SSE stream-close mid-session | #62 | repo-A #666 went from 92s empty-output to 1432s + real review |
| v1.12.5 | Billing-aware structured-fallback (`BetaManagedAgentsBillingError`) | #64 | svc-tx billing exhaustion → actionable comment instead of stack trace |
| v1.12.6 | Footer-regex word-boundary trap fixed | #67 | repo-A #666 round 7 verifier output recovered: `\b` failed when 40-hex SHA followed by `Wiki` (both `\w`) |
| **v1.13.0** | **5 prompt additions** — exposure escalation (verifier), CLAUDE.md gotcha grep + paired-doc drift + gate-output symmetry (code-reviewer), category-symmetric respond gate (review-respond) | #70 | Captures new failure classes from repo-C #153 + repo-A HIPAA cross-patient leak + repo-A #732 respond cycle |
| v1.14.0 | Fast-mode Opus on code-reviewer + security-auditor (B1/Item E); security-audit FAIL-only 4-col table (drop PASS/FAIL clutter) | #74, #77, #78 | ~2× faster generation on the two heaviest agents at zero prompt cost (fast premium unbilled on managed) |
| v1.15.0 | Learn cadence cut **3×** → every 15 reviews / 14 days (was 5/2); `air-learner` Opus→Sonnet | #81 | Biggest single learn-cost cut; cadence config verified correct 2026-06-05 |
| v1.16.0 | Opus alias 4.7→4.8 + cost-doc correction ($5/$25, not $15/$75); cooldown debounce + respond-driven re-request; agent removal = archive (no DELETE route) | #84, #86, #83 | Re-request fires the re-review exactly on `--respond`, not every push |
| v1.17.0 | **Per-repo memory-store backend (repo-D pilot)**; fail-loud on run-failed + billing canary preflight; cross-PR awareness + session-efficiency guidance; 12-char SHA-prefix footer match | #90, #87, #80, #89 | Store migration begins; billing exhaustion no longer leaves CI silently green |
| v1.18.0 | **Agent version pinning** (`agent_versions` input — Item F); file-handoff via Files-API mounts (#92) **gated OFF** (#96 — threads are isolated containers); `fresh` input | #95, #92, #96, #94 | Pinning enables canary/rollback; file-handoff parked pending runtime support |
| v1.19.0 | **Pattern A targeted retrieval** (grep pattern files, not whole reads — Track 1); learn bloat caps (surgical); byte-bound 100KB overflow chunking | #105, #101, #102, #99 | Attacks the dominant cache-read cost lever; chunking enables the store split |
| v1.19.1–1.19.2 | Codex bwrap-disable fix; pre-post dedup (no stacked duplicate reviews); default coordinator to **inline mode** via explicit MODE header; fail-loud on codex apology | #107, #109, #112, #110 | Restores Codex on the runner; inline is the production handoff shape |
| v1.20.0 | Optional **`expected_reviewer` identity assertion** (token-owner == requested reviewer); retry transient preflight billing_error w/ backoff | #116, #113 | Closes the multi-reviewer PAT trust seam (was deferred as repo-A #90) |
| v1.21.0 | **Solo / both review mode** (`AIR_REVIEW_MODE` full\|solo\|both) — single-agent advisory review for comparison/cost | #117 | ~3× cheaper/faster than the 6-agent coordinator; benchmarking live (see §Since-v1.13.0 C) |
| **v1.22.0** | **Deterministic store→wiki mirror render** — `render_store_to_wiki.py` replaces the in-session AI render; throttled per-review (≤1×/hr) + authoritative on learn | #119 | Wiki stays fresh between learns; ~40% less learn-session output (in-session render removed) |
| v1.23.0 | **Auth preflight** — fail $0 before any session on a missing/expired/no-access review token | #122 | Token failures cost nothing and surface an actionable comment instead of a silent/late error |
| v1.24.0 | Safe cost/quality prompt fixes from the 06-07 review audit | #124 | Low-risk verifier/reviewer prompt tightening — no behavior gamble |
| v1.25.0 | **Promote fast-path** — re-review a `promote/staging-to-main-*` PR as a delta vs its last-reviewed sibling (≥80% line overlap) instead of a full re-read | #126 | ~64% cost cut on the repo-A/repo-B Phase-4 promote chain (backtest), zero net-new-finding loss; conservative full-review fallback below threshold |
| v1.26.0 | Promote fast-path **opt-in via a caller `AIR_PROMOTE_FASTPATH` repo/org variable** (no caller-workflow edit) | #128 | Fleet/repo toggle from Settings; default OFF |
| **v1.27.0** | **UI / business-audience copy reviewer** (`air-ui-copy-reviewer`, 6th specialist, Sonnet) — flags dev jargon, AI "writing fluff", clarity + static UX/a11y on user-facing diffs | #130 | team request; dispatch-gated so backend-only PRs add $0; blocker reserved for clear user/clinical harm |
| v1.28.0 | ui-copy reviewer extends to **CLI/TUI copy** via a PROJECT-PROFILE `## User-Facing Copy Paths` opt-in | #134 | Covers non-web patient/agent copy (repo-C system prompts) while keeping backend PRs $0 |
| v1.28.1 | ui-copy dispatch gate no longer treats internal `docs/` as user-facing (narrowed to help/content/faq) | #136 | Removes false dispatch on eng-doc PRs |
| **v1.29.0** | `review_mode` resolvable from a caller **`AIR_REVIEW_MODE` variable** (variable wins over input) | #138 | Lets a caller flip full/solo/both from Settings — fixed `both` silently never running on the work repos (callers forced `\|\| 'full'`) |

---

## Since v1.13.0 — delivered + active (2026-05-23 → 06-06)

The five arcs that dominated the last two weeks. Each links to the Shipped rows above.

### A. Memory-store migration — COMPLETE fleet-wide
The "wiki split" top-backlog item (40-60% of session input tokens) shipped as a per-repo Anthropic memory store as source of truth. **All four repos are now store-backed** (repo-D pilot v1.17.0 #90 → repo-A 2026-06-03 → repo-B + repo-C). Shape: per-author `/authors/<login>.md`, 100KB byte-chunking (#99), review sessions mount **read-only** with deterministic post-review writes (`pattern_writer.py` — kills the injection-poisoning write path AND exact-string-replace fragility), counter at `/meta/air-meta.json` (sha256-preconditioned, no push races). **Pattern A targeted retrieval** (#105, v1.19.0) layered on top — agents grep pattern files instead of whole-reads. Net: directly attacks the dominant cache-read cost lever (was ~68% of spend). Store discovery by name `air-patterns <owner>/<repo>` IS the rollout flag.

### B. Deterministic store→wiki mirror render — SHIPPED v1.22.0 (#119)
`managed/render_store_to_wiki.py` (the inverse of the migrate split) replaces the in-session AI wiki render the `air-learner` used to do. Runs **throttled per-review** (≤1×/hr, gated by `meta.py mirror-due` — a cheap meta read, git push only when stale) + **authoritatively after each learn** curation. The AI learn shrinks to store curation + REVIEW-HISTORY only. **Measured:** post-merge learns dropped ~40% output (29K/21K vs pre-merge 59K/47K — the in-session render is gone); confirmed live on repo-A (`last_mirror_render` stamping). Verified lossless + delete-nothing against all 4 live wikis before merge. **Managed-only** — CLI store-awareness is the deferred next phase (CLI reads the mirror fine; its writes are non-authoritative and get overwritten by the next render).

### C. Solo / both review mode — SHIPPED v1.21.0 (#117), BENCHMARKING
`AIR_REVIEW_MODE` (or `review_mode` input / `review.py --mode`): `full` (6-agent coordinator, default + only gate-safe), `solo` (one agent, all lenses, ~3× cheaper/faster), `both` (full gates + solo posts alongside for comparison). **Measured per-session (repo-A #1025, 2026-06-05):** full ~$5 / 15 min wall, solo ~$2 / 3.5 min — ~60% cheaper, ~4× faster. `both` runs them concurrently (wall ≈ full; pays full+solo, no time saving — it's a measurement mode). **Gate-safety finding (3 both-mode PRs):** solo caught the same blocker on #1025 but **missed 2 of 3 mediums (incl. a PHI one) and downgraded a PHI medium to a nit** — confirms "not gate-safe on substantial PRs"; competitive-to-better on trivial PRs (#395) and re-review delta-tracking (#1018). **Active decision:** collect ~10 both-mode PRs (esp. with live blockers/mediums) → adopt adaptive routing — **full for initial/large, solo for re-review/small** — if the data holds. Default stays `full`.

### D. Multi-reviewer PAT model — SHIPPED on callers (+ an ops gap)
Caller workflows resolve the **requested reviewer's** `<STEM>_PAT` from an `AIR_PAT_MAP` repo variable (`secrets[format('{0}_PAT', stem)] || AIR_BOT_TOKEN`), with the **`expected_reviewer` assertion** (v1.20.0 #116) closing the trust seam. Live on repo-A/repo-B; **repo-C ported 2026-06-05** (PR #228, was on the legacy bare-`AIR_BOT_TOKEN` caller).
**OPS GAP (caused a 2-day repo-C outage):** the weekly `rotate-air-pat.sh` fan-out refreshes the per-reviewer `<STEM>_PAT` secrets but **NOT `AIR_BOT_TOKEN`**, and **intermittently skips repo-C entirely** — repo-C's `AIR_BOT_TOKEN` went stale (06-03→06-05, every review 403'd at checkout), and the rotated `<STEM>_PAT`s landed on the other repos in the fan-out 06-05 but not repo-C. **Fix to track:** add repo-C to the fan-out for all `<STEM>_PAT` + refresh its `AIR_BOT_TOKEN`; until then only one reviewer's PAT works on repo-C and the others spend-then-fail. Cheap detector: a lone-stale `AIR_BOT_TOKEN` `updated_at` vs the fleet.

### E. Reliability + ops (v1.16.0–v1.20.0)
Fail-loud on run-failed outcomes + **billing canary preflight** (v1.17.0 #87 — billing exhaustion no longer leaves CI silently green); cooldown debounce + respond-driven re-request (v1.16.0 #86); inline-MODE default + Codex bwrap fix (v1.19.x); pre-post dedup against double-triggered runs (#109). The 06-03→06-05 repo-C 403s cost **$0** (failed at git-checkout, before any session) — confirms the preflight ordering.

### F. MCP Tunnels — FUTURE IDEA (captured, unscheduled)
Give the managed reviewer internal-context MCP tools (DB-schema / contracts / scanners) over a private tunnel so reviews reason against live internal context. Full-mode-only, post-learn, pilot locally first. Not scheduled; tracked so it isn't lost.

---

## Current production data

### Wall time (10 successful runs, May 1-2 2026)

| Repo | Avg | Range | Notes |
|---|---|---|---|
| repo-A | 31.7 min | 24.6 – 42.3 min | All on PR #635 (5-collection mongo import, 1380-line diff) |
| repo-B | 18.9 min | 13.0 – 26.3 min | Spread of PR sizes |
| repo-B with Opus verifier (pre-tier) | 39.4 min | n=1 | **Same PR diff, Sonnet ran 3× faster** |

### Coordinator session decomposition

```
codex (GHA-side)          ~35-45s     bounded; rarely the bottleneck
setup.py + checkout       ~5-15s      flat overhead
coordinator session       18-40 min   ⟵ dominant
  ├── 4 specialists in parallel (~slowest stragger ~5-15 min)
  ├── verifier (sonnet)   ~3-8 min
  ├── wiki bash update    ~10-30s
post review + verdict     ~2-5s
learn.py epilogue (15/PR) ~3-10 min   only every 15 reviews / 14 days
```

### Re-review density is the new norm

PR #635 received 9 reviews over 2 days. Comment sizes converged but not monotonically: 8.2KB → 6.7KB → 4.1KB → 6.0KB → 9.5KB → 5.7KB → 6.3KB → 4.6KB → 2.4KB. Specialists flag *different* things on similar diffs — variance, not just convergence.

### Failure profile (updated 2026-06-05)

| Mode | Recent count | Status |
|---|---|---|
| Successful | 36+ in last 7d | normal |
| `Skipped` (own-PR, closed-PR, race-with-merge) | many | working as intended |
| Cancelled (race-with-push, concurrency cancel) | 19 in last 7d | `cancel-in-progress: true` working as designed |
| `422 Validation Failed` posting comment | 0 in last 7d | v1.12.1 retry path holding |
| Stale-coordinator regurgitation | 0 in last 7d | v1.12.3/v1.12.4 mitigations holding |
| Billing exhaustion | 2026-05-22 (air key, 11d invisible) + 2026-06-02 (org key, ~4h, 3 repos) | run-failed comment posts but job stayed green → fail-loud + billing canary preflight shipped post-1.16.0 |
| **SSE-quiet / coordinator dispatch latency** | 3+ in 3d (2026-05-19→21) | see "coordinator dispatch latency" below |
| **repo-C stale-token outage** | 06-03→06-05 (every repo-C review 403'd at git-checkout) | rotation fan-out skipped repo-C AND never refreshes `AIR_BOT_TOKEN` → stale token. Fixed by porting repo-C to the multi-PAT caller (PR #228, uses a freshly rotated `<STEM>_PAT`); fan-out gap itself still OPEN — see §Since-v1.13.0 D. Cost: $0 (failed pre-session) |

### 2026-06-02 session-efficiency analysis (repo-C #216 re-review)

A managed-session self-analysis + raw event audit of one re-review session (~3.7M input / 86K output tokens) produced a triaged efficiency backlog:

**Shipped (this PR — combined with cross-PR awareness):** scoped-search + timeout-retry + note-the-gap discipline in code-reviewer/security-auditor (a single unscoped search timeout cost ~10 min wall AND expired the 5-min prompt cache for every later turn); verifier treats declared gaps as unverified; coordinator idle-wake hygiene (measured: 4 wakes × ~40K cache-reads — pennies, but free to fix); per-pattern narrative caps in both learn flows.

**Top backlog item — wiki split + structured appends (est. 40-60% of session input tokens):** REVIEW.md (132K chars on repo-C) is loaded by the coordinator + 2-3 specialists + the verifier every session; the verifier already fails to read it (110K tool-output cap), silently degrading the accepted-patterns whitelist. **DONE (fleet-wide) — see §Since-v1.13.0 A.** Shipped as the memory-store migration (PR A, repo-D pilot v1.17.0): per-repo Anthropic memory store as source of truth (per-author `/authors/<login>.md` files, 100KB/memory cap enforces the split), review sessions mount read-only with deterministic post-review writes (`pattern_writer.py` — kills both the injection-poisoning write path and exact-string-replace fragility), learn mounts read-write to curate it (the git-wiki mirror is now rendered **deterministically post-session**, not in-session — v1.22.0), counter moves to the store with sha256 preconditions (no more push races). **All 4 repos migrated** (repo-D → repo-A 2026-06-03 store memstore_01SWmkzjVhLGFfPwLPM8xaH6 → repo-B + repo-C), repo-A after a one-time chunked wiki cleanup (glossary 261→171KB all terms preserved, history 553→379KB, profile 173→119KB).

**Additions from the 2026-06-02 session-event audit (second pass):**
- **Coordinator wake batching** — the per-specialist-completion wakes are runtime behavior (~33K cache-reads × 4-6 wakes ≈ $0.25/run after the prompt-side hygiene). Check whether the multiagent roster supports wake-on-all-complete; if not, file as Anthropic feedback. Acceptable waste, not urgent.
- **Pre-install repo deps in the managed environment** — code-reviewer spent ~22s on `pip install` + pytest inside a session. NUANCE: the environment is currently SHARED across all repos (`air-review-env`), so per-repo dep preinstall needs either per-repo environments or a base-image story — design note, not a quick patch.
- **simplify wall-time** — re-measure after the store pilot before any tier change ("switch simplify to fast" is not possible: fast mode is Opus-only). The giant wiki reads are the suspected root cause of simplify's 5+ min runs; PR A should fix it for free.
- **PR B (file handoff) — BLOCKED ON RUNTIME (shipped v1.18.0 as experimental, off by default):** implementation complete (Files-API mounts at `/workspace/context/`, pointer delegations, `/workspace/findings/<name>.md` handback, simplify inline carve-out, inline fallback) but the live verification run (air #92 replay, run 26855698173, session sesn_01BmuyMmoVUP6xeaWWNXW9pM, 2026-06-03) proved **callable-agent threads run in isolated containers**: `file` session resources don't appear in sub-agent thread containers (verifier found `/workspace/context/` absent while `/workspace/repo` — a github_repository resource — was present), and one thread's `/workspace/findings/` writes are invisible to siblings. Specialists improvise when pointed at non-existent paths (simplify reviewed a hallucinated Go PR; two specialists ack'd writes nobody could read). Fail-loud caught it (no bad review posted; structured run-failed comment + exit 1). Gated behind `AIR_FILE_HANDOFF=1` pending runtime support — **file as Anthropic feedback**: thread-shared workspace (or file-resource propagation to threads) is the unlock for cheap multi-agent handoff. Re-verify with a closed-PR `fresh=true` dispatch before re-enabling. Positive side-finding from the same run: bash-equipped specialists executed the heredoc-write + one-line-ack protocol flawlessly in their own containers.

**Opus→Sonnet on code-reviewer/security-auditor — BENCHMARKED 2026-06-03, VERDICT: STAY ON OPUS (revisit only under cost pressure).** Ran `bench/sonnet-tier` (both agents `model: sonnet`, no fast) as local dry-run replays of repo-A #703, repo-C #216, svc #84 against `air@bench/sonnet-tier` in a quiet window (retier hit the SHARED production agents — the workspace is unified across all repos, NOT isolated as first assumed; production was protected only by running in a verified-quiet window + immediate resync to Opus-fast v30, confirmed no production CI review fired 01:46–02:39 UTC; nothing posted — bench coordinator had Part B disabled). LESSON: a quiet window is MANDATORY for tier benchmarking, not optional — there is no per-key workspace isolation. Findings:
- **Only svc #84 was a controlled same-SHA comparison** (`619fdad1`). Sonnet retained 3/5 Opus findings, **missed 2** (CI-glob working-dir assumption [low]; lazy-import transitive-cache [pre-existing]), and added 1 valid new one (temp-file hygiene). A real — if low-severity — coverage regression.
- **repo-A #703 and repo-C #216 heads had moved past their baselines** (more commits landed after the reviewed SHA), so they were NOT controlled. They still showed Sonnet is *competent*: no hallucination, deep cross-file/PHI reasoning (repo-A: soft-deleted-PHI-before-filter medium, `patient_vpc_id` unhashed-forward; repo-C: surfaced 2 blockers + a medium reasoning across deploy.sh ↔ CLAUDE.md ↔ PROJECT-PROFILE).
- **The strict bar (zero missed blockers/mediums) was never exercised** — no controlled comparison contained a live blocker/medium. Closed-PR-at-HEAD replay structurally can't test medium-catching: the issues that earn those findings get fixed, so they're gone at HEAD (repo-A's 3 original mediums were all FIXED by its final head).

Decision rationale: the bar is a gate Sonnet must *clear* (demonstrate zero missed mediums/blockers); it wasn't cleared because it wasn't testable, and the one controlled data point regressed. Opus stays the default on the two highest-signal agents. Sonnet is clearly usable, so this is "insufficient controlled evidence to drop a tier," not "Sonnet is bad."

**Proper follow-up if cost pressure rises** (the dominant cost lever was always cache-read VOLUME — addressed by the store migration — not tier; Sonnet $3/$15 vs Opus-fast-on-managed $5/$25 ≈ 40% off only those two agents' tokens): build a harness to replay PRs at their **pre-fix SHA** (e.g. repo-A #703 @ `0fcf7cdb`, where mediums #1 index-order / #2 N+1 / #3 PHI-depth were live — baseline captured at `/tmp/air-bench/baseline-repo-A-703-FIRST.md` during the run) so the medium bar is actually exercised under a controlled same-SHA diff. `review.py` reviews the live PR head only, so this needs a SHA-override path (or `--re-review` against the pre-fix SHA). Until then, Opus.

**Declined (caching math):** file-handoff's INPUT-side rationale — content enters the reader's context either way; only saves coordinator replay at 0.1× cache-read rates (pennies). (The OUTPUT-side rationale — the coordinator re-typing 16K tokens per run — is what PR B shipped on.) System-prompt trimming / shared-skill extraction — cache-written once per thread, marginal. Checklist-from-file — the read tool loads the same tokens. Skip-specialists-on-small-diffs — declined on policy ("a 1-line PR can have a blocker" is a core design rule), and the cited session's own shell+docs PR is where the security auditor had found the supply-chain gap one round earlier.

**Verify after repo-C #220 (pin bump):** Codex was silently absent in the session (`bwrap: loopback` sandbox error on the runner) — repo-C reviews run 4-reviewer until the pin lands on v1.16.0; if the error persists, open a bug.

### Selective context retrieval + observability (HIGHEST cost lever — 3 tracks)

Measured 2026-06-03 (sessions API, 30d): **cache-read is ~68% of managed spend** (~4,800M tokens × $0.50 ≈ $2,400 of ~$3,540), driven almost entirely by loading whole wiki files (repo-A glossary 261KB, project-profile 173KB, REVIEW-HISTORY 553KB) into the coordinator + 4 specialists + verifier EVERY review. The bloat-cap work (#99/#101/#102) makes those files *correct and smaller*; **selective retrieval makes their size stop mattering** — a perfectly-trimmed 120KB glossary still costs when loaded 5×. This is a bigger lever than the model-tier question (declined) or file-handoff. Three independent tracks:

- **Track 1 — Pattern A: lazy / targeted reads — SHIPPED fleet-wide (#105).** Agents grep the large pattern files for diff terms instead of reading whole; ACCEPTED-PATTERNS kept WHOLE (suppression is category-keyed; a self-review caught that grepping it misses concept-keyed entries → FPs). Watch next reviews for FP/recall (glossary naming-convention edge). Original design note: Change the specialist + verifier prompts from "read GLOSSARY.md / REVIEW.md whole" to targeted pulls: `grep` the glossary for terms appearing in the diff; read only `authors/<this-PR-author>.md`; read the pattern category matching the changed file types; read the small files (accepted-patterns, severity-calibration) whole. The agents already have Read/Grep/Glob on the mount, so this is a PROMPT change with no new infra, and it works on **either** backend (wiki git mount or `/mnt/memory` store) — and even on a *bloated* source, since `grep` returns only matched lines, not the whole file (so it's independent of both the cleanup and the store migration). Risk: recall — does a targeted grep surface everything a whole-file read would? Prototype = measure cache-read drop AND finding-set retention vs a baseline repo-A review.
- **Track 2 — Pattern B: a Haiku "librarian" callable sub-agent.** The coordinator dispatches a cheap retriever first; it reads the diff + searches the store and emits a compact (~2KB) "relevant context pack" (matched glossary terms, applicable author patterns, the accepted-pattern, the review-focus rule for the touched paths). Specialists get the pre-filtered pack instead of the raw store — one retrieval shared by all 5 (no per-agent grep duplication). Adds one serial pre-step + an agent to maintain; one agent's recall gates all. Adopt only if Pattern A's per-specialist duplication or recall proves insufficient on the heaviest repos.
- **Track 3 — Observability sink (NOT "Outcomes" — see correction below).** Record a structured per-review record (tokens in/out/cache, $, active_seconds, per-agent timing, stall events, verdict, findings-by-severity, verifier confirmed-vs-dropped, later developer disputes, re-review round, trigger type + cooldown hit). Tonight's cost+waste analysis was ad-hoc archaeology against `sessions.list()`/`.retrieve()`; a sink makes it repeatable. **Source = session usage data (sessions API) + a per-review record we write ourselves** — there is NO native telemetry primitive. **Store it EXTERNALLY, not in the agent-mounted memory store**: it's append-heavy, agents never read it, and the 100KB-per-memory cap makes append-logs awkward. Destination options: a committed metrics file / GitHub Actions artifact / external DB — anywhere we query, not where agents mount. **Closed loop:** SEVERITY-CALIBRATION is *derived* from dispute rates — feed the record into it so calibration self-updates instead of being recomputed each learn pass.

  **Correction re: "Outcomes" (researched 2026-06-03, [cookbook](https://platform.claude.com/cookbook/managed-agents-cma-verify-with-outcome-grader)):** the platform's **Outcomes** feature is NOT a telemetry/observability sink — it is a rubric-based **grader loop**. You send a `user.define_outcome` event (`rubric` text/file + `max_iterations`); the platform auto-provisions a separate grader agent (same model+tools, fresh isolated context) that scores the writer's output against the rubric after each turn and emits `span.outcome_evaluation_end` events (`result`: satisfied / needs_revision / failed / max_iterations_reached; `explanation`: per-criterion feedback fed back to the writer to drive revision). The grader runs as a **separately-billed session per iteration** (a 3-iteration loop ≈ 6 model invocations). Docs are explicit: usage analytics / token-counting / latency are out of scope for Outcomes.

  **Outcomes fit for air — assessed LOW, deferred.** air already has `review-verifier` filling the "separate grader in a fresh context" role (FP filter, confidence scoring, source re-read, severity calibration). Outcomes' plausible air use is a NARROW output-integrity gate on the final comment (SHA footer matches HEAD, every finding has file:line + severity, no template snippets parsed as findings) with auto-regenerate-on-fail — which would have caught #89 (SHA tail corruption) and the file-handoff malformed-output failure. BUT those are now handled cheaply by fail-loud + the 12-char SHA-prefix fix, so the marginal value is low while each Outcomes iteration adds a billed grader session + latency — the opposite of the cost-reduction goal. Revisit only if we want autonomous review-quality *iteration* (writer revises until a rubric passes), not for observability and not to replace the verifier.

Sequence: Track 1 first (prompt-only, attacks the dominant cost at near-zero risk, measurable on repo-A), Track 3 in parallel (stop doing cost archaeology), Track 2 only if A's recall is weak. The wiki cleanup makes the store correct; Track 1 makes it cheap.

**Storage sizing (researched 2026-06-03).** Memory-store limits confirmed in docs: **≤100KB per memory/file**, **max 8 stores per session** (per-store memory-count cap not documented; earlier notes said ~2000; storage itself appears unpriced in beta — reads bill as ordinary tool tokens). Implications:
- **Context store, per repo (post-cleanup):** the big files exceed the 100KB cap and MUST be chunked (handled by `migrate_wiki_to_store.py`'s byte-chunker, #99). Rough repo-A projection: glossary ~120KB → 2 memories, project-profile ~119KB → 2, REVIEW-HISTORY ~372KB → ~4 (its 222KB Finding-Frequency table dominates), common-findings 34KB → 1, service-patterns 17KB → 1, accepted/severity small, per-author files ~9KB each → ~1 per active author. **Total ≈ 15-25 memories per repo** — far under any plausible per-store cap and well within the 8-stores/session budget (we mount one). Chunking + Pattern A compound: a chunked file means a targeted read pulls one ~95KB chunk at most, and a `grep` pulls only matched lines.
- **The 222KB Finding-Frequency table is the next sizing watch-item:** it's cumulative-by-design (one row per pattern, ~339 rows) and read by git-history-reviewer. It's bounded by pattern count (won't grow like the per-PR narrative did), but it's the single largest context object post-cleanup — a candidate for Pattern A grep-retrieval (pull only rows matching the diff's author/files) rather than whole-file load.
- **Observability data: keep OUT of the memory store.** ~390 reviews/month × ~1-2KB/record ≈ ~0.6MB/month. As append-only JSONL in a store it'd need ~6 chunked memories/month and grow unbounded — and agents never read it. Put it in an external/committed metrics location (Track 3). The agent store holds only what agents read.

### 2026-05-19 → 21 incidents — coordinator dispatch latency

A new failure mode emerged 2026-05-19 evening: coordinator sessions reach `status=running` but emit zero `agent.message` events for the entire poll budget. Events queue server-side and drain only on session termination. Reviews DO complete (real `## Code Review` posted), but at 30-45 min wall vs. typical 18-22.

Examples:
- **repo-B #319** (2026-05-20 10:41) — success after 2619s, `agent_msgs=0` through full REST poll, drained at session end
- **repo-C #184** (2026-05-21 17:25) — success after 1453s, `agent_msgs=0` for the entire 1246+s poll window
- **repo-A #851** (2026-05-21 18:06) — still in-progress at 49 min as of audit
- **repo-C PR #184 lost ~30 min** to operator-driven cancellations (3 cancelled attempts before 4th left alone long enough to complete)

**Likely root cause:** Anthropic-side SSE event delivery is degraded. Work proceeds server-side; live delivery to GHA-side workflow is broken. Reviews completing despite zero in-flight events points to server-side queueing.

**Implications for Phase 0:**
- The originally-drafted `agent_msgs=0` early-abort (review §D.2 in plan-review) would have **falsely aborted** these successful runs. Revised version: log an informational note ("SSE delivery degraded — reviews taking ~30-45 min today vs usual ~22 min. Do not cancel.") instead of aborting.
- Operator visibility is the real problem to solve, not abort logic.

### Coordinator regurgitation hypothesis (from repo-D #37 — historical)

After three reproductions on svc-tx #37 (15+ re-review rounds on same PR) and failed cache-bust:

**Hypothesis:** The coordinator regurgitates `prior_review_body` from its user-message context instead of dispatching specialists. 92.5s wall is enough for ~one model turn — not the 3-turn protocol. Coordinator likely "recognizes" the heavy prior-PR-specific body and short-circuits TURN 1, emitting TURN 3 that copies the prior body including its prior-SHA footer.

Supporting evidence:
- Coordinator wall on failures: 92.4s, 92.5s, 92.5s (highly consistent, ~one model turn)
- Failures PR-specific: only svc-tx #37 hit it
- `Reviewed at:` SHA in broken outputs always matches most recent prior bot review's SHA
- Cache-bust commits DID NOT recover — rules out Anthropic prefix-cache
- PR-restart workaround (close + reopen identical branch as fresh PR, removing re-review codepath) DID recover

This is a **model-behavior issue**, not caching. Fix is on prompt/orchestrator side. See P0-NEW below.

### Hidden observability gap

`managed/review.py` prints phase markers to block-buffered stdout that only flushes when the script exits. GHA log timestamps all markers at **the same instant** — script-exit time. If a run hangs at minute 35, live log shows nothing past `[1] Syncing...` until the watchdog kills it. See P0.

---

## Phase 0 — Audit-derived fixes (ship NEXT)

Three small fixes derived from the 2026-05-19/20/21 audit. All client-side patches in `managed/review.py` + `managed/api.py`. No new managed-agents features. `<1 day total.`

**Status (2026-06-06):** A1/A2 (full coordinator dump + SSE-degraded info log) effectively subsumed by the v1.12.3–v1.12.6 REST-drain work + v1.17.0 fail-loud. **C10 still OPEN** — `managed/api.py` still sends `managed-agents-2026-04-01-research-preview` (verified 2026-06-05; the research-preview surfaces we rely on, e.g. callable-agent threads + the sessions usage API, still work, so no forcing function to drop the suffix).

### A1 — Capture full `coordinator_out` on SHA-mismatch
**Why:** Today only first 2000 chars dump on failure. repo-A #830 (2026-05-19) had a substantive 4715-char Re-review rejected by SHA-validator; without full dump we can't tell if footer was absent, stale, or regex-missed.
**Fix:** ~5 LOC change to dump full `coordinator_out` to a debug artifact instead of truncating.
**Source:** `air-improvements-plan-review.md §D.1`

### A2 — SSE-degraded informational log (revised — NOT abort)
**Why:** Original draft: `agent_msgs=0` early-abort at t≥300s. **Revised per 2026-05-21 audit:** that would have falsely killed every successful slow run today. Reviews DO complete via REST event drain at session termination.
**Fix:** when SSE-quiet → REST-poll path triggers, log: `"SSE delivery degraded — REST polling. Reviews are taking ~30-45 min today vs. usual ~22 min. Do not cancel; events drain at session termination."` Solves the actual problem (operators cancelling because they assume hang) without false-aborting work.
**Source:** `air-improvements-plan-review.md §D.2` (revised 2026-05-21)

### C10 / P12 — Drop `-research-preview` from beta header
**Why:** `managed/api.py` uses `managed-agents-2026-04-01-research-preview`. Public-beta header is `managed-agents-2026-04-01` (no suffix). Multiagent moved from research preview to public beta May 2026.
**Fix:** one-line change in `managed/api.py`.
**Risk:** non-zero — if research-preview is more permissive than public beta on some surface we depend on, immediate failures. Validate against dogfood PR before rollout.
**Source:** `air-improvements-plan-review.md §C.1`, `legacy P12`

---

## Phase 1 — Performance + safety (~1 day)

### B1 / Item E — Fast mode for Opus (shipped v1.14.0; alias bumped to 4.8 post-1.15.0)
**What:** `{"id":"claude-opus-4-8","speed":"fast"}` model override on `code-reviewer.md` and `security-auditor.md` frontmatter (alias resolved via `MODEL_ALIASES`). Shortens median review time at zero prompt cost — the fast-mode premium is not billed on Managed Agents sessions.
**Verification needed:** local CLI router must honor the `model:` field's object form. If not, gate on managed-agent path only.
**Source:** `air-improvements-plan.md §3.1 E + §3.4 O`

### C8 / Item M — Session metadata — **still OPEN (worth doing)**
**What:** Patch the managed entrypoint (`client.beta.sessions.create`) to set `metadata: {pr_number, repo_path, mode, plugin_version}`. Enables every future ops question — cost per repo/mode/version, failure rate per cohort.
**Status (2026-06-06):** NOT shipped — sessions still carry an empty `metadata: {}` (confirmed via the sessions API). The 2026-06-05 cost reconciliation (per-session usage → $/day, full-vs-solo) was ad-hoc archaeology against `sessions.list()` + `.retrieve()` keyed off the session **title** (`air-coordinator — <repo>`); structured `metadata` would make it a filter, not a parse. Cheap; compounds with Track 3 observability.
**Cost:** trivial.
**Source:** `air-improvements-plan.md §3.3 M`

**Dropped from Phase 1** (vs. original plan):
- **Item D thread interruption** — deferred entirely; no specialist-level hangs observed in audit (only coordinator-level, which interruption doesn't help)
- **Item L `always_ask` wiki bash** — re-scoped to medium under Safety (F1) because plumbing `--dry-run` from GHA → coordinator is harder than rated, and no production occurrence yet

---

## Phase 2 — Structured findings + outcomes (SEQUENTIAL, 6-10 weeks total)

**Sequencing rule:** B + A + C all touch the same orchestrator surface (coordinator + verifier). Regressing any one affects every review. Ship each ALONE, observe ~5 production PRs, then ship next.

### 2.a — Item B: Custom tool `record_finding` (3-4 weeks, ship FIRST)
**Why:** Today's specialists return free-text markdown; verifier parses titles like `[matches author pattern: X (3x)]` via regex. A `record_finding` tool returns typed data, eliminates regex-parsing failure class, makes wiki updates mechanical.

**Scope** (re-costed from plan's ••• to ••••):
- 4 specialist prompts rewritten to emit tool calls instead of markdown
- Verifier parsing logic rewrites
- Wiki-update logic in `review.md` rewrites (currently parses markdown)
- **Rendering layer added** — user-facing `## Code Review` markdown body still needs to be rendered FROM the structured data. `Strengths`, `Reviewed at: <sha>` footer, `[already raised by @<author>]` all flow through this.

**Schema sketch** in `air-improvements-plan.md §5.2`.

**Source:** `air-improvements-plan.md §3.1 B + §5.2`, `plan-review §B.2`, `§E.2`

### 2.b — Item A: Outcomes + rubric (then, 2-3 weeks)
**Why:** v1.12.6's `\b` word-boundary bug (PR #67) shipped because we lacked an automated structural check on verifier output. A 5-criterion rubric (`## Code Review` opener, `Reviewed at: <40hex>` footer + SHA match, has at least one severity section, no `[empty message]` text) catches the class AT SESSION END. Pattern 1 in 2026-05-19 audit (repo-A #830 silently discarded 4715-char review) is exactly the bug class.

**Status:** research preview → public beta May 2026. Needs Outcomes access request first (`https://claude.com/form/claude-managed-agents`).

**Rubric draft** + wiring sketch in `air-improvements-plan.md §5.1`.

**Source:** `air-improvements-plan.md §3.1 A + §5.1`, `plan-review §E.1`, legacy P11

### 2.c — Item C: Persistent worker threads (then, ~2 weeks)
**Why** (revised motivation per `plan-review §B.5`): the original use case ("verifier asks specialist: are you sure this is pre-existing given new caller?") is already solved by v1.13.0's exposure-escalation clause. **New motivation:** verifier-driven evidence interrogation on borderline `[matches author pattern: X (Nx)]` annotations — "what evidence supports your blocker call here?" Specialist thread is already cache-warm; follow-up costs only new content tokens.

**Protocol extension** sketched in `air-improvements-plan.md §5.3` (TURN 2.5 — conditional, 1 round cap).

**Source:** `air-improvements-plan.md §3.1 C + §5.3`, `plan-review §B.5`

---

## Phase 3 — Wiki + MCP refactor (3-4 weeks)

### Item G — Skills-based wiki loading (~3 weeks)
**Why:** Every specialist eagerly loads every wiki file × 4 specialists = same content in 4 separate context windows per review. Skills API loads metadata in system prompt (~500 tokens once), then each agent `bash cat`s the specific section it needs. Cuts ~5-15k tokens per specialist invocation on chatty wikis.
**Source:** `air-improvements-plan.md §3.2 G`

### Item H — Codex as MCP server (large)
**What:** Move codex from `--no-codex` bash shellout in `review.md` to an MCP server registered in the coordinator's agent config. Removes ~100 LOC of codex-shellout bash; codex becomes a structured tool call with results streamed into `agent.mcp_tool_use` events.
**Needs:** MCP shim authored.
**Source:** `air-improvements-plan.md §3.2 H`

### Item I — Files API session-scoped artifacts (medium)
**What:** Write verifier output, raw specialist findings, codex findings to `/mnt/session/outputs/` so they're retrievable post-hoc via `scope_id=session_id`. Use for the local cache in `~/Documents/reviews/<repo>/...` instead of pulling from PR comments.
**Side benefit:** removes the wiki-push-failure recovery branch in `coordinator.md` — artifacts are durable independent of wiki push state.
**Source:** `air-improvements-plan.md §3.2 I`

---

## Phase 4 — Versioning + scale-out (long horizon)

### Item F — Agent versioning (medium-large) — SHIPPED (v1.18.0, simplified shape)
**Why:** Air's coordinator + specialist prompts evolve with every plugin release. Today an update affects all repos simultaneously (no canary). Pinning enables canary rollouts + rollback.
**Shipped as:** `agent_versions` JSON input on `managed-review.yml` (not `.air-config`) → `AIR_AGENT_VERSIONS` env → `setup.py` skips prompt sync for pinned agents (`parse_agent_pins` fails loudly on malformed input) + `review.py` overrides roster versions. Work repos pin the blessed set from release notes (capture snippet in `managed/README.md`); air floats. `air-learner` not pinnable. Pin the whole set from one release — a pinned coordinator's sub-agent roster is whatever its pinned version recorded.
**Still open:** per-repo `.air-config` discovery (callers currently pass the pin in their `with:` block) and the workspace-vs-per-user question (`plan §6 Q3`) — revisit if multiple orgs adopt.
**Source:** `air-improvements-plan.md §3.1 F + §4 Phase 4`

### Item J — Self-spawning coordinator for monorepo PRs (••••, large)
**Why:** Biggest weakness today is huge PRs (50+ files spanning services). `{"type":"self"}` in roster lets coordinator fan out: one sub-coordinator per service, each running 4-specialist pipeline against its slice. Aggregate verifier consumes sub-results.
**Trigger:** PR with `changedFiles > 50` OR distinct top-level directories `> 3`.
**Source:** `air-improvements-plan.md §3.2 J + §4 Phase 4`

### Item N — Self-hosted sandbox for PHI repos (••••, conditional)
**Why:** If any fleet repo carries regulated data (e.g. PHI) in diffs, running on self-hosted sandbox means the diff never traverses Anthropic infra. Required for HIPAA-adjacent workloads.
**Open question** (`plan §6 Q2`): does any repo currently routed through air carry such data in diffs?
**Source:** `air-improvements-plan.md §3.3 N + §4 Phase 4`

---

## Phase 5 — Reframed P0-P9 priorities (from prior roadmap)

Sorted by **value × evidence × cost-to-ship**. Each has a concrete trigger.

### P0-NEW — Coordinator regurgitation diagnostic + retry
**Status:** Partially shipped (v1.12.1 defense, v1.12.3-v1.12.6 mitigations). Root-cause retry-on-regurgitation not yet shipped.
**Two-step fix:**
1. Debug logging (v1.12.2 shipped) — first 1000 chars of `coordinator_out` on SHA-mismatch
2. **Detection + retry mechanic** (NOT yet shipped) — if first coordinator returns in <300s AND output footer SHA matches `prior_sha`, retry the coordinator session WITHOUT `prior_review_body` (degrades to fresh-review codepath for retry). Tradeoff: loses FIXED/NOT-FIXED classification — acceptable.
**Alternative rejected:** strengthening verifier_task prompt with imperative SHA instruction — won't help if coordinator isn't reaching verifier in regurgitation mode (92.5s isn't enough for 4-specialist + verifier dispatch).
**Evidence:** 3 production failures on svc-tx #37 (runs 25367689850, 25368789413, 25369351035), all at ~92.5s, all with prior-SHA footer. Cache-bust failed; PR-restart succeeded.

### P0 — Live progress flush (~1 day)
**Problem:** stdout is block-buffered; users can't tell if a 30-min run is making progress or hung. Today this gets confused with actual hangs.
**Fix:** add `flush=True` to all `print()` calls in `managed/review.py` phase markers + run with `python -u` in `managed-review.yml`. Zero risk, zero cost.
**Compounds with:** A2 (SSE-degraded info log) — both improve operator visibility.

### P1 — Post-failure recovery
**Status:** Path A + B shipped v1.12.1; Path C deferred to P0-NEW step 2.

### P2 — Re-review fast path (~3 days, biggest user-perceived win)
**Problem:** PR #635's 9 re-reviews each ran the full 4-specialist + verifier loop on entire diff. Re-reviews on a 50-line inter-diff don't need code-reviewer/simplify/security-auditor reasoning over the full PR Context — they need prior-finding classification + new findings on inter-diff only.
**Fix:** introduce re-review-specific coordinator prompt:
1. Fans out specialists on **inter-diff only** (not full PR)
2. Skips git-history-reviewer (blame/churn didn't change)
3. Runs verifier with prior-findings classification as primary task
4. Targets 5-10 min total instead of 30
**Backtest:** PR #635 has 9 reviews of ground truth.
**Evidence:** PR #635's last review (2.4KB, all FIXED + 2 nits) took 23.6 min — same as first review. Work was 5× simpler; cost unchanged.

### P3 — Cache stable context across re-reviews (~1 week, compounds with P2)
**Problem:** every coordinator session re-loads PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, REVIEW.md, SEVERITY-CALIBRATION.md fresh.
**Fix:** put wiki + PROJECT-PROFILE in stable prefix at start of user message; add `cache_control: ephemeral` breakpoints. Instrument `usage` per turn; report cache_read vs cache_create ratios.

### P4 — Auth-handler / config-only PR fast path (~1 week, niche)
**Fix:** if `additions + deletions < 50` AND no security-sensitive paths touched (from PROJECT-PROFILE), skip security-auditor + git-history-reviewer.
**Risk:** missing a security issue in a tiny PR. Mitigation: conservatively define "security-sensitive paths".

### P5 — Move codex into coordinator (eval-only) — **probably don't do**
35-45s wall savings not worth 2.5× coordinator cost. Re-evaluate only if Anthropic ships parallel-tool support on Sonnet.

### P6 — Codex prompt tightening (~1 day, modest)
Measure citation rate over 20 runs. If <10%, evaluate dropping codex. If >30%, tighten prompt.

### P7 — Wiki epilogue dispatcher (~1 day, latency-only win)
**Fix:** dispatch `learn.py` as separate `workflow_dispatch` job (or `needs: [review]` sibling job) triggered after review posts. User-visible review latency stops at "Posted: ..." instead of dragging through learn.py.
**Evidence:** 42-min run (`25237782482`) was 30 min coordinator + 10 min learn.py.

### P8 — Cross-repo benchmark publication (~1 day)
**Status:** `air_ref` shipped v1.11.0. Set up weekly cross-repo run on known-good repo-A PR fixture; publish cost / wall time / finding count parity to wiki page.

### P9 — Self-review deferrals from v1.12.x stack
**P9-a** — `terminated_reason` consumer-coupled contract comment + structured error pair. Trigger: any PR changing `run_session` format.
**P9-b** — REST poll API-call amplification (~160 calls/stuck session). Circuit breaker on consecutive empty drains + exp. backoff. Trigger: >20% reviews hit stuck-poll path.
**P9-c** — `_drain_via_rest` no-progress observability (drain every Kth iter; surface `open_threads`). Trigger: next long-running session debug.
**P9-d** — `error!r` repr may leak fields if SDK adds `request_id`/`account_id`. Post `f"{error.type}: {error.message}"` instead. Trigger: SDK schema bump.
**P9-e** — Stale `## Code Review` substring in else-branch fallback prose. Rephrase to "without a recognised header". Trigger: next `pr_conversation.py` touch.
**P9-f** — Self-referential `VorobiovD/air` link in dogfood failure comments. Conditional suppress when `args.repo == "VorobiovD/air"`. Trigger: dogfood failures become noisy.
**P9-g** — `_run_failed_body` helper to dedupe three branches. Trigger: adding 4th failure branch.

---

## Phase 6 — Managed-agents platform features (P10-P13 + capability uptake)

### P10 — Webhooks for session lifecycle
**What:** Anthropic shipped webhook delivery for managed-agents (May 7, 2026). Events: `session.status_idled`, `session.status_terminated`, `session.thread_idled`, `session.outcome_evaluation_ended`. Push-based, HMAC-signed (`X-Webhook-Signature`).
**Why it matters:** the entire SSE/REST race we fixed in v1.12.3→v1.12.6 goes away if orchestrator just waits for `session.status_idled` webhook + one REST `events.list` call.
**Why not now:** requires publicly-resolvable HTTPS endpoint. GHA is ephemeral. Two viable architectures: thin webhook receiver (Worker/Lambda/Vercel) writing to S3/KV, or polling-fallback retained alongside webhooks.
**Trigger:** 7th SSE/REST class bug, OR Anthropic deprecates SSE, OR notifications outside GHA job's lifetime needed.

### P11 — Outcomes for verifier-output quality gates (= Item A, see Phase 2.b)
Folded into Phase 2.b above.

### P12 — Drop `-research-preview` from beta header (= Item C10, see Phase 0)
Folded into Phase 0 above.

### P13 — New `multiagent` config shape
**What:** Anthropic introduced `multiagent: {type: "coordinator", agents: [...]}` shape. Our code uses legacy `callable_agents: [...]`. New shape adds `{"type": "self"}` for self-call (relevant for Item J monorepo work).
**Trade-off:** cosmetic refactor in `managed/setup.py::create_or_update_agent`.
**Trigger:** Anthropic announces deprecation of legacy shape, OR Item J ships and needs self-call.

### Items K + L (small managed-agents items)
- **Item K** — `agent.thinking` event surfacing to debug log. Today the dashboard sees only `agent.message` (final output). Streaming `agent.thinking` gives reviewers "why did this get flagged" without re-running. Small effort. **Open.**
- **Item L** — `always_ask` tool permission policy on wiki push when `--dry-run` is set. **Re-scoped to medium** under Safety (F1) — needs `--dry-run` plumbing from GHA → coordinator. No production occurrence yet. **Deferred.**

### Items P + Q (underused)
- **Item P** — `tool_use_id` round-trips for `user.tool_confirmation`. Today coordinator's bash commits run unconfirmed; an `always_ask` policy on bash lets a human gate destructive pushes.
- **Item Q** — `span.outcome_evaluation_end.usage` — free per-iteration token telemetry once outcomes are wired.

---

## Phase 7 — legacy review-bot pattern migration

The repo-A wiki memory captures **11 specific failure categories** the bot reliably catches. These are tribal knowledge today; not codified in plugin agents.

**Source:** local project memory (legacy-bot review patterns)

The 11 categories:
1. EXISTS-gate downstream leak (now partially covered by v1.13.0 gate-output symmetry)
2. Sibling gate asymmetry
3. Citation rot (now partially covered by v1.13.0 category-symmetric respond gate)
4. Stale-branch siblings
5. AGENTS.md §6.2 contract violations
6. Test-mock omissions
7. Hardcoded line numbers
8. Allowlist token-family expansion gaps
9. Short-circuit asymmetry
10. Fixture coverage gaps
11. `isActive()` vs enum lists

**Decision needed:** which become hardcoded in `code-reviewer.md` (universal across repos) vs which stay fully wiki-data-driven (per-repo `REVIEW.md` patterns)?

**Effort:** small-medium per pattern.

---

## Conflicts requiring decisions

Items where two sources describe the same thing under different names, or disagree on priority/approach. Resolved positions noted; the conflicts themselves are documented so future revisits have full context.

1. **Outcomes naming (P11 vs Item A vs cost-plan)** — three sources, three names. Cost-plan's "skipped because doesn't save money" superseded by audit (repo-A #830 footer drop is a quality miss outcomes would catch). **Resolved: quality gate, Phase 2.b.**
2. **Fast-mode Opus (Item E vs Item O)** — same change duplicated. **Resolved: collapsed as B1.**
3. **Wiki dry-run gate (Item L)** — plan says small/P0, review says medium/P1. **Resolved: review wins (deferred to F1).**
4. **Thread interruption (Item D)** — plan lists P0 small; audit shows no specialist-level hangs (only coordinator-level). **Resolved: drop from Phase 1.**
5. **`record_finding` (Item B)** — plan rates ••• (2-3w), review re-costs •••• (3-4w). **Resolved: trust higher estimate.**
6. **Persistent threads (Item C)** — plan's motivation overlaps with v1.13.0. **Resolved: revised motivation, de-prioritize behind A and B.**
7. **Two roadmap docs** — `air-improvements-plan.md` and prior `improvement-roadmap.md` covered overlapping items. **Resolved: this consolidated file is now the single source.**
8. **Legacy-bot patterns hardcoded vs wiki-data-driven** — 11 categories live only in repo-A wiki. **OPEN — see Phase 7.**
9. **Cost plan vs new plan on multi-agent** — cost-plan: multi-agent amortizing on Opus+Sonnet, net cost on Haiku-specialists. **Aligned (current arch is Opus+Sonnet).**
10. **Cost plan Phase 2/3** (Haiku-on-specialists A/B) **never had the structured A/B set up.** Quality watchpoint stalled. **OPEN — see Deferred.**

---

## Inventory gaps (NOT yet tracked anywhere)

Things that should be tracked but currently aren't captured in any doc/issue/task:

1. **GHA workflow self-observability** — no metrics on `cancel-in-progress` cancellation rate, Codex install latency, PR-context fetch failures. Only signal today: "human notices bot didn't post".
2. **Prompt regression test harness** — no fixture-based suite running each agent prompt against known PRs to diff findings. P8 is per-PR, not per-prompt.
3. **Legacy-bot pattern → plugin migration matrix** — see Phase 7. No doc tracks which of the 11 patterns are (a) hardcoded in `code-reviewer.md`, (b) flowing through PROJECT-PROFILE, or (c) only emergent from REVIEW.md.
4. **Wiki schema versioning** — `architecture-review.md` raised "no versioning contract between orchestrator and agents". Same concern for wiki files. No migration tool, no schema version field.
5. **Pricing-change resilience** — cost plan anchored on 2026-04-27 Anthropic pricing. No quarterly re-run cadence to validate phase recommendations against current prices.
6. **Org service-account migration trigger** — repo-A/repo-B currently run under a personal PAT pending an org service account. No tracking when DevOps delivers; no PAT-rotation runbook.
7. **`--respond` Step 5e false-positive monitoring** — v1.13.0 added the category-symmetric grep gate. No tracking of FP rate on legitimate single-locus fixes.
8. **External commitments tracking** — see `docs/external-commitments.md`.

---

## Explicit non-fits (won't ship)

- ~~**Memory stores in place of GitHub wiki for `REVIEW.md`**~~ — **SUPERSEDED (shipped v1.17.0→v1.22.0).** The original objection (loses commit history, CLI sharing, public visibility) was resolved by making the store the source of truth AND keeping the git wiki as a **deterministically-rendered mirror** (`render_store_to_wiki.py`, v1.22.0): commit history + CLI reads + visibility all preserved via the mirror, while the store gives the 100KB split + injection-safe deterministic writes. See §Since-v1.13.0 A/B.
- **Dreaming** (research preview) — coupled to memory stores. Now that stores ARE in use, re-assessable — but no identified fit; leave deferred.
- **Thread archival** — relevant only at thread-count pressure. We use 6/25 threads. No-op.
- **Vault credentials with `mcp_oauth` background refresh** — relevant for OAuth-rotating secrets. Our `ANTHROPIC_API_KEY` and `AIR_BOT_TOKEN` are static GitHub secrets. No-op.
- **Finance agent templates** — domain-specific, not our use case.

---

## Deferred / explicit non-goals

- **Phase 3 from cost-optimization-plan.md (parallel_sessions_haiku, $0.63/round).** Skipping multi-agent saves $1.7K/year but loses architectural parity with local CLI. Not worth the divergence.
- **Phase 2 from cost-optimization-plan.md (Haiku on specialists, $4K/year).** Quality watchpoint stalled — never set up the A/B that compares Haiku-specialist findings to Opus-specialist findings. Until that A/B exists, savings are speculative against documented quality risk. (See Conflict #10.)
- **GitLab managed agent.** CLI plugin supports GitLab via `platform-gitlab.md`. Managed agent stays GitHub-only until a GitLab consumer asks.
- **Slack / Confluence integrations** (from expansion-plan §3.4) — Deferred.
- **Paste-diff companion plugin** — Deferred in air's scope (external request, tracked internally).

---

## Phase 4 retrospective lessons (from repo-D #37)

A single PR with 14 review rounds, 13 consecutive CHANGES_REQUESTED, and an eventual two-failure cascade became the dominant data source for Phase 4 + Phase 5 priorities. Seven lessons that bias future phases:

1. **Asymmetric gates are a usability trap.** Fresh review and re-review used to gate on different severity sets (blocker vs blocker+medium). The asymmetry meant a PR could go APPROVED → CHANGES_REQUESTED on a re-review with no new blockers. Fix was structural, not parameter-tuning.

2. **The verifier "knows" enough to break loops, but the prompt didn't ask.** Prior review body has been in context since carry-forward shipped — we just didn't tell the verifier to do anything with the repetition signal. Cheap to add a rule; very effective.

3. **Self-review with --dry-run catches structural bugs that synthetic fixtures miss.** Codex caught the legacy-missing-severity regression all four Claude reviewers missed. Cross-model review at self-review step is high-leverage when the change touches a load-bearing default.

4. **Defensive aborts must produce signal, not silence.** Orchestrator's SHA-validation refused to submit a verdict on stale coordinator output (correct), then fell back to raw-posting the same stale output (wrong). 422 cascade left developer with frozen CHANGES_REQUESTED + no in-PR signal. Whenever we add a defensive check, add a structured "this is what went wrong" comment in parallel.

5. **Coordinator wall-time is a stale-cache signal.** A 92s coordinator run on a real PR is impossibly fast (typical 1500-2400s). When run is short AND output unusable, almost certainly a cached prior-thread response.

6. **"Cached output" was the wrong frame.** Cache-bust commit (whitespace change to README) DID NOT recover svc-tx #37. Coordinator returned same 92.5s + prior-SHA output on next run with different prefix. Rules out Anthropic prefix-cache; points at model-behavior issue (regurgitating `prior_review_body`).

7. **PR-restart is a valid escape hatch.** On long re-review chains where coordinator has degraded, closing the failing PR and reopening from same branch as fresh PR avoids re-review codepath. Workaround loses comment history but recovers merge path. Worth documenting in bot's run-failed comment so users have a path forward.

---

## Decision rule for future phases

Add a row to this doc with: trigger, evidence, fix, risk, expected impact. Ship only when evidence is ≥3 production occurrences. Don't preemptively optimize against synthetic fixtures — the empirical learning loop is the comparative advantage we have over the cost-optimization-plan's experiment harness.

---

## Glossary of phases

- **Phase 0** — Audit-derived fixes (2-3 small items, ship next, <1 day total)
- **Phase 1** — Performance + safety (1 day)
- **Phase 2** — Structured findings + outcomes + persistent threads (sequential, 6-10 weeks)
- **Phase 3** — Wiki + MCP refactor (3-4 weeks)
- **Phase 4** — Versioning + scale-out + self-hosted PHI (long horizon)
- **Phase 5** — Reframed P0-P9 from prior roadmap (mixed status)
- **Phase 6** — Managed-agents platform features (P10-P13 + small Items K/L/P/Q)
- **Phase 7** — legacy review-bot pattern migration (decision needed)

---

*End of master roadmap. All previous planning artifacts now redirect here.*
