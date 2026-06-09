# air — Automated Code Review with Verification, Pattern Learning, and Team Knowledge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](../../LICENSE)
[![Version](https://img.shields.io/badge/version-1.29.0)](.claude-plugin/plugin.json) <!-- x-release-please-version -->
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://claude.ai/code)
[![GitHub](https://img.shields.io/badge/GitHub-supported-black.svg)](https://github.com)
[![GitLab](https://img.shields.io/badge/GitLab-supported-orange.svg)](https://gitlab.com)

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
- **GitHub CLI** (`gh`) or **GitLab CLI** (`glab`) — authenticated. Platform is auto-detected from the git remote URL. GitLab also requires `jq` installed.
- **Repo access** — must be able to view PRs/MRs on the target repo
- **Codex plugin** (optional) — if installed, runs as an additional reviewer. Skips gracefully if not available
- **Wiki** — auto-created on first run if missing. Used to store learned review patterns. Works with both GitHub and GitLab wikis.

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
/air:review --full                     # Review entire codebase (first-time audit)
/air:review 123 --closed               # Review a closed/merged PR (post-merge audit, pattern backfill)
/air:review --dry-run                  # Print to console, don't post online
/air:review --no-codex                 # Skip Codex review pass
/air:review https://github.com/org/repo/pull/45        # Cross-repo review (GitHub)
/air:review https://gitlab.com/group/project/-/merge_requests/45  # Cross-repo review (GitLab)
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

### Respond Mode

After fixing review findings (manually or with Claude Code), auto-generate the response:

```bash
/air:review --respond
```

This reads the existing review, then for each finding:
- **Auto-classifies** as fixed/unfixed by checking if the flagged code changed
- **Verifies fixes** are correct — compares what was done vs what was suggested
- **Asks you** about unfixed findings (dispute/acknowledge/won't-fix) for non-obvious cases
- **Runs the full review pipeline** on your fix diff to catch regressions
- **Detects additional changes** beyond the fixes (refactors, new features) and lists them

Posts a structured response the reviewer's re-review can parse directly, then pushes the branch.

## How It Works

### Pipeline (13 steps)

1. **Parse** — PR number or URL, flags, cross-repo detection
2. **Smart default** — detect existing reviews, auto re-review or self-review
3. **Load context** — CLAUDE.md, wiki patterns (REVIEW.md), finding history (REVIEW-HISTORY.md), project memory, session context
4. **Fetch** — batched API call (1 call for all metadata), diff, commits, blame summaries, file churn, previous PR comments (cross-PR pattern signal), current PR conversation (humans + other AI bots on this PR), CI status, file statuses (A/M/D/R)
5. **Pre-flight** — CI failures flagged to agents, conflict markers = automatic blocker, file complexity alerts
6. **Re-review** — inter-diff generation, developer response parsing, FIXED/NOT FIXED/DISPUTED tracking
7. **Review** — up to 6 agents + Codex in parallel (the UI/copy reviewer joins on user-facing diffs), each receives full PR Context block including history data
8. **Verify** — dedicated verification agent filters false positives with git blame decision tree
9. **Attribution** — console-only table showing which agent found what (never posted)
10. **Consolidate** — deduplicate, assign severity, generate Strengths section
11. **Format** — clickable links with full SHA, sequential numbering across all sections, trailing unnumbered Related PRs section when concurrent open PRs overlap
12. **Post** — new comment, or PATCH existing (--rewrite), or console-only (--dry-run)
13. **Learn** — wiki push with graduated resistance + auto-trigger full cleanup every 15 reviews

### Six Specialized Agents

Each agent receives the same rich context block (PR metadata, CI status, blame summaries, file churn, previous PR comments, project memory, session context). The identical prefix across the parallel agents enables prompt-cache hits within each model family.

**Model tiering (v1.5.0+):** Judgment-heavy reviewers (code-reviewer, security-auditor) run on Opus 4.8 in fast mode. The verifier, simplify, and the UI/copy reviewer run on Sonnet 4.6; git-history-reviewer runs on Haiku 4.5 — cheaper models matched to lighter task shapes. Each agent's model is declared in its own frontmatter.

**code-reviewer** — Bugs, logic errors, error handling, design issues, and test coverage gaps. Checks for orphan imports on deleted files, reference updates on renames, missing tests for new functionality. Reads TODO/FIXME/HACK markers and flags comment rot (outdated comments that no longer match the code). Greps `CLAUDE.md` and `**/*CONTEXT*.md` / `**/*HANDOFF*.md` / `**/*GOTCHAS*.md` files (repo root and any subdirectory) for diff-scope keywords so path-keyed gotchas get cross-referenced. Detects paired-doc drift when a PR adds a row to an enumerated structure but the paired sentinel doc or count string wasn't updated. Flags gate-output asymmetry — when an aggregate-predicate scope admits a parent record but the eager-load returns child rows without re-applying the per-row filter (cross-tenant data-leak class; blocker-grade for PHI / multi-tenant).

**simplify** — Three review dimensions: Code Reuse (active codebase search for existing utilities), Code Quality (dead code, copy-paste, stringly-typed code, redundant state), and Efficiency (N+1 patterns, missed concurrency, hot-path bloat, TOCTOU, unbounded structures). Read-only — reports findings but never edits files.

**security-auditor** — 31-item checklist covering sensitive data protection (6 items, conditional on project type), injection vulnerabilities (4), authentication/authorization (3), input validation (3), data exposure (3), operational security (4), silent failures (5), and resource exhaustion (3). PROJECT-PROFILE.md controls which checks apply per repo. Produces a PASS/FAIL table for every PR.

**git-history-reviewer** — Reviews code through the lens of git history. Blame analysis (stale code, absent authors, integration boundaries), file churn patterns (5+ commits in 6 months = design smell), previous PR review comments on the same files. Uses REVIEW-HISTORY.md for finding frequency and file hot spot data.

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

Patterns are stored on the repo's wiki (GitHub or GitLab) for legacy repos. **Store-backed repos** (migrated to a per-repo Anthropic memory store — see CLAUDE.md "Pattern storage") treat the wiki as an exported read-only mirror; the store is the source of truth. Wiki/mirror pages:
- **No PRs needed** to update patterns — anyone can push directly
- **No merge conflicts** on pattern files
- **Every team member's reviews contribute** automatically
- **Repo-specific** — each repo's patterns stay in that repo's wiki

Two files:
- **REVIEW.md** — curated patterns: author tendencies, service-specific gotchas, common findings, accepted patterns. Updated incrementally after each review.
- **REVIEW-HISTORY.md** — analytical data auto-generated from PR comment history. Finding frequency tables, file hot spots, author trends, timeline. Regenerated periodically.

### Auto-trigger Cleanup

A wiki-backed counter (`.air-meta.json` at the wiki root) tracks reviews since last cleanup, shared across CLI and managed runs. Every 15 reviews or 14 days (with ≥1 new PR) — whichever comes first — the next `/air:review` or `managed/review.py` automatically triggers:
- Full REVIEW.md deduplication and reorganization
- REVIEW-HISTORY.md regeneration from PR comment history
- Counter resets — distributed across the team

### Developer Feedback Loop

When developers dispute findings during re-review, the pipeline evaluates their explanation with graduated resistance:

- **Security/compliance** (HIGH resistance) — requires a concrete compensating control described, not just "we always do this"
- **Code quality** (MEDIUM resistance) — accepted if the developer explains the design tradeoff
- **Style/nits** (LOW resistance) — team conventions respected readily

Accepted explanations are added to an `Accepted Patterns` section in the wiki. Future reviews check this section and won't re-flag the same pattern.

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

Same quality as PR review (all in-scope agents + Codex + verifier). Output is a fix plan with exact current/replacement code for each finding, grouped by file.

## Cross-Repo Reviews

Review PRs from other repos without switching directories:

```bash
/air:review https://github.com/org/other-repo/pull/45
/air:review https://gitlab.com/group/other-project/-/merge_requests/45
```

Gracefully skips data that requires a local checkout (blame, churn, file statuses) and falls back to API-only data. Wiki patterns are skipped (repo-specific).

## Cost

CLI mode bills your Claude Code seat (subscription usage, not API dollars). Managed (CI) mode bills the Anthropic API key — **measured** from real session usage (~340 review sessions, May–June 2026): median **~$5–9 per review**, heavy PRs $15–30; learn epilogue ~$8–11 (Opus, pre-v1.15.0; ~40% less on Sonnet). The driver is cache-read volume (~5M cached tokens read per median review session, 30M on large PRs), not output. Token rates: Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5 per MTok — the "$15/$75" Opus rate quoted in earlier revisions was wrong, and the old per-agent estimates assumed one-shot calls (~50× below real agentic-session reads).

**Timing:** 9-15 minutes per review. All agents run in parallel — the bottleneck is the slowest agent, not the sum.

## Pre-commit Drift Check (v1.6.0+)

The plugin registers a `PreToolUse` hook on `Bash` that fires on every `git commit` (when Claude runs it via the Bash tool). The hook runs drift checks before letting the commit through; a non-zero exit blocks the commit with the output shown to Claude.

This shifts detection of the wiki's `Stale documentation references` and `Flow routing gaps` patterns from **post-hoc reviewer finding** to **pre-commit gate** — no more "grep for the old version before committing" prose advice that nobody follows.

### Three progressively stronger levels

1. **Zero config** — out of the box, the hook runs built-in auto-detection: manifest-file version vs shields.io version badge in README, `currently X.Y.Z` lines in `CLAUDE.md` / `README.md` / `docs/*.md`, and `**Version:** X.Y.Z` markdown headers. Supports `plugin.json`, `package.json`, `pyproject.toml`, `Cargo.toml`, `composer.json` manifests. Catches the most common drift class (version bumps not mirrored in docs) without any setup.

2. **Tailored (auto-generated)** — a `.air-checks.sh` gets generated automatically at two points if one doesn't exist:
   - First `/air:review` run on your repo — the deep-scan agent that produces `PROJECT-PROFILE.md` + `GLOSSARY.md` also emits `.air-checks.sh` tailored to your project's layout (which manifest, which mirror docs, which sentinel strings).
   - Any `/air:learn` run — bootstraps from the already-loaded `PROJECT-PROFILE.md` if the file is still missing (e.g., you installed the plugin after the wiki profile already existed, so Step 3.5 never fired on your repo).

   Generated files are written non-executable — review, `chmod +x`, commit to enable.

3. **Custom** — write (or edit) your own `.air-checks.sh` at the repo root. When the hook sees a custom script, it runs *only* that (you take full control). Your script can still delegate to built-ins via `$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh`:

   ```bash
   #!/bin/bash
   set -u
   status=0
   fail() { printf '  [FAIL] %s\n' "$1" >&2; status=1; }

   # Run built-ins first (version mirror, shields badge, etc.)
   "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" || status=1

   # Your project-specific checks below
   grep -q "MyCanonicalString" my-contract.md \
     || fail "my-contract.md missing required canonical string"

   exit $status
   ```

### Evolution over time

Each `/air:learn` run (periodic, every 15 reviews or 14 days):
- If `.air-checks.sh` doesn't exist yet → generates one from the wiki's `PROJECT-PROFILE.md` (see point 2 above).
- If it exists → inspects recurring Author Patterns in your wiki `REVIEW.md` and, if it sees codifiable drift (e.g., "Stale documentation references" flagged 3+ times with specific mirror-file shape), appends **commented-out** suggestions at the bottom for you to review-and-uncomment. Suggestions are capped at 3 per run and de-duplicated against existing content.

### Controls

- **Bypass:** `git commit --no-verify` skips the check entirely.
- **Disable:** create an empty executable `.air-checks.sh` with just `exit 0` — the hook runs that and returns clean.
- **Not executable:** if `.air-checks.sh` exists but isn't `chmod +x`, the hook prints a nudge and falls back to built-ins so you still get protection while you finish reviewing the auto-generated script.

See `.air-checks.sh` in the air repo itself for a real-world extension example (version consistency from built-ins + air-specific convention-enforcement greps).

## What's New in v1.8.0

**Managed-agent orphan-session cleanup.** If the Python driver dies mid-review (CI kill, Ctrl-C, uncaught exception), still-running sessions on Anthropic's side would previously keep burning tokens until their own idle timeout (~5 min) and block `DELETE /sessions/{id}`. v1.8.0 tracks live session IDs and sends `user.interrupt` on `atexit` + `SIGTERM` + `SIGHUP` (parallelized via daemon threads with a 12s bound so it doesn't starve CI's SIGKILL grace). Mid-review orphans (timed-out specialists) are also interrupted between Phase 1 and Phase 2 so they don't bill through the verifier phase.

**Auto-detect re-review mode (managed).** When a managed review runs on a PR that already has an `air-machine`-authored `## Code Review` comment, the driver now auto-detects and switches modes:
- Head SHA matches the prior `Reviewed at:` → **skip** (no-op, saves a full review cost on synchronize pushes that didn't change the tree)
- Head SHA advanced → **re-review** — specialists get the inter-diff (not the full PR diff), the prior review body, and any developer PR comments posted since, and classify each prior finding as FIXED / NOT FIXED / PARTIALLY FIXED / DISPUTED
- No prior → full review (existing behavior)
- `--fresh` forces a full review regardless

**Developer responses as context.** When a developer posts a reply on the PR ("will not fix #3 because Y"), the next managed re-review absorbs it as `<developer-comment>` context and surfaces the rationale in classifications instead of re-flagging the finding. Pair with `/air:review --respond` after pushing fixes for the tightest feedback loop.

**`--closed` opt-in for closed/merged PRs.** Default behavior still refuses to review closed/merged PRs (auto-trigger is event-gated anyway). `--closed` opts in for legitimate cases: post-merge audits, wiki-pattern backfill from historical PRs, dogfooding without opening a new PR. Works through the CLI plugin (`/air:review 123 --closed`), direct invocation (`python managed/review.py <repo> 123 --closed`), and `workflow_dispatch` from the Actions tab.

**`workflow_dispatch` trigger.** The reusable `managed-review.yml` now accepts `pr_number` + `closed` inputs, and the dogfood `air-review.yml` adds a `workflow_dispatch` trigger so any PR can be re-reviewed on demand from the Actions UI. See `managed/README.md` for the updated template.

Plus: pre-flight state gate, commit-checkout for closed PRs (since the head branch is often deleted on merge), and a stack of correctness fixes around session streaming, pagination, threadpool-during-atexit, and workflow input typing.

## Standalone Wiki Cleanup

```bash
/air:learn              # Full cleanup + history regeneration
/air:learn --dry-run    # Preview without pushing
/air:learn --history-only  # Only regenerate REVIEW-HISTORY.md
```

Fetches all review comments from recent merged PRs, extracts recurring patterns, deduplicates the wiki, and pushes back. Run manually when patterns feel noisy or after a batch of reviews.
