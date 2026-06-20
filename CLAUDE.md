# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**air** is a Claude Code plugin for automated PR code review, with an optional Managed Agent mode for CI automation.

The CLI plugin (`plugins/air/`) is markdown files and JSON metadata — no build system or dependencies. The Managed Agent (`managed/`) adds Python scripts for Anthropic's cloud API + a GitHub Actions workflow.

Two CLI commands: `/air:review` (13-step review pipeline) and `/air:learn` (wiki cleanup/regeneration). For CI: `managed/review.py` or the reusable GitHub Action.

## Project Structure

```
plugins/air/
├── agents/              # 6 review specialists + 1 managed-mode coordinator (markdown prompts)
│   ├── code-reviewer.md
│   ├── security-auditor.md
│   ├── simplify.md
│   ├── git-history-reviewer.md
│   ├── ui-copy-reviewer.md    # UI/business-audience copy + static UX/a11y (conditional — UI-touching diffs)
│   ├── review-verifier.md
│   └── coordinator.md         # Managed-mode delegator — fans out specialists via callable_agents (not a reviewer)
├── commands/            # CLI command orchestration
│   ├── review.md              # Main pipeline — the core of the plugin
│   ├── review-self.md         # Self-review flow (--self mode)
│   ├── review-respond.md      # Respond flow (--respond mode)
│   ├── review-solo.md         # Solo flow (--solo mode — one Fable agent, all lenses, gate-capable)
│   ├── learn.md               # Wiki maintenance
│   └── platform-gitlab.md     # GitLab CLI/API/field mappings (reference, not a command)
├── hooks/               # Pre-commit drift-check hook (v1.6.0+)
│   ├── hooks.json             # PreToolUse hook registration
│   ├── pre-commit-drift.py    # Wrapper: narrows to `git commit`, routes custom/built-in
│   └── builtin-checks.sh      # Zero-config auto-detection (version mirror + badge)
├── lib/                 # Shared Python helpers (stdlib-only, called by CLI + managed)
│   ├── meta.py                # /air:learn trigger counter (wiki file or memory-store backend + find-store)
│   ├── wiki_git.py            # Clone + commit-meta-with-retry (legacy wiki backend)
│   ├── pattern_lifecycle.py   # Deterministic author-pattern strengthen/clean/decline/archive ops
│   ├── pr_conversation.py     # Merge GitHub PR comments/reviews into <pr-conversation> agent context
│   ├── solo_prompt.py         # THE solo-prompt assembly (CLI --solo runs it; managed setup.py imports it)
│   └── verdict.py             # THE review-gating contract (shared: CLI Step 12 runs it via --decide; managed imports it)
└── .claude-plugin/
    └── plugin.json      # Plugin metadata (name, version, author)

.air-checks.sh           # Opt-in per-repo drift checks (consumed by the hook above)

.claude-plugin/
└── marketplace.json     # Marketplace distribution definition

managed/                          # Managed Agent (CI automation)
├── api.py                        # Shared API helpers
├── setup.py                      # Creates/updates agents + environment via API
├── review.py                     # Client-side driver: orchestrates the review run (launches coordinator, posts)
├── github_client.py              # GitHub REST: fetchers, pagination, comment/verdict POSTs
├── verdict.py                    # Thin shim re-exporting plugins/air/lib/verdict.py (the shared gating contract)
├── session_runner.py             # Session lifecycle: run_session, REST drain, billing retry, SIGTERM cleanup
├── prompts.py                    # Prompt builders: PR context block + verifier-task templates
├── learn.py                      # Triggers wiki/store maintenance sessions (single-agent)
├── memory_store.py               # Per-repo pattern memory store: discovery, reads, sha256-preconditioned writes
├── pattern_writer.py             # Applies pattern_lifecycle ops to the store after each review
├── migrate_wiki_to_store.py      # One-shot wiki → store migration (per-author split, --dry-run)
├── migrate_workspace_stores.py   # One-shot store → store copy across workspaces (sha256-verified)
├── render_store_to_wiki.py       # Deterministic store→wiki mirror render (throttled per-review + on learn)
├── salvage_review.py             # Drain a finished orphaned session and post its review ($0 — e.g. after a job cancel)
├── test-session.py               # Quick 9-test verification script
├── prompts/learn-orchestrator.md # System prompt for cloud learn agent
│                                 # (review orchestrator.md deleted in v1.7.0 — review.py orchestrates client-side)
├── requirements.txt              # Python dependencies
└── README.md

.github/workflows/
├── managed-review.yml            # Reusable GitHub Action (teams reference this)
├── air-review.yml                # Dogfood caller for this repo (PR + workflow_dispatch)
└── release-please.yml            # Automated tag + GitHub Release on version bumps
```

