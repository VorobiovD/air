# air — Automated Code Review with Verification, Pattern Learning, and Team Knowledge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.2.0-green.svg)](plugins/air/.claude-plugin/plugin.json)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://claude.ai/code)
[![GitHub](https://img.shields.io/badge/GitHub-supported-black.svg)](https://github.com)
[![GitLab](https://img.shields.io/badge/GitLab-supported-orange.svg)](https://gitlab.com)

## Why

Code reviews are slow, inconsistent, and lose institutional knowledge when people leave. air fixes this:

- **5 specialized agents** review in parallel — security, code quality, simplification, git history, and an optional Codex second opinion
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
4. **Fetch** — batched API call (1 call for all metadata), diff, commits, blame summaries, file churn, previous PR comments, CI status, file statuses (A/M/D/R)
5. **Pre-flight** — CI failures flagged to agents, conflict markers = automatic blocker, file complexity alerts
6. **Re-review** — inter-diff generation, developer response parsing, FIXED/NOT FIXED/DISPUTED tracking
7. **Review** — 5 agents + Codex in parallel, each receives full PR Context block including history data
8. **Verify** — dedicated verification agent filters false positives with git blame decision tree
9. **Attribution** — console-only table showing which agent found what (never posted)
10. **Consolidate** — deduplicate, assign severity, generate Strengths section
11. **Format** — clickable links with full SHA, sequential numbering across all sections
12. **Post** — new comment, or PATCH existing (--rewrite), or console-only (--dry-run)
13. **Learn** — author pattern lifecycle (create/strengthen/decline/archive), wiki push with graduated resistance, auto-trigger full cleanup every 5 reviews

### Five Specialized Agents

All agents run on Opus for consistent quality. Each receives the same rich context block (PR metadata, CI status, blame summaries, file churn, previous PR comments, project memory, session context).

**code-reviewer** — Bugs, logic errors, error handling, design issues, and test coverage gaps. Checks for orphan imports on deleted files, reference updates on renames, missing tests for new functionality. Reads TODO/FIXME/HACK markers and flags comment rot. Matches every finding against the PR author's known behavioral patterns and annotates matches.

**simplify** — Three review dimensions: Code Reuse (active codebase search for existing utilities), Code Quality (dead code, copy-paste, stringly-typed code, redundant state), and Efficiency (N+1 patterns, missed concurrency, hot-path bloat, TOCTOU, unbounded structures). Read-only — reports findings but never edits files.

**security-auditor** — 31-item checklist covering sensitive data protection (6 items, conditional on project type), injection vulnerabilities (4), authentication/authorization (3), input validation (3), data exposure (3), operational security (4), silent failures (5), and resource exhaustion (3). PROJECT-PROFILE.md controls which checks apply per repo. Produces a PASS/FAIL table for every PR. Matches findings against author patterns — an author with "Shell injection risk (3x)" gets extra scrutiny on security checks.

**git-history-reviewer** — Reviews code through the lens of git history. Blame analysis (stale code, absent authors, integration boundaries), file churn patterns (5+ commits in 6 months = design smell), previous PR review comments on the same files. Uses REVIEW-HISTORY.md for finding frequency and file hot spot data. Annotates findings that match the PR author's known patterns.

**Codex** (GPT-5.4) — Independent second opinion from a different model family. Catches things Claude agents miss due to shared blind spots. Runs as a background process, results collected before verification.

### Verification Agent

After all 5 reviewers complete, the **review-verifier** checks every finding against the actual code:

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

Patterns are stored on the repo's wiki (GitHub or GitLab):
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

A local counter (`~/.claude/review-learn-meta.json`) tracks reviews since last cleanup. Every 5 reviews or 2 days — whichever comes first — whoever runs `/air:review` automatically triggers:
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

Same quality as PR review (all 5 agents + Codex + verifier). Output is a fix plan with exact current/replacement code for each finding, grouped by file.

## Cross-Repo Reviews

Review PRs from other repos without switching directories:

```bash
/air:review https://github.com/org/other-repo/pull/45
/air:review https://gitlab.com/group/other-project/-/merge_requests/45
```

Gracefully skips data that requires a local checkout (blame, churn, file statuses) and falls back to API-only data. Wiki patterns are skipped (repo-specific).

## Cost

Per review (API pricing, Opus 4.6 at $5/$25 per 1M tokens):

| Component | Tokens | Cost |
|---|---|---|
| 4 agents | ~135k | ~$1.38 |
| Verification agent | ~27k | ~$0.28 |
| Codex | external | varies |
| **Total** | **~162k** | **~$1.66** |

At 40 reviews/month: ~$66/month. On Team/Pro subscription this is included in the seat cost.

**Timing:** 9-15 minutes per review. All agents run in parallel — the bottleneck is the slowest agent, not the sum.

## Standalone Wiki Cleanup

```bash
/air:learn                  # Full cleanup + history regeneration
/air:learn --dry-run        # Preview without pushing
/air:learn --history-only   # Only regenerate REVIEW-HISTORY.md
/air:learn --refresh-profile  # Re-run full project scan for PROJECT-PROFILE.md + GLOSSARY.md
```

Fetches all review comments from recent merged PRs, extracts recurring patterns, deduplicates the wiki, migrates legacy author patterns to lifecycle format, reconciles clean-PR counters, and pushes back. Run manually when patterns feel noisy or after a batch of reviews.
