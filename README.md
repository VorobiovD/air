# air — Automated Code Review with Verification, Pattern Learning, and Team Knowledge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.49.0)](plugins/air/.claude-plugin/plugin.json) <!-- x-release-please-version -->
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://claude.ai/code)
[![GitHub](https://img.shields.io/badge/GitHub-supported-black.svg)](https://github.com)

> **New in 1.32.0:** deterministic re-review severity-pin + deferred-findings ledger — a prior blocker on code that didn't change can't silently drift to medium or be dropped (kill switch `AIR_LEDGER_PIN`); CLI solo mode (`--solo`); managed diff hygiene + cost caps. See the [improvement roadmap](docs/improvement-roadmap.md) for the full version history.

## Why

Code reviews are slow, inconsistent, and lose institutional knowledge when people leave. air fixes this:

- **6 specialized agents** review in parallel — security, code quality, simplification, git history, UI/business-audience copy (on user-facing diffs), and an optional Codex second opinion
- **A verification agent** filters false positives before posting — findings must score 60+ confidence
- **Wiki-backed learning** captures patterns from every review — author tendencies, service gotchas, accepted patterns
- **Re-review tracking** knows what was fixed and what wasn't — no starting over
- **Your memory enriches reviews** — Claude Code memory feeds institutional knowledge into every agent

One command. One consolidated PR comment. Gets smarter over time.

## Install

```
/plugin marketplace add VorobiovD/air
/plugin install air@air
```

Two commands become available: `/air:review` and `/air:learn`. To enable auto-updates, run `/plugin marketplace update air` or enable in `/plugin` → Marketplaces → air → "Enable auto-update".

## Prerequisites

- **Claude Code** — installed and running
- **GitHub CLI** (`gh`) — authenticated
- **Repo access** — must be able to view PRs on the target repo
- **Codex plugin** (optional) — if installed, runs as an additional reviewer. Skips gracefully if not available
- **Wiki** — auto-created on first run if missing. Used to store learned review patterns.

## Usage

```bash
/air:review 123                        # Full review of PR #123
/air:review                            # Auto-detect: review current branch's PR, or self-review
/air:review --self                     # Review local changes before pushing
/air:review --self --fix               # Self-review + auto-apply fixes
/air:review --re-review                # Delta review: FIXED/NOT FIXED tracking + new findings
/air:review --fresh                    # Full review from scratch, new comment
/air:review --rewrite                  # Full review, edit existing comment in place
/air:review --respond                  # Reply to review: classify findings + self-check + push
/air:review --solo                     # One agent, all 6 lenses, via your subscription ($0 API); advisory — add --gate to gate
/air:review --respond --dry-run        # Preview response without posting
/air:review --full                     # Review entire codebase (first-time audit)
/air:review 123 --closed               # Review a closed/merged PR (post-merge audit, pattern backfill)
/air:review --dry-run                  # Print to console, don't post online
/air:review --no-codex                 # Skip Codex review pass
/air:review https://github.com/org/repo/pull/45        # Cross-repo review
```

### Smart Default (no flags)

When you run `/air:review` with no arguments:
1. Checks if the current branch has an open PR — if yes, reviews it
2. If an existing review comment is found with new commits since — auto re-reviews
3. If no PR exists but you have local changes — auto self-reviews
4. If nothing to review — tells you

### Re-review Mode

After posting a review, developers can respond to findings by number:
- `#3 — fixed` → re-review checks if the code actually changed
- `#5 — this is our standard pattern` → evaluated with graduated resistance before accepting
- `#8 — pre-existing, not from this PR` → classified separately, not dropped

Re-review generates an inter-diff (only changes since last review) so agents focus on what's new.

A deterministic **severity-pin + deferred-findings ledger** guards the re-review gate: a prior `blocker` on code that didn't change can't silently drift to a lower severity or be dropped — the gate can only ever get stricter, never un-gate. It's on by default; set `AIR_LEDGER_PIN=0` (or `false`/`no`) to disable.

### Respond Mode

After fixing review findings (manually or with Claude Code), auto-generate the response:

```bash
/air:review --respond
```

This reads the existing review, then for each finding:
- **Auto-classifies** as fixed/unfixed by checking if the flagged code changed
- **Verifies fixes** are correct — compares what was done vs what was suggested
- **Asks you** about unfixed findings (dispute/acknowledge/won't-fix) for non-obvious cases
- **Scales self-check by diff size** — small fixes (< 50 lines) run code-reviewer + verifier only; larger diffs get the full 4-agent panel
- **Detects additional changes** beyond the fixes (refactors, new features) and lists them

Posts a structured response the reviewer's re-review can parse directly, then pushes the branch. Use `--respond --dry-run` to preview without posting.

## How It Works

### Pipeline (13 steps)

1. **Parse** — PR number or URL, flags, cross-repo detection
2. **Smart default** — detect existing reviews, auto re-review or self-review
3. **Load context** — CLAUDE.md, wiki patterns (REVIEW.md), finding history (REVIEW-HISTORY.md), project memory, session context
4. **Fetch** — batched API call (1 call for all metadata), diff, commits, blame summaries, file churn, previous PR comments (cross-PR pattern signal), current PR conversation (humans + other AI bots on this PR), CI status, file statuses (A/M/D/R)
5. **Pre-flight** — CI failures flagged to agents, conflict markers = automatic blocker, file complexity alerts, pure-promotion PR detection
6. **Re-review** — inter-diff generation, developer response parsing, FIXED/NOT FIXED/DISPUTED tracking
7. **Review** — up to 6 agents + Codex in parallel (the UI/copy reviewer joins on user-facing diffs), each receives full PR Context block including history data
8. **Verify** — dedicated verification agent filters false positives with git blame decision tree. Bootstrap calibration defaults when no severity data exists.
9. **Attribution** — console-only table showing which agent found what (never posted)
10. **Consolidate** — deduplicate, assign severity, generate Strengths section
11. **Format** — clickable links with full SHA, sequential numbering across all sections, trailing unnumbered Related PRs section when concurrent open PRs overlap
12. **Post** — new comment, or PATCH existing (--rewrite), or console-only (--dry-run)
13. **Learn** — author pattern lifecycle (create/strengthen/decline/archive), wiki push with graduated resistance, auto-trigger full cleanup every 15 reviews

**Pre-commit drift check (v1.6.0+):** A `PreToolUse` hook fires before every Claude-driven `git commit` and runs either a repo's opt-in `.air-checks.sh` or the plugin's built-in auto-detection (manifest-version mirror greps). Step 3.5 and `/air:learn` bootstrap the tailored script from `PROJECT-PROFILE.md`. See [`plugins/air/README.md`](plugins/air/README.md#pre-commit-drift-check-v160) for the three-level progression.

### Six Specialized Agents

Each agent receives the same rich context block (PR metadata, CI status, blame summaries, file churn, previous PR comments, project memory, session context). The identical prefix across the parallel agents enables prompt-cache hits within each model family (Opus calls share their own cache, Sonnet calls share theirs).

**Model tiering (intended default):** Judgment-heavy reviewers (code-reviewer, security-auditor) run on Opus 4.8 in fast mode. The verifier, simplify, and the UI/copy reviewer run on Sonnet; git-history-reviewer runs on Haiku 4.5 — cheaper models matched to lighter task shapes. Each agent's model is declared in its own frontmatter (`plugins/air/agents/<name>.md`) — **that `model:` line is the source of truth for the live tier, not this prose.** (code-reviewer + security-auditor are currently pinned to Sonnet too, temporarily, per #169 — so the live fleet runs Sonnet for all four review lenses + Haiku for git-history until the workspace model-access constraint lifts.)

**code-reviewer** — Bugs, logic errors, error handling, design issues, and test coverage gaps. Checks for orphan imports on deleted files, reference updates on renames, missing tests for new functionality. Reads TODO/FIXME/HACK markers and flags comment rot. Greps `CLAUDE.md` and `**/*CONTEXT*.md` / `**/*HANDOFF*.md` / `**/*GOTCHAS*.md` files (repo root and any subdirectory) for diff-scope keywords so path-keyed gotchas (e.g. "Secrets Manager stores resource ID") get cross-referenced. Detects paired-doc drift when a PR adds a row to an enumerated structure (IAM keys, secrets, callers) but the paired sentinel doc or count string wasn't updated. Flags gate-output asymmetry — when an aggregate-predicate scope (`EXISTS`, `whereHas`, set-membership) admits a parent record but the include / eager-load returns child rows without re-applying the per-row filter (cross-tenant data-leak class; blocker-grade for PHI / multi-tenant). Matches every finding against the PR author's known behavioral patterns and annotates matches.

**simplify** — Three review dimensions: Code Reuse (active codebase search for existing utilities), Code Quality (dead code, copy-paste, stringly-typed code, redundant state), and Efficiency (N+1 patterns, missed concurrency, hot-path bloat, TOCTOU, unbounded structures). Read-only — reports findings but never edits files.

**security-auditor** — 31-item checklist covering sensitive data protection (6 items, conditional on project type), injection vulnerabilities (4), authentication/authorization (3), input validation (3), data exposure (3), operational security (4), silent failures (5), and resource exhaustion (3). PROJECT-PROFILE.md controls which checks apply per repo. Produces a PASS/FAIL table for every PR. Matches findings against author patterns — an author with "Shell injection risk (3x)" gets extra scrutiny on security checks.

**git-history-reviewer** — Reviews code through the lens of git history. Blame analysis (stale code, absent authors, integration boundaries), file churn patterns (5+ commits in 6 months = design smell), previous PR review comments on the same files. Uses REVIEW-HISTORY.md for finding frequency and file hot spot data. Annotates findings that match the PR author's known patterns.

**ui-copy-reviewer** — UI / business-audience copy on user-facing diffs (web markup, i18n catalogs, help/content docs, and CLI/TUI copy modules a repo declares under `## User-Facing Copy Paths` in PROJECT-PROFILE.md). Flags developer jargon, AI "writing fluff", and clarity/tone problems in user-visible strings, plus statically-detectable UX/a11y issues in markup. Dispatch-gated so backend-only PRs add $0; advisory by default, reserving a blocker only for clear user/clinical harm.

**Codex** (GPT-5.4) — Independent second opinion from a different model family. Catches things Claude agents miss due to shared blind spots. Runs as a background process, results collected before verification.

### Verification Agent

After all in-scope reviewers complete, the **review-verifier** checks every finding against the actual code:

- Reads the source file at the flagged line + surrounding context
- Uses a structured decision tree to classify findings:
  - `+` line in diff → introduced by this PR
  - `-` line in diff → PR removed this code (the removal is the change)
  - Context line → uses `git blame` to determine if introduced or pre-existing
- Assigns confidence score (0-100) and one of 6 verdicts:
  - **CONFIRMED** (60+) — real finding, keep at stated severity
  - **DOWNGRADED** (60+) — real but severity was overstated
  - **IMPROVEMENT** (60+) — working code with a better alternative
  - **PRE-EXISTING** (any) — real but not introduced by this PR → separate section
  - **ACCEPTED PATTERN** (any) — matches team-approved pattern in wiki → suppressed
  - **FALSE POSITIVE** (<60) — factually wrong → dropped

### Review Output

Posted as a single PR comment. Here's a real example from [PR #1](https://github.com/VorobiovD/air/pull/1#issuecomment-4210596856) on this repo:

<details>
<summary>Example: review of the --full flag PR (click to expand)</summary>

> ## Code Review
>
> Adds `--full` flag for entire-codebase review. The implementation is clean but missing discoverability updates (argument-hint, README) and has a few ambiguities in how it integrates with the self-review flow.
>
> ### Security Audit: 7/7 PASS
>
> | Check | Result |
> |---|---|
> | Command injection | PASS |
> | Template injection | PASS |
> | Secrets management | PASS |
> | Infrastructure secrets | PASS |
> | Temp file hygiene | PASS |
> | Tool/permission minimality | PASS (N/A) |
> | Hardcoded paths | PASS |
>
> ### Medium
>
> **1. `--full` missing from argument-hint frontmatter**
>
> `plugins/air/commands/review.md#L3` — The `argument-hint` in YAML frontmatter lists every flag except `--full`. Users won't see it in CLI suggestions or tab-completion.
>
> **2. `--full` not documented in README.md**
>
> `README.md#L23-L33` — The Usage section lists every flag except `--full`.
>
> ### Low
>
> **3. Ambiguous skip target for `--full` routing**
>
> `plugins/air/commands/review.md#L21-L26` — Line 21 says "skip to the Self-Review Flow section below" but line 26 says "proceed to Self Step 2." These are contradictory.
>
> **4. `--full --fix` combination is unguarded**
>
> `plugins/air/commands/review.md#L17` — `--fix` is documented as "(only with `--self`)" but nothing prevents `--full --fix` from reaching Self Step 6.
>
> **5. "Never posts to GitHub" wording is misleading**
>
> `plugins/air/commands/review.md#L17` — The self-review flow pushes to the wiki. Rewording suggested.
>
> ### Strengths
>
> - The `git hash-object -t tree /dev/null` approach is the idiomatic git method for full-codebase diffs
> - Clean integration with the existing self-review flow without duplicating infrastructure
>
> ---
>
> 5 findings for this PR (2 medium, 3 low). No blockers.
>
> Reviewed at: 1d528e1b

</details>

## Pattern Learning

### Wiki-Backed Storage

Patterns are stored on the repo's wiki for legacy repos. **Store-backed repos** (migrated to a per-repo Anthropic memory store — see CLAUDE.md "Pattern storage") treat the wiki as an exported read-only mirror; the store is the source of truth. Wiki/mirror pages:
- **No PRs needed** to update patterns — anyone can push directly
- **No merge conflicts** on pattern files
- **Every team member's reviews contribute** automatically
- **Repo-specific** — each repo's patterns stay in that repo's wiki

Six wiki pages (created automatically as needed):
- **REVIEW.md** — curated patterns: author behavioral profiles, service-specific gotchas, common findings. Updated incrementally after each review.
- **REVIEW-HISTORY.md** — analytical data auto-generated from PR comment history. Finding frequency tables, file hot spots, author trends with clean-PR tracking, timeline. Regenerated periodically.
- **PROJECT-PROFILE.md** — project characteristics generated by an Opus deep scan on first run: languages, architecture, services, review focus rules, applicable security checks.
- **GLOSSARY.md** — project-specific terminology extracted from code and docs. Prevents false findings on intentional naming.
- **ACCEPTED-PATTERNS.md** — team-approved patterns that suppress matching findings in future reviews. Populated when developers successfully dispute findings.
- **SEVERITY-CALIBRATION.md** — per-agent confidence thresholds computed from dispute rates. Auto-recalculated when enough data exists.

### Author Pattern Lifecycle

Author patterns in REVIEW.md are behavioral profiles that evolve over time — not task items that get deleted when fixed. Each pattern tracks occurrence count and consecutive clean PRs:

```
### alice
- **Shell injection risk** (3x: #45, #52, #67 | last 2 PRs: 2 clean): Misses escapeshellarg() on user input in shell commands
- **Empty array guard** (1x: #67 | new): Uses implode() on arrays without checking empty first
```

Lifecycle: **create** (1x, new) → **strengthen** (Nx, counter resets on match) → **decline** (5 clean PRs) → **archive** (10 clean PRs). Patterns are never deleted — archived patterns stay permanently as historical context. Review agents match every finding against the PR author's known patterns and annotate matches, which the orchestrator uses to drive lifecycle transitions.

### Auto-trigger Cleanup

A wiki-backed counter (`.air-meta.json` at the wiki root) tracks reviews since last cleanup, shared across CLI and managed runs. Every 15 reviews or 14 days (with ≥1 new PR) — whichever comes first — the next `/air:review` or managed run automatically triggers:
- Full REVIEW.md deduplication and reorganization
- REVIEW-HISTORY.md regeneration from PR comment history
- Counter resets — distributed across the team

### Developer Feedback Loop

When developers dispute findings during re-review, the pipeline evaluates their explanation with graduated resistance:

- **Security/compliance** (HIGH resistance) — requires a concrete compensating control described, not just "we always do this"
- **Code quality** (MEDIUM resistance) — accepted if the developer explains the design tradeoff
- **Style/nits** (LOW resistance) — team conventions respected readily

Accepted explanations are stored in ACCEPTED-PATTERNS.md (a dedicated wiki page). Future reviews check this file and suppress matching findings automatically.

## Better Reviews with Your Context

The pipeline reads your Claude Code memory files (`project` and `reference` types) and injects relevant institutional knowledge into every agent. This means reviews get better the more you use Claude Code.

To make this work well, save project-relevant context to your Claude Code memory:
- "Remember that the auth service is migrating from JWT to OAuth2"
- "Remember that pricing configs must have matching values across all variant files"
- "Remember that the staging API is at api-staging.example.com"

Different team members bring different knowledge:
- A security engineer's memories inform the security-auditor about known compliance patterns
- A backend dev's memories inform the code-reviewer about API design decisions
- A devops engineer's memories inform infrastructure-specific checks

The skill also uses context from your current conversation session — if you were just discussing a bug or reviewing related code, that context flows into the review automatically.

## Self-Review Mode

Review your own code before pushing:

```bash
/air:review --self          # Get a fix plan
/air:review --self --fix    # Get a fix plan + auto-apply fixes
```

Same quality as PR review (all in-scope agents + Codex + verifier). Output is a fix plan with exact current/replacement code for each finding, grouped by file. Never posts a PR comment — wiki pattern updates still push.

## Cross-Repo Reviews

Review PRs from other repos without switching directories:

```bash
/air:review https://github.com/org/other-repo/pull/45
```

Gracefully skips data that requires a local checkout (blame, churn, file statuses) and falls back to API-only data. Reads the target repo's wiki for pattern context (author patterns, project profile, accepted patterns). Wiki writes are skipped to avoid cross-pollination.

## Cost

CLI mode bills your Claude Code seat (subscription usage, not API dollars). Managed (CI) mode bills the Anthropic API key — **measured** from real session usage (~340 review sessions, May–June 2026; token rates: Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5 per MTok):

| Session | Median | Heavy PR |
|---|---|---|
| Review (coordinator + 4 specialists + verifier; +UI/copy specialist on user-facing diffs) | **~$5–9** | $15–30 |
| Learn epilogue (full wiki cleanup) | ~$8–11 on Opus (pre-v1.15.0); ~40% less on Sonnet | $20+ |

The dominant cost driver is **cache-read volume**, not output: a median review session reads ~5M cached tokens (the multi-agent loop re-reads the PR context + wiki block every tool-use turn); large PRs reach 30M reads. Output is a minor share (50–170K tokens/review). The structurally identical PR Context block across agents keeps those reads at the $0.10-per-$1 cache-hit rate — without it costs would be ~10× higher.

v1.15.0 cut learn frequency ~3× (15-review/14-day cadence, was 5/2) and moved the learner to Sonnet. The fast-mode premium is not billed on Managed Agents sessions. (Earlier revisions of this section estimated $0.15–0.75 per agent from one-shot-call assumptions and quoted a wrong "$15/$75" Opus rate — real agentic sessions read ~50× more tokens than a one-shot call; per-agent static estimates are obsolete.)

**Timing:** 9-15 minutes per review. All agents run in parallel — the bottleneck is the slowest agent, not the sum.

## Automated Reviews (CI/CD)

Review every PR automatically — no human trigger needed. Uses Anthropic Managed Agents.

### Setup (one-time per org, ~10 min)

**1. Create a bot account**

Sign up a new GitHub account (e.g., `air-reviewer-bot`). This is the identity that posts reviews.

**2. Add the bot to your repos**

Invite the bot as a collaborator (Write role) on repos you want reviewed. Or add as an org member.

**3. Generate a token**

On the bot account: Settings → Developer settings → Personal access tokens → **Tokens (classic)** → Generate new token.
- Scopes: check `repo`
- Expiration: 90 days or longer

**4. Add secrets**

In your GitHub org: Settings → Secrets and variables → Actions → New organization secret:
- `ANTHROPIC_API_KEY` — your Anthropic API key (with Managed Agents access)
- `AIR_BOT_TOKEN` — the bot's classic PAT from step 3

**5. Enable on a repo**

Add this file to any repo (request-driven variant — re-reviews fire when the bot is requested as reviewer, which `/air:review --respond` does automatically; see `managed/README.md` for the push-driven variant with the `cooldown_minutes` debounce):

```yaml
# .github/workflows/air-review.yml
name: air review
on:
  pull_request:
    types: [opened, ready_for_review, review_requested]

jobs:
  review:
    # Replace `air-machine` with your bot's login.
    if: ${{ github.event.action != 'review_requested' || github.event.requested_reviewer.login == 'air-machine' }}
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      AIR_BOT_TOKEN: ${{ secrets.AIR_BOT_TOKEN }}
```

The first PR auto-creates the review agents. Re-reviews fire on reviewer re-request (automatic via `--respond`) instead of every push — measured at ~$5–9 per review session, the trigger model is the biggest cost lever. This is the minimal form: add the `workflow_dispatch` block from `managed/README.md` Variant A for on-demand runs from the Actions tab. Agent prompts update automatically when the air repo is updated.

### What happens on each PR

1. GitHub Action triggers → checks out air repo → syncs agent prompts
2. Creates a Managed Agent session with the repo pre-cloned
3. Orchestrator runs the full review pipeline (same agents as CLI)
4. Posts review as the bot account
5. Pushes learned patterns to the repo's wiki

### Remote wiki maintenance

```bash
# From your machine (requires ANTHROPIC_API_KEY + AIR_BOT_TOKEN env vars)
python managed/learn.py myorg/myrepo                  # Full cleanup
python managed/learn.py myorg/myrepo --history-only   # Only regenerate history
python managed/learn.py myorg/myrepo --refresh-profile # Re-scan project profile
```

## Standalone Wiki Cleanup

```bash
/air:learn                  # Full cleanup + history regeneration
/air:learn --dry-run        # Preview without pushing
/air:learn --history-only   # Only regenerate REVIEW-HISTORY.md
/air:learn --refresh-profile  # Re-run full project scan for PROJECT-PROFILE.md + GLOSSARY.md
```

Fetches all review comments from recent merged PRs, extracts recurring patterns, deduplicates the wiki, migrates legacy author patterns to lifecycle format, reconciles clean-PR counters, and pushes back. Run manually when patterns feel noisy or after a batch of reviews.