## Architecture

**Review pipeline** (`commands/review.md`): Parses args, detects platform (GitHub/GitLab) from git remote, fetches PR/MR data via `gh` or `glab` CLI, runs up to 6 agents + optional Codex in parallel (the UI/copy reviewer joins only on user-facing diffs), passes results through a verification agent that filters false positives (confidence < 60 = dropped), then posts a consolidated comment. GitLab-specific command mappings are in `commands/platform-gitlab.md`.

**Agents** (`agents/*.md`): Stateless markdown prompt files. Each is a specialized reviewer personality that receives the same rich context block (PR diff, blame data, wiki patterns, project memory). Tiered models: code-reviewer and security-auditor run on Opus 4.8 with fast-mode speed (~2× faster generation; the fast-mode premium is not billed on Managed Agents sessions — "inference speed is managed by the runtime" per the pricing docs — but on the raw Messages API fast Opus 4.8 bills $10/$50 per MTok vs $5/$25 standard); review-verifier, simplify, and ui-copy-reviewer run on Sonnet 4.6; git-history-reviewer runs on Haiku 4.5. Each agent declares its model (and optional `speed:`) in frontmatter; managed resolves aliases via `MODEL_ALIASES` in `managed/setup.py`.

**Verification** (`agents/review-verifier.md`): Post-review quality gate. Reads actual source at flagged lines, classifies findings as CONFIRMED/DOWNGRADED/IMPROVEMENT/PRE-EXISTING/ACCEPTED PATTERN/FALSE POSITIVE using git blame decision tree.

**Pattern storage**: Two backends. **Store-backed repos** (migrated via `managed/migrate_wiki_to_store.py`) keep patterns in a per-repo Anthropic memory store — per-author files under `/authors/<login>.md`, shared files at the root, counter at `/meta/air-meta.json`; review sessions mount it read-only (PR content is untrusted — writes happen deterministically in `managed/pattern_writer.py` post-review), learn sessions mount read-write to curate it. The git wiki is an **exported mirror** rendered by a deterministic Python step (`managed/render_store_to_wiki.py`, the inverse of the migrate split), NOT by the AI session: it runs throttled after each review (≤1×/hour, gated by `meta.py mirror-due` — a cheap meta read in the common case, git push only when stale) and authoritatively after each `/air:learn` curation. Managed-only — the CLI never renders the store (a CLI-only store repo sees a stale wiki between managed runs). **Legacy repos** store everything on the repo's wiki (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md, REVIEW-ARCHIVE.md) with the counter in `.air-meta.json` at the wiki root. Auto-cleanup every 15 reviews or 14 days (with ≥1 new PR) — both backends via `plugins/air/lib/meta.py` (a repo's store presence, discovered by name `air-patterns <owner>/<repo>`, IS the rollout flag).

