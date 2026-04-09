---
description: Clean up, deduplicate, and reorganize the wiki REVIEW.md using AI. Also regenerates REVIEW-HISTORY.md from PR comment history.
argument-hint: [--dry-run] [--history-only] [--refresh-profile]
---

Fetch REVIEW.md from the wiki, clean it up using AI, generate REVIEW-HISTORY.md from PR comment history, and push both back.

Note: `/air:review` auto-triggers this command every 5 reviews or every 2 days (whichever comes first). You can also run it manually for immediate cleanup.

**Flags:**
- `--dry-run` — preview changes without pushing to wiki
- `--history-only` — only regenerate REVIEW-HISTORY.md, don't touch REVIEW.md
- `--refresh-profile` — re-run the full Opus deep scan for PROJECT-PROFILE.md + GLOSSARY.md (same as first-run discovery). Use when the project has changed significantly (new language, new service, major restructure). Overwrites existing profile and glossary with fresh scan results.

## Platform Detection

Same as `/air:review` — detect platform from git remote URL (or `AIR_PLATFORM` env var override). See review.md "Platform Detection" section for full logic. Sets `PLATFORM`, `PLATFORM_DOMAIN`, and `CLI`.

All `gh` commands below are written for GitHub. On GitLab, translate using platform-gitlab.md — same as review.md.

## Step 1: Fetch from wiki

```bash
# GitHub: gh repo view --json nameWithOwner --jq '.nameWithOwner'
# GitLab: glab repo view --json path_with_namespace --jq '.path_with_namespace'
CURRENT_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)
WIKI_URL="https://$PLATFORM_DOMAIN/$CURRENT_REPO.wiki.git"
cd /tmp && rm -rf review-wiki-learn && git clone --depth 1 "$WIKI_URL" review-wiki-learn 2>/dev/null
```

If the clone succeeded (the directory `/tmp/review-wiki-learn/.git` exists), copy whichever pattern files exist. Each copy is independent — on a first run the wiki may have no pattern files yet:
```bash
WIKI_DIR="/tmp/review-wiki-learn"
if [ -d "$WIKI_DIR/.git" ]; then
  cp "$WIKI_DIR/REVIEW.md" /tmp/REVIEW.md 2>/dev/null
  cp "$WIKI_DIR/REVIEW-HISTORY.md" /tmp/REVIEW-HISTORY.md 2>/dev/null
  cp "$WIKI_DIR/PROJECT-PROFILE.md" /tmp/PROJECT-PROFILE.md 2>/dev/null
  cp "$WIKI_DIR/ACCEPTED-PATTERNS.md" /tmp/ACCEPTED-PATTERNS.md 2>/dev/null
  cp "$WIKI_DIR/SEVERITY-CALIBRATION.md" /tmp/SEVERITY-CALIBRATION.md 2>/dev/null
  cp "$WIKI_DIR/GLOSSARY.md" /tmp/GLOSSARY.md 2>/dev/null
fi
```

If the clone failed (no `.git` directory): print "Wiki not found — create at https://$PLATFORM_DOMAIN/$CURRENT_REPO/-/wikis (GitLab) or https://$PLATFORM_DOMAIN/$CURRENT_REPO/wiki (GitHub)" and STOP.

**GitLab note:** After `CURRENT_REPO` is set, resolve the project ID for API calls: `PROJECT_ID=$(glab api "projects/$(echo $CURRENT_REPO | sed 's|/|%2F|g')" --jq '.id')`

