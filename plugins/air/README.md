# air — Automated Code Review with Verification, Pattern Learning, and Team Knowledge

A Claude Code plugin that runs automated code reviews on any PR. Five specialized agents + Codex review in parallel, a dedicated verification agent filters false positives, findings are posted as a consolidated GitHub comment, and the system learns patterns across reviews via wiki-backed storage. Each team member's institutional knowledge enriches the review through Claude Code's memory system.

## Install

```
/install VorobiovD/air
```

That's it. Two commands become available: `/air:review-pr` and `/air:learn`. Updates are automatic.

## Prerequisites

- **Claude Code** — installed and running
- **GitHub CLI** (`gh`) — authenticated via `gh auth login`
- **Repo access** — must be able to run `gh pr view` on the target repo
- **Codex plugin** (optional) — if installed, runs as an additional reviewer. Skips gracefully if not available
- **GitHub Wiki** — auto-created on first run if missing. Used to store learned review patterns

## Usage

```bash
/air:review-pr 123                        # Full review of PR #123
/air:review-pr                            # Auto-detect: review current branch's PR, or self-review
/air:review-pr --self                     # Review local changes before pushing
/air:review-pr --self --fix               # Self-review + auto-apply fixes
/air:review-pr --re-review                # Delta review: FIXED/NOT FIXED tracking + new findings
/air:review-pr --fresh                    # Full review from scratch, new comment
/air:review-pr --rewrite                  # Full review, edit existing comment in place
/air:review-pr --full                     # Review entire codebase (first-time audit)
/air:review-pr --dry-run                  # Print to console, don't post to GitHub
/air:review-pr --no-codex                 # Skip Codex review pass
/air:review-pr https://github.com/org/repo/pull/45   # Cross-repo review
```

### Smart Default (no flags)

When you run `/air:review-pr` with no arguments:
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
11. **Format** — clickable GitHub links with full SHA, sequential numbering across all sections
12. **Post** — new comment, or PATCH existing (--rewrite), or console-only (--dry-run)
13. **Learn** — wiki push with graduated resistance + auto-trigger full cleanup every 5 reviews

### Five Specialized Agents

All agents run on Opus for consistent quality. Each receives the same rich context block (PR metadata, CI status, blame summaries, file churn, previous PR comments, project memory, session context).

**code-reviewer** — Bugs, logic errors, error handling, design issues. Checks for orphan imports on deleted files, reference updates on renames. Reads TODO/FIXME/HACK markers and flags comment rot (outdated comments that no longer match the code).

**simplify** — Duplication, dead code, unused imports, unnecessary complexity. Read-only — reports findings but never edits files.

**security-auditor** — 28-item checklist covering PHI/HIPAA (6 items), injection vulnerabilities (4), authentication/authorization (3), input validation (3), data exposure (3), operational security (4), and silent failures (5). Produces a PASS/FAIL table for every PR.

**git-history-reviewer** — Reviews code through the lens of git history. Blame analysis (stale code, absent authors, integration boundaries), file churn patterns (5+ commits in 6 months = design smell), previous PR review comments on the same files. Uses REVIEW-HISTORY.md for finding frequency and file hot spot data.

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

Posted as a single PR comment:

```
## Code Review

<one-line summary>

### Security Audit: X/Y PASS

| Check | Result |
|---|---|
| PHI in responses | PASS |
| Injection vulnerabilities | FAIL — evidence |

### Blockers

**1. Shell injection via unescaped user input**
file.go#L42-L45 — explanation with clickable GitHub link

### Medium

**2. Missing error propagation in retry loop**
file.py#L88 — explanation

### Low

**3. Unused import after merge resolution**
file.go#L3 — explanation

### Pre-existing Issues

> Not introduced by this PR. May warrant separate tickets.

**4. PHI field exposed in debug endpoint**
handler.py#L156 — explanation

### Strengths

- Error handling in the new retry logic covers all three failure modes
- Tests cover both happy path and the nil-subscription edge case

---

5 findings for this PR. Blockers should be fixed before merge.

Reviewed at: abc123def
```

## Pattern Learning

### Wiki-Backed Storage

Patterns are stored on the repo's GitHub Wiki:
- **No PRs needed** to update patterns — anyone can push directly
- **No merge conflicts** on pattern files
- **Every team member's reviews contribute** automatically
- **Repo-specific** — ai-relay patterns stay in ai-relay wiki

Two files:
- **REVIEW.md** — curated patterns: author tendencies, service-specific gotchas, common findings, accepted patterns, HIPAA reference. Updated incrementally after each review.
- **REVIEW-HISTORY.md** — analytical data auto-generated from PR comment history. Finding frequency tables, file hot spots, author trends, timeline. Regenerated periodically.

### Auto-trigger Cleanup

A local counter (`~/.claude/air:learn-meta.json`) tracks reviews since last cleanup. Every 5 reviews or 2 days — whichever comes first — whoever runs `/air:review-pr` automatically triggers:
- Full REVIEW.md deduplication and reorganization
- REVIEW-HISTORY.md regeneration from PR comment history
- Counter resets — distributed across the team

### Developer Feedback Loop

When developers dispute findings during re-review, the pipeline evaluates their explanation with graduated resistance:

- **Security/HIPAA** (HIGH resistance) — requires a concrete compensating control described, not just "we always do this"
- **Code quality** (MEDIUM resistance) — accepted if the developer explains the design tradeoff
- **Style/nits** (LOW resistance) — team conventions respected readily

Accepted explanations are added to an `Accepted Patterns` section in the wiki. Future reviews check this section and won't re-flag the same pattern.

## Better Reviews with Your Context

The pipeline reads your Claude Code memory files (`project` and `reference` types) and injects relevant institutional knowledge into every agent. This means reviews get better the more you use Claude Code.

To make this work well, save project-relevant context to your Claude Code memory:
- "Remember that agent-core is migrating from Bedrock to AgentCore runtime"
- "Remember that billing-tool variants must have matching prices across all files"
- "Remember that the staging API is at kkrus95s3k..."

Different team members bring different knowledge:
- A security engineer's memories inform the security-auditor about known compliance patterns
- A backend dev's memories inform the code-reviewer about API design decisions
- A devops engineer's memories inform infrastructure-specific checks

The skill also uses context from your current conversation session — if you were just discussing a bug or reviewing related code, that context flows into the review automatically.

## Self-Review Mode

Review your own code before pushing:

```bash
/air:review-pr --self          # Get a fix plan
/air:review-pr --self --fix    # Get a fix plan + auto-apply fixes
```

Same quality as PR review (all 5 agents + Codex + verifier). Output is a fix plan with exact current/replacement code for each finding, grouped by file.

## Cross-Repo Reviews

Review PRs from other repos without switching directories:

```bash
/air:review-pr https://github.com/org/other-repo/pull/45
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
/air:learn              # Full cleanup + history regeneration
/air:learn --dry-run    # Preview without pushing
/air:learn --history-only  # Only regenerate REVIEW-HISTORY.md
```

Fetches all review comments from recent merged PRs, extracts recurring patterns, deduplicates the wiki, and pushes back. Run manually when patterns feel noisy or after a batch of reviews.