**Pre-commit drift check** (`hooks/`): A `PreToolUse` hook on `Bash` fires before every Claude-driven `git commit`, runs either a repo-specific `.air-checks.sh` (if executable at repo root) or the plugin's built-in auto-detection (manifest version vs shields badge + `currently X.Y.Z` + `**Version:** X.Y.Z` across `CLAUDE.md`/`README.md`/`docs/**/*.md`). Non-zero exit blocks the commit. `/air:review` Step 3.5 and `/air:learn` Step 4.65 generate/augment `.air-checks.sh` from `PROJECT-PROFILE.md`. `git commit --no-verify` bypasses. See `plugins/air/README.md` for the three-level progression.

## Development Workflow

- Edit agent files (`agents/*.md`) or command files (`commands/*.md`) directly
- Reload in Claude Code with `/reload` or reconnect
- Test with `/air:review <pr-number>` on a repo with PRs
- `--dry-run` flag prints to console without posting online
- After receiving a review, fix findings and run `/air:review --respond` to auto-classify, self-check, and reply

## Key Design Decisions

- **All agents run in parallel** — bottleneck is the slowest agent, not the sum
- **Batched API calls** — Step 4 uses 3 batched `gh` calls (metadata, diff, commits), not one per field, plus a bounded sibling-PR overlap scan (≤50 scanned / 10 reported)
- **Graduated dispute resistance** — security findings require compensating controls, style nits are readily accepted
- **Cross-repo reviews** skip local git data (blame, churn, wiki patterns) gracefully
- **Self-review mode** (`--self`) outputs a fix plan grouped by file; `--self --fix` auto-applies
- **Re-review mode** generates inter-diff from `REVIEWED_AT_SHA`, tracks FIXED/NOT FIXED per finding
- **Re-review severity-pin + deferred-findings ledger** (default ON, kill switch `AIR_LEDGER_PIN` ∈ `0`/`false`/`no`; both managed + CLI, single-sourced in `plugins/air/lib/verdict.py`) — makes severity carry-forward and finding-persistence a HARD deterministic guarantee, closing re-review's #1 failure mode: a prior `blocker` on code that *didn't change* silently drifting back to `medium` (un-gating a real blocker), or a prior finding being silently dropped. Same advisory→enforced move as `has_conflict_markers`. **Spine = finding-NUMBER identity**, with line evidence used ONLY where provably safe (the two-tier rule, validated on live fleet re-reviews): **round 3+** (prior is a re-review) pins purely by number (`INDETERMINATE`) — carried `#N` has no anchor and the body's only `**N.**` anchors are that round's NEW findings, whose numbers RESTART at 1 and collide with carried `#N`, so any line-evidence join cross-wires a carried blocker and could un-gate it (the bug the dogfood review caught). **Round-1→round-2** (the most common re-review; prior is a *fresh* review with non-colliding anchors) USES hunk-level line evidence: `extract_fresh_findings` enumerates the fresh findings by number+section-severity, `extract_fresh_finding_locations` recovers their anchors, and `finding_changed` marks a finding CHANGED iff its line falls in an edited hunk's old-side window — so a real fix (incl. additive/refactor) is HONORED, a fake `FIXED` on an untouched file is rewritten to `NOT FIXED`. Without this, pure-number-identity over-gated ~70% of round-2 re-reviews (every genuine fix); hunk-level dropped that to ~10% (only cross-region fixes — fix at a different line than the anchor — fail safe). `pin_and_resurrect` pins severity via `_max_severity` (reverts a downgrade, preserves an escalation → gate only gets *stricter*, never un-gate), rewrites a fake `FIXED` on a medium+ unchanged finding to `NOT FIXED` (an explicit low/nit `FIXED` is trusted), rewrites a `DEFERRED` to `NOT FIXED` only when it's a `blocker` or its code changed (a non-blocker `DEFERRED` on unchanged code stays deferred), tolerates a `**`-bolded status, keeps `DISPUTED`/`FALSE POSITIVE`/`PRE-EXISTING` as evidence-bearing exits, and **resurrects** any prior `#N` silently absent. Managed builds the ledger in `review.py`; CLI in `review.md` Step 11.5 (`verdict.py --pin` before Step 12's `--decide`). Logs `[pin]`/`[ledger]`. **v1 limits:** round-3+ pins by number not line (over-conservative — a legit late fix on a carried finding stays NOT FIXED until cleared via DISPUTED/override); round-2 line evidence is hunk-level, so a cross-region fix (fix lands at a different line than the flagged anchor) reads UNCHANGED and over-gates (fails safe, disputable); a finding *introduced in round K* isn't pinned until it appears in a status block (per-round renumbering ⇒ cross-round anchor tracking is v2).
- **Fresh-gate exposure floor** (default ON, kill switch `AIR_CATEGORY_FLOOR` ∈ `0`/`false`/`no`; floor *application* single-sourced in `plugins/air/lib/verdict.py`, parity across managed + CLI `--decide`) — the deterministic "active exposure = blocker" enforcement (same advisory→enforced move as `has_conflict_markers` and the re-review ledger). The re-review ledger pins severity by *carrying a prior finding forward*; a **fresh** review has no prior to pin against, so a weaker tier finding a real exposure but rating it `medium` would silently APPROVE (the 2026-06-12 Sonnet bench: org-wide PII + an RBAC bypass, both found, both rated medium, both would have un-gated). The verifier tags each confirmed, genuinely-exploitable exposure `[sec:<token>]` (one of a fixed blocker-class vocabulary — `pii-exposure`/`authz-bypass`/`sqli`/`idor`/`leaked-credential`/…, the `_BLOCKER_CATEGORIES` frozenset); `count_category_floored` floors any such tag (outside the already-counted Blockers section) to a blocker for the gate, regardless of the model's own label. The model *classifies into a bucket* (reliable across tiers); verdict.py *assigns the gate severity* (ours, deterministic). Inert on tag-less bodies → disabling (or any run before tags exist) is byte-identical to the pre-floor gate; it can only ever make the gate *stricter* on a real exposure, never un-gate. **Single emission source (all paths):** the tag-emission rule lives in the verifier SYSTEM PROMPT (`plugins/air/agents/review-verifier.md`) — the system prompt for the managed verifier agent, the CLI verifier agent, AND assembled into solo by `solo_prompt.py` — so managed CI, the CLI, and solo all emit `[sec:<token>]` from one source (no `prompts.py` task injection). The floor *application* is parity-wired in `verdict.py --decide`, so all three paths gate identically. Vocabulary is locked to the frozenset by `.air-checks.sh` Check F (a Python step asserts every blocker-class token appears backtick-quoted in `review-verifier.md`).
- **Promote fast-path** (managed-only, opt-in, default off) — enabled by the `promote_fastpath` workflow input **OR** a caller repo/org variable `AIR_PROMOTE_FASTPATH=true` (`managed-review.yml` ORs the two; `vars` in a reusable workflow resolves to the *caller's* variables, so a caller toggles it from Settings with no workflow edit — an org var is a fleet-wide switch). `review.py` reads the resolved `AIR_PROMOTE_FASTPATH` env. A fresh `promote/staging-to-main-*` PR has no prior review of its own, so it normally falls to a full re-read even though it nearly duplicates its predecessor promote. When enabled, `review.py:_detect_promote_fastpath()` finds the last-merged sibling promote air already reviewed and, **if the two overlap ≥80% of changed lines** (`PROMOTE_OVERLAP_THRESHOLD`), re-reviews this PR as a delta against the sibling's `Reviewed at:` SHA instead — reusing the entire re-review engine (inter-diff, carry-forward verifier, unfixed-blocker-only gating). Below 80% overlap, or on any missing-sibling / unresolvable-SHA / compare-failure, it falls back to full review. The one cross-PR fix: dev-comment context is forced empty (the sibling's comment id isn't in this PR's thread, so the normal `filter_comments_after` cursor would leak the whole current thread). Repo still mounts read-only, so specialists keep full-file context on unchanged sibling lines. Backtested on the repo-A/repo-B Phase-4 promote chain at ~64% cost reduction with zero net-new-finding loss. **v1 limitation:** no periodic full-anchor re-read — a long chain rides re-reviews indefinitely (decision logs print `[promote]` lines for monitoring).
- **Respond mode** (`--respond`) automates the developer side — classifies findings, verifies fixes match suggestions, self-checks for regressions, posts parseable response
- **Multi-PAT gate-orphan dismissal** (managed-only) — air's formal verdict is a PR review posted under whichever rotated bot PAT is active; GitHub's `reviewDecision` blocks on ANY account whose latest review is `CHANGES_REQUESTED`, so an APPROVE under account B never clears a stale block left under account A — a correct APPROVE at HEAD silently fails to un-gate (observed on a live PR). Every air verdict body now carries an invisible sentinel (`<!-- air-review-verdict -->`, an HTML comment hidden in the GitHub UI); after each verdict `dismiss_stale_air_verdicts` (`github_client.py`) dismisses prior `CHANGES_REQUESTED` reviews air owns — from accounts OTHER than the one just used (same-account is auto-superseded by GitHub). "air owns" = carries the sentinel (account-independent, zero false-positives — only air writes it) OR matches an optional caller allowlist (`AIR_PAT_MAP` keys / `AIR_BOT_LOGINS`) for legacy pre-sentinel orphans. **Never dismisses a human's review** (matches neither, by construction). Best-effort — a missing-dismiss-permission failure logs `[dismiss]`/`[warn]` and is non-fatal. The CLI posts under a single developer account (no rotation), so the orphan can't occur there — managed-only by design.
- **Diff hygiene & cost caps** (managed-only — CLI path unchanged by design): generated/vendored diff segments (minified bundles, sourcemaps, snapshots, `dist/`/`vendor/`/`node_modules/`, and lockfiles **whose same-directory manifest also changed**) stub to a visible `[air: <path>: N changed lines omitted (generated/vendored)]` marker — lockfile-only changes (the supply-chain shape) stay whole at the stub gate. Diff capped at `AIR_DIFF_MAX_BYTES` (500KB default) via greedy first-fit at file boundaries with an explicit truncation marker — a lockfile omitted by the *cap* (rare: lockfile-only diff > 500KB) gets a dedicated `[air: LOCKFILE … supply-chain review incomplete]` marker so the security lens flags the gap; a truncated re-review delta never skips codex. Conversation block tail-caps at `CONVERSATION_MAX_ENTRIES=30` (newest kept); codex skips re-review deltas under `CODEX_RE_REVIEW_MIN_LINES=20` changed lines. See `managed/README.md` → "Diff hygiene & cost caps".
- **UI / business-audience reviewer** (`air-ui-copy-reviewer`, v1.27.0; 6th specialist, Sonnet) — flags developer jargon, AI "writing fluff", clarity, and statically-detectable UX/a11y in user-facing changes. **Dispatch-gated** by `review.py:_diff_touches_ui()` so backend-only PRs add **$0**: a path is in-scope if it hits the built-in web allowlist (`.tsx/.jsx/.vue/.svelte/.html`, i18n catalogs, user-facing docs) **OR** matches a repo-declared glob in PROJECT-PROFILE.md's **`## User-Facing Copy Paths`** section — the opt-in that extends coverage to CLI/TUI copy modules (e.g. repo-C's Python user/agent message modules). Those globs are read from the store (`memory_store.read_memory`) **only when the web check misses**, so web PRs and store-less repos pay nothing extra. The coordinator dispatch is count-agnostic (an `Optional specialists in scope this run:` note names it or not); solo/both include the lens (it self-scopes). Advisory by default; reserves a **blocker only for clear user/clinical harm** (critical-flow AND affirmatively misleading), defaulting to medium when unsure. Built-in rubric + optional `## Voice & Copy` override. **Limit:** reviews only static copy in the diff — not runtime-generated agent output.
- **Pre-commit drift check** (v1.6.0) — plugin-wide `PreToolUse` hook blocks Claude-driven commits on detectable doc/version drift. Zero-config built-ins cover version mirror; opt-in `.air-checks.sh` adds repo-specific greps. Supply-chain note: the custom script executes with user privileges, so treat `.air-checks.sh` edits in incoming PRs as security-sensitive.
- **Single-agent `solo` mode** (opt-in via `AIR_REVIEW_MODE` / the `review_mode` workflow input / `review.py --mode`) — `full` (default, the multi-agent coordinator — 4 core specialists + the conditional UI/copy reviewer on UI-touching diffs + verifier), `solo` (one `air-solo-reviewer` agent applying all 6 lenses + self-verify in ONE session; its system prompt is assembled at sync from the 6 specialist `.md` files by the shared `plugins/air/lib/solo_prompt.py` (managed setup.py imports it; the CLI `--solo` flow runs it), so it never drifts and has no standalone prompt file — the UI lens self-scopes on non-UI diffs; the agent is created only when a run uses solo/both), or `both` (run the coordinator AND solo **concurrently** — the coordinator review gates as usual and drives the verdict/learn, the solo review posts alongside as a labeled non-blocking `## Code Review (solo — experimental)` comment for comparison). Benchmarked at ~$2–4 / ~7 min vs full's ~$10 / ~25 min on repo-A #994.
  - **Solo posts the same `APPROVE` / `REQUEST_CHANGES` verdict as `full`** — intentional (it can gate/approve), BUT it is **NOT gate-safe**: a single agent downgrades blocker *severity* (it can APPROVE a PR whose real blocker it rated medium), so its verdict is not a trustworthy hard gate. Enable solo only on repos where a single agent's verdict is acceptable (test/advisory); the default stays `full`. In `both` mode only the coordinator verdict gates — the solo comment carries none.
  - **Pattern learning:** on store-backed repos solo still strengthens author patterns (deterministic `pattern_writer` post-review); on **legacy-wiki repos** per-review reinforcement is skipped (it runs in the coordinator's TURN 3 wiki-write, which solo doesn't execute) — only the `/air:learn` cleanup cadence applies there.
  - `air-solo-reviewer` is deliberately NOT pinnable (pin the specialists it's assembled from). **CLI counterpart (v1.31.0):** `/air:review --solo` runs the SAME assembled prompt (`plugins/air/lib/solo_prompt.py` — one implementation, both paths import it) as ONE local Fable agent via the user's Claude Code subscription ($0 API spend; dodges org-side model gating since subscription inference ≠ org API). Advisory by default (comment, no verdict); `--solo --gate` opts into gating — blocker-class validation (2026-06-12) showed 1/2 blocker retention, so the verifier-anchored full pipeline stays the gating standard. Fresh full-PR reviews only (no re-review delta, no Codex); flow in `commands/review-solo.md`.
- **File-handoff in the managed coordinator** (v1.18.0, EXPERIMENTAL — off by default, `AIR_FILE_HANDOFF=1`; **dead end, do not flip**) — uploads context via the Files API and mounts at `/workspace/context/`. Verified 2026-06-03 that callable-agent threads run in isolated containers, and probe 3 (2026-06-11) showed `file` session resources don't materialize at all on the current runtime (not even in the primary thread). Superseded by the multiagent migration below; code retained only until that migration is validated.
- **Multiagent migration / workspace-handoff** (PR6′, EXPERIMENTAL — off by default, `AIR_MULTIAGENT=1` to enable) — runs full mode through `air-coordinator-ma`, a GA `multiagent`-roster coordinator (same `coordinator.md` prompt; created by setup.py only when the flag is on; not pinnable) whose sub-agent threads **share `/workspace`**. The coordinator's `MODE: WORKSPACE-HANDOFF` writes PR context + diff + verifier task to `/workspace/context/` ONCE in TURN 0 and delegates short file pointers — replacing the per-delegation re-emission (~60–150K output tokens/review, full mode's #1 structural cost). `air-git-history-reviewer` stays inline (the Haiku tier under-read file pointers in the 2026-06-10 decision bench). `session_runner.ThreadTracker` handles the GA thread-lifecycle event rename (`session.thread_status_idle`, threads can idle-then-re-run, primary thread excluded from the open count). De-risked by 4 runtime probes (parallel fan-out at width 5, cross-thread reads, event names, in-container git) and a completed 2026-06-11 A/B on 4 PRs across air + the work repos ($1.00–1.77/review vs $3.92–7.73 production inline, ≈ −65–80%, quality held). Per-repo enable: caller variable `AIR_MULTIAGENT=1` (Settings → Variables, passed through by managed-review.yml); delete to roll back.
- **Tiered MA coordinator** (opt-in, default off, `AIR_MA_COORDINATOR_MODEL=haiku`) — the MA coordinator is a **pure delegate-and-relay** layer: it does NOT synthesize (coordinator.md TURN 3 Part A outputs the verifier's review **VERBATIM**; the verifier is the synthesizer), so a cheaper/faster model is **relay-safe**. Validated 2026-06-19 (relay-fidelity A/B): across 4 runs a Haiku coordinator relayed **0/9, 0/11, 0/5, 0/7 findings dropped** — perfect fidelity — and the gate verdict was unchanged; the only run-to-run finding differences (incl. a one-off PHI-miss) traced to **upstream specialist/verifier nondeterminism**, coordinator-independent. Benefit: ~15% lower wall-time (idle-wakes get cheap/fast), and shorter gaps mean the runtime's 5-min prompt cache evicts less (the cache TTL itself is **not settable** in managed agents — confirmed: no `cache_control` field in agent/session create, Anthropic Agent-SDK issue #89; the 1h TTL is raw-Messages-API-only). Routes to a **separate** `air-coordinator-ma-<alias>` agent (setup.py step 4c) so a per-repo opt-in never mutates the shared Sonnet coordinator other callers use; unset/`sonnet`/unknown → the standard `air-coordinator-ma`. NOT a gate-safety change (specialists + verifier unchanged). Per-repo enable: `AIR_MA_COORDINATOR_MODEL=haiku` alongside `AIR_MULTIAGENT=1`; delete to roll back.

## Conventions

- Agent prompts are human-readable instructions, not minified — edit freely
- Findings must score 60+ confidence from verifier to appear in output
- Conflict markers in PR diff = automatic blocker finding
- Security auditor uses a 31-item checklist; PROJECT-PROFILE.md controls which items apply per repo
- Version is in `plugins/air/.claude-plugin/plugin.json` (currently 1.34.0) <!-- x-release-please-version -->
- Install via `/plugin marketplace add VorobiovD/air` then `/plugin install air@air`

## Releases (automated via release-please)

Never tag or cut a release manually. Every commit on main that uses Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, etc.) feeds the release PR:

- `.github/workflows/release-please.yml` runs on every push to main
- It maintains a long-lived "chore(main): release vX.Y.Z" PR on the repo
- That PR shows: next version (computed from commit types — `feat` → minor bump, `fix` → patch, `BREAKING CHANGE:` in footer → major), all version-mirror files bumped atomically, and a CHANGELOG.md entry
- Merge that PR when you're ready to release → bot creates the git tag + GitHub Release automatically

Files tracked for version mirroring are defined in `.release-please-config.json`'s `extra-files` array. Don't maintain a second list here — keep the config as the single source.

Force a specific version bump regardless of commit types by adding `Release-As: 1.9.0` in a commit footer.

After cutting a release, paste the blessed agent-version set into the GitHub Release notes (capture snippet in `managed/README.md`) — pinned callers reference it via the `agent_versions` workflow input; the air repo itself floats.