If `--history-only` was passed, skip to Step 4 (only regenerate history, don't touch REVIEW.md).

## Step 2: Analyze REVIEW.md

Read the entire file and analyze every pattern entry for:

- **Semantic duplicates** — patterns that describe the same issue in different words. Merge into one, keeping the best wording. Examples:
  - "raw third-party response proxied to callers" + "unfiltered external API forwarded in error details" → one pattern
  - "user_id int/string breaking change" + "user_id schema breaks integer callers" → one pattern
- **Stale patterns** — patterns that were fixed project-wide and no longer relevant. Check recent PRs if needed. Mark as potentially stale but don't remove without confirming.
- **Misplaced patterns** — author patterns that should be service patterns (or vice versa). Move to the correct section.
- **Vague patterns** — entries too generic to be actionable. Make them specific or remove.
- **Patterns in wrong section** — if learned from one author but applies to everyone, move to Common Findings.

## Step 3: Reorganize REVIEW.md

- Alphabetize authors within Author Patterns
- Group related patterns within each section (security together, config together, etc.)
- Ensure no section exceeds ~15 patterns — promote the most general ones to Common Findings
- If a compliance reference section exists (e.g., HIPAA Quick Reference for healthcare projects), keep it unchanged (it's a reference, not learned patterns)

Generate the cleaned-up REVIEW.md content.

## Step 3.5: Refresh PROJECT-PROFILE.md

**If `--refresh-profile` was passed:** Run the full Opus deep scan (same as `/air:review` Step 3.5 first-run discovery). This overwrites the existing PROJECT-PROFILE.md and GLOSSARY.md with fresh results. Use when the project has changed significantly — new language, new service, major restructure, or when agents have flagged wiki drift.

**Otherwise (default, lightweight refresh):**

Only run if `/tmp/PROJECT-PROFILE.md` exists (first-run already happened). If it doesn't exist, skip — the first-run discovery in `/air:review` Step 3.5 handles initial creation.

File-based detection only (~2s, no Opus agent):
```bash
# Detect new/removed manifest files
ls go.mod package.json requirements.txt composer.json Makefile Dockerfile *.tf template.yaml 2>/dev/null
# Detect new top-level service directories
ls -d */ 2>/dev/null | head -20
```

Update the `## Languages` and `## Services` sections in `/tmp/PROJECT-PROFILE.md`. Do NOT touch:
- "Review Focus Rules" section — manually curated after initial generation
- "Applicable Security Checks" section — unless a new language/framework was detected (e.g., SQL files appeared for the first time → add the SQL injection check)

**Auto-trigger deep refresh:** If the lightweight scan detects a significant change (new manifest file type that didn't exist before, e.g., `package.json` appearing in a Go-only project), automatically escalate to a full Opus deep scan for this run. Print: "New framework detected — running full profile refresh."

## Step 4: Generate REVIEW-HISTORY.md (KAIROS)

Fetch all review comments from recent closed/merged PRs and extract finding history:

```bash
# Fetch last 30 closed/merged PRs with review comments
# GitLab: use projects/$PROJECT_ID/merge_requests?state=merged&per_page=30&order_by=updated_at&sort=desc, use .iid not .number
RECENT_PRS=$(gh api "repos/$CURRENT_REPO/pulls?state=closed&per_page=30&sort=updated&direction=desc" --jq '.[] | select(.merged_at != null) | .number' 2>/dev/null)

for PR_NUM in $RECENT_PRS; do
  # Get review comments (inline code comments)
  # GitLab: projects/$PROJECT_ID/merge_requests/$PR_NUM/discussions
  gh api "repos/$CURRENT_REPO/pulls/$PR_NUM/comments" --jq '.[] | {pr: '$PR_NUM', path: .path, body: (.body | split("\n")[0][:200])}' 2>/dev/null

  # Get issue comments that start with "## Code Review" (our posted reviews)
  # GitLab: projects/$PROJECT_ID/merge_requests/$PR_NUM/notes (filter same way)
  gh api "repos/$CURRENT_REPO/issues/$PR_NUM/comments" --jq '.[] | select(.body | startswith("## Code Review")) | {pr: '$PR_NUM', body: .body}' 2>/dev/null
done
```

**Sensitive data safety:** Do NOT fetch `diff_hunk` from review comments — it may contain secrets, credentials, PII, or other sensitive data. Only fetch `path` and `body` (first 200 chars).

**Rate limiting:** If any API call returns 403/429, pause for 5 seconds and retry once. Cap total API calls at 100.

From the raw data, generate `REVIEW-HISTORY.md` with these sections:

```markdown
# Review History — Auto-generated from PR Comments

Last generated: <date>
PRs analyzed: <count>

## Finding Frequency

| Finding pattern | Count | Last seen | PRs |
|---|---|---|---|
| Sensitive data in API responses | 5 | PR #98 | #88, #91, #93, #96, #98 |
| Debug functions in production | 3 | PR #96 | #88, #91, #96 |
| ... | ... | ... | ... |

## File Hot Spots

| File/directory | Findings | Recent PRs |
|---|---|---|
| src/handlers/auth.py | 8 | #93, #96 |
| services/payment-api/ | 6 | #88, #91, #98 |
| ... | ... | ... |

## Author Trends

| Author | Total findings | Blockers | Most common pattern |
|---|---|---|---|
| alice | 12 | 2 | Missing input validation |
| bob | 9 | 1 | Broad exception handling |
| ... | ... | ... | ... |

## Timeline

| PR | Date | Author | Findings | Blockers |
|---|---|---|---|---|
| #98 | 2026-04-03 | alice | 4 | 1 |
| #96 | 2026-04-01 | bob | 3 | 0 |
| ... | ... | ... | ... | ... |
```

This is raw analytical data — not curated patterns. REVIEW.md remains the authoritative pattern source. REVIEW-HISTORY.md is context for deeper analysis.

## Step 4.5: Recalculate SEVERITY-CALIBRATION.md

Source data: REVIEW-HISTORY.md (just regenerated) + ACCEPTED-PATTERNS.md (if exists).

For each combination of (agent name, finding category):
1. Count total findings from REVIEW-HISTORY.md
2. Count disputed findings (from timeline + accepted patterns entries)
3. Compute `dispute_rate = disputed / total`

Threshold logic (only apply when 10+ data points for that agent+category):
- `dispute_rate > 50%` → confidence threshold = 80 (majority of findings disputed — agent is noisy on this project)
- `dispute_rate > 40%` → confidence threshold = 75
- `dispute_rate < 10%` → confidence threshold = 50 (very few disputes — agent is well-calibrated, allow more findings)
- Otherwise → 60 (default)

Output format for `/tmp/SEVERITY-CALIBRATION.md`:
```markdown
# Severity Calibration — Auto-generated

Last recalculated: <date>
Data points: <total findings analyzed>

## Thresholds

| Agent | Category | Threshold | Reason | Data points |
|---|---|---|---|---|
| security-auditor | data-exposure | 75 | dispute rate 45% | 20 |
| code-reviewer | error-handling | 50 | dispute rate 5% | 20 |

## Default

For any agent+category not listed above: use threshold 60.
```

If fewer than 10 total data points across all agents, skip this step entirely — insufficient data for calibration.

## Step 4.7: Refresh GLOSSARY.md

**Only run if `/tmp/GLOSSARY.md` exists** (first-run already created it).

Scan `/tmp/REVIEW.md`, `/tmp/ACCEPTED-PATTERNS.md`, `CLAUDE.md`, and `README.md` from the repo root for domain-specific terms not yet in the glossary:
- Proper nouns (service names, tool names)
- Abbreviated terms (JWT, API, OTP)
- Business domain terms (guardrail, variant, tenant)

Append new terms. Do not remove existing terms. Format:
```markdown
# Project Glossary — Domain Terms

Last updated: <date>

| Term | Definition | Context |
|---|---|---|
| tenant | Isolated customer workspace | multi-tenancy |
| guardrail | Safety constraint on agent input/output | AI safety |
| idempotency key | Unique token preventing duplicate operations | payment API |
```

## Step 5: Report

Print a summary:
```
REVIEW.md cleanup:
- Merged N duplicate patterns
- Moved N patterns between sections
PROJECT-PROFILE.md: <refreshed / skipped (no profile yet)>
SEVERITY-CALIBRATION.md: <recalculated from N data points / skipped (insufficient data)>
GLOSSARY.md: <N new terms added / no new terms>
- Flagged N potentially stale patterns

REVIEW-HISTORY.md generated:
- Analyzed N PRs (N with review comments)
- N unique finding patterns
- Top hot spot: <file> (N findings)
- Top recurring: <pattern> (N occurrences)
```

## Step 6: Push to wiki

If `--dry-run` was specified, print the proposed content and stop.

Otherwise, push to the wiki:

```bash
WIKI_DIR="/tmp/review-wiki-learn"
if [ ! -d "$WIKI_DIR/.git" ]; then
  cd /tmp && git clone --depth 1 "$WIKI_URL" review-wiki-learn 2>/dev/null
fi
cp /tmp/REVIEW.md "$WIKI_DIR/REVIEW.md"
cp /tmp/REVIEW-HISTORY.md "$WIKI_DIR/REVIEW-HISTORY.md" 2>/dev/null
cp /tmp/PROJECT-PROFILE.md "$WIKI_DIR/PROJECT-PROFILE.md" 2>/dev/null
cp /tmp/ACCEPTED-PATTERNS.md "$WIKI_DIR/ACCEPTED-PATTERNS.md" 2>/dev/null
cp /tmp/SEVERITY-CALIBRATION.md "$WIKI_DIR/SEVERITY-CALIBRATION.md" 2>/dev/null
cp /tmp/GLOSSARY.md "$WIKI_DIR/GLOSSARY.md" 2>/dev/null
cd "$WIKI_DIR" && git add REVIEW.md REVIEW-HISTORY.md PROJECT-PROFILE.md ACCEPTED-PATTERNS.md SEVERITY-CALIBRATION.md GLOSSARY.md && { git diff --quiet --cached || git commit -m "review-learn: cleanup + calibration $(date +%Y-%m-%d)"; } && git push
rm -rf /tmp/review-wiki-learn
```

## Step 7: Update meta

After successful push, update the auto-trigger metadata:

```bash
echo '{"last_cleanup": "'$(date +%Y-%m-%d)'", "reviews_since": 0}' > $HOME/.claude/review-learn-meta.json
```

## Cleanup

```bash
rm -f /tmp/REVIEW.md /tmp/REVIEW-HISTORY.md /tmp/PROJECT-PROFILE.md /tmp/ACCEPTED-PATTERNS.md /tmp/SEVERITY-CALIBRATION.md /tmp/GLOSSARY.md
```
