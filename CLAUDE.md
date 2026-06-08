# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**air** is a Claude Code plugin for automated PR code review, with an optional Managed Agent mode for CI automation.

The CLI plugin (`plugins/air/`) is markdown files and JSON metadata — no build system or dependencies. The Managed Agent (`managed/`) adds Python scripts for Anthropic's cloud API + a GitHub Actions workflow.

Two CLI commands: `/air:review` (13-step review pipeline) and `/air:learn` (wiki cleanup/regeneration). For CI: `managed/review.py` or the reusable GitHub Action.

## Project Structure

```
plugins/air/
├── agents/              # 5 review agent definitions (markdown prompts)
│   ├── code-reviewer.md
│   ├── security-auditor.md
│   ├── simplify.md
│   ├── git-history-reviewer.md
│   └── review-verifier.md
├── commands/            # CLI command orchestration
│   ├── review.md              # Main pipeline — the core of the plugin
│   ├── review-self.md         # Self-review flow (--self mode)
│   ├── review-respond.md      # Respond flow (--respond mode)
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
│   └── pr_conversation.py     # Merge GitHub PR comments/reviews into <pr-conversation> agent context
└── .claude-plugin/
    └── plugin.json      # Plugin metadata (name, version, author)

.air-checks.sh           # Opt-in per-repo drift checks (consumed by the hook above)

.claude-plugin/
└── marketplace.json     # Marketplace distribution definition

managed/                          # Managed Agent (CI automation)
├── api.py                        # Shared API helpers
├── setup.py                      # Creates/updates agents + environment via API
├── review.py                     # Triggers review sessions
├── memory_store.py               # Per-repo pattern memory store: discovery, reads, sha256-preconditioned writes
├── pattern_writer.py             # Applies pattern_lifecycle ops to the store after each review
├── migrate_wiki_to_store.py      # One-shot wiki → store migration (per-author split, --dry-run)
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

**Review pipeline** (`commands/review.md`): Parses args, detects platform (GitHub/GitLab) from git remote, fetches PR/MR data via `gh` or `glab` CLI, runs 5 agents + optional Codex in parallel, passes results through a verification agent that filters false positives (confidence < 60 = dropped), then posts a consolidated comment. GitLab-specific command mappings are in `commands/platform-gitlab.md`.

**Agents** (`agents/*.md`): Stateless markdown prompt files. Each is a specialized reviewer personality that receives the same rich context block (PR diff, blame data, wiki patterns, project memory). Tiered models: code-reviewer and security-auditor run on Opus 4.8 with fast-mode speed (~2× faster generation; the fast-mode premium is not billed on Managed Agents sessions — "inference speed is managed by the runtime" per the pricing docs — but on the raw Messages API fast Opus 4.8 bills $10/$50 per MTok vs $5/$25 standard); review-verifier and simplify run on Sonnet 4.6; git-history-reviewer runs on Haiku 4.5. Each agent declares its model (and optional `speed:`) in frontmatter; managed resolves aliases via `MODEL_ALIASES` in `managed/setup.py`.

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
- **Respond mode** (`--respond`) automates the developer side — classifies findings, verifies fixes match suggestions, self-checks for regressions, posts parseable response
- **Pre-commit drift check** (v1.6.0) — plugin-wide `PreToolUse` hook blocks Claude-driven commits on detectable doc/version drift. Zero-config built-ins cover version mirror; opt-in `.air-checks.sh` adds repo-specific greps. Supply-chain note: the custom script executes with user privileges, so treat `.air-checks.sh` edits in incoming PRs as security-sensitive.
- **Single-agent `solo` mode** (managed-only, opt-in via `AIR_REVIEW_MODE` / the `review_mode` workflow input / `review.py --mode`) — `full` (default, the 6-agent coordinator), `solo` (one `air-solo-reviewer` agent applying all 5 lenses + self-verify in ONE session; its system prompt is assembled at sync from the 5 specialist `.md` files by `setup.py:assemble_solo_prompt()`, so it never drifts and has no standalone prompt file; the agent is created only when a run uses solo/both), or `both` (run the coordinator AND solo **concurrently** — the coordinator review gates as usual and drives the verdict/learn, the solo review posts alongside as a labeled non-blocking `## Code Review (solo — experimental)` comment for comparison). Benchmarked at ~$2–4 / ~7 min vs the 6-agent's ~$10 / ~25 min on qai-be #994.
  - **Solo posts the same `APPROVE` / `REQUEST_CHANGES` verdict as `full`** — intentional (it can gate/approve), BUT it is **NOT gate-safe**: a single agent downgrades blocker *severity* (it can APPROVE a PR whose real blocker it rated medium), so its verdict is not a trustworthy hard gate. Enable solo only on repos where a single agent's verdict is acceptable (test/advisory); the default stays `full`. In `both` mode only the coordinator verdict gates — the solo comment carries none.
  - **Pattern learning:** on store-backed repos solo still strengthens author patterns (deterministic `pattern_writer` post-review); on **legacy-wiki repos** per-review reinforcement is skipped (it runs in the coordinator's TURN 3 wiki-write, which solo doesn't execute) — only the `/air:learn` cleanup cadence applies there.
  - `air-solo-reviewer` is deliberately NOT pinnable (pin the specialists it's assembled from). **CLI gap (by design):** `/air:review` runs its agents locally in the Claude Code session with no managed coordinator, so there is no CLI `solo` equivalent — the mode is a `review.py` concept only.
- **File-handoff in the managed coordinator** (v1.18.0, EXPERIMENTAL — off by default, `AIR_FILE_HANDOFF=1` to enable) — PR context, diff, and verifier task + codex findings upload via the Files API and mount read-only at `/workspace/context/`; coordinator delegations become short pointers and specialists write findings to `/workspace/findings/<name>.md` (simplify replies inline — no write tool). Would cut the ~16K-output-token / ~4-min TURN-1 re-emission, BUT verified 2026-06-03 that callable-agent threads run in isolated containers on the research-preview runtime: `file` resources don't appear in sub-agent threads and cross-thread workspace writes don't propagate (github_repository mounts do). Inline is the production shape until the runtime supports it; `coordinator.md` + agent prompts handle both.

## Conventions

- Agent prompts are human-readable instructions, not minified — edit freely
- Findings must score 60+ confidence from verifier to appear in output
- Conflict markers in PR diff = automatic blocker finding
- Security auditor uses a 31-item checklist; PROJECT-PROFILE.md controls which items apply per repo
- Version is in `plugins/air/.claude-plugin/plugin.json` (currently 1.24.0) <!-- x-release-please-version -->
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
