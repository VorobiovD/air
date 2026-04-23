## Self-Review Flow (--self mode)

When `--self` is passed, this is a completely different flow. No PR needed. Reviews local changes before push.

### Self Step 1: Get the diff

```bash
git diff HEAD > /tmp/self-review.diff
```

If the diff is empty, try staged only:
```bash
git diff --cached > /tmp/self-review.diff
```

If still empty: "No changes to review. Stage or modify files first." and STOP.

Print summary: "<N> files changed, +<additions>/-<deletions>"

### Self Step 2: Load Context

Same as regular Step 3 — clone the wiki and copy ALL wiki pages to /tmp/ (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md, GLOSSARY.md). Also read CLAUDE.md from the repo root and the current repo's `.claude/agents/` for any repo-specific review rules. Run Step 3.5 (first-run project discovery — see `commands/review.md` Step 3.5) if PROJECT-PROFILE.md doesn't exist.

Also generate blame summaries and churn data for the changed files (same as Step 4's "Git history context") so all agents — including git-history-reviewer — have the data they need.

### Self Step 3: Full Review (4 agents + Codex)

Same quality as PR review. Construct a PR Context block (same structure as Step 7 in `commands/review.md`) with the self-review diff summary, blame summaries, churn data, and wiki page availability flags. Pass this context block to all agents. Launch ALL reviewers in parallel:

**Agent 1: Code Reviewer** - focused on YOUR changes:
- Bugs you might have introduced
- Error handling you forgot
- Edge cases in new logic
- Patterns from REVIEW.md that match your code

**Agent 2: Simplify** (read-only) - check your new code for:
- Duplication with existing code in the repo
- Unnecessary complexity you can simplify before anyone sees it
- Dead code or unused imports

**Agent 3: Security Auditor** - check:
- Did you accidentally log sensitive data (PII, credentials, tokens)?
- Any new SQL without parameterization?
- Any new endpoints missing auth?
- Secrets or credentials in the diff?
- Silent failures: empty catch blocks, ignored errors, fallback masking

**Agent 4: Git History Reviewer** - check your changes against:
- Recent churn on files you touched (are you in a refactor loop?)
- Blame context — modifying code you didn't write? Verify assumptions
- Previous PR feedback on these files

**Codex** (unless `--no-codex`):
```bash
CODEX_SCRIPT=$(find ~/.claude/plugins/cache/openai-codex -name "codex-companion.mjs" 2>/dev/null | sort -V | tail -1)
[ -n "$CODEX_SCRIPT" ] && node "$CODEX_SCRIPT" review "--base HEAD"
```
Codex reviews against HEAD (your uncommitted changes). Graceful skip if not configured.

### Self Step 4: Verification

Launch **review-verifier** on all findings, same as regular PR review. This prevents the fix plan from containing false positives that waste your time.

### Self Step 5: Generate Fix Plan

For each finding, generate a concrete fix plan:

```
=== Self-Review: <N> findings ===

1. [blocker] <description>
   File: <path>:<line>
   Current:  <the problematic code>
   Fix:      <the corrected code>
   Reason:   <why this matters>

2. [medium] <description>
   File: <path>:<line>
   Current:  <code>
   Fix:      <corrected code>
   Reason:   <why>

...

Apply fixes? [y/N/select]  (only if --fix was passed, otherwise just show the plan)
```

Requirements for the fix plan:
- Show the EXACT current code and the EXACT replacement
- Every fix must be a minimal, targeted change - don't refactor surrounding code
- Group by file for readability
- Blockers first, then medium, then low

### Self Step 6: Apply Fixes (if --fix)

If `--fix` was passed:
- Apply each fix using the Edit tool
- After all fixes applied, run the diff again to verify:
```bash
git diff HEAD
```
- Print: "Applied <N> fixes. Review the changes with `git diff` before committing."

If `--fix` was NOT passed:
- Just print the fix plan and stop
- Print: "Run with --fix to auto-apply, or fix manually."

### Self Step 7: Learn from self-review

**Self-review learns the same as regular review.** The full pipeline ran — patterns are just as valuable regardless of whether they came from a PR or a self-check.

1. Add new patterns from this review to REVIEW.md (same as Step 13 sub-steps 2 and 2.5 — author pattern lifecycle with clean-PR tracking, semantic dedup, all severity levels). For self-review, resolve the author using the same method as the own-PR guard: `gh api user --jq '.login'` (GitHub) or `glab api user 2>/dev/null | jq -r '.username'` (GitLab). This ensures the heading matches `### <author.login>` used in regular PR reviews.
2. Record any `WIKI DRIFT:` notes in `## Pending Drift` section
3. Push to wiki:
```bash
WIKI_DIR="/tmp/review-wiki-self"
WIKI_URL="https://$PLATFORM_DOMAIN/$CURRENT_REPO.wiki.git"
if [ ! -d "$WIKI_DIR/.git" ]; then
  cd /tmp && git clone --depth 1 "$WIKI_URL" review-wiki-self 2>/dev/null
fi
cp /tmp/REVIEW.md "$WIKI_DIR/REVIEW.md"
cp /tmp/ACCEPTED-PATTERNS.md "$WIKI_DIR/ACCEPTED-PATTERNS.md" 2>/dev/null
cd "$WIKI_DIR" && git add REVIEW.md ACCEPTED-PATTERNS.md && { git diff --quiet --cached || git commit -m "review: self-review patterns $(date +%Y-%m-%d)"; } && git push
```
4. Increment the review counter AND check auto-trigger threshold:
```bash
META_FILE="$HOME/.claude/air:learn-meta.json"
if [ -f "$META_FILE" ]; then
  LAST_CLEANUP=$(cat "$META_FILE" | grep -o '"last_cleanup" *: *"[^"]*"' | grep -o '[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}')
  REVIEWS_SINCE=$(cat "$META_FILE" | grep -o '"reviews_since" *: *[0-9]*' | grep -o '[0-9]*$')
  CLEANUP_EPOCH=$(date -j -f "%Y-%m-%d" "$LAST_CLEANUP" +%s 2>/dev/null || date -d "$LAST_CLEANUP" +%s 2>/dev/null || echo 0)
  DAYS_SINCE=$(( ($(date +%s) - $CLEANUP_EPOCH) / 86400 ))
else
  REVIEWS_SINCE=99
  DAYS_SINCE=99
fi
echo "Auto-trigger check: reviews_since=$REVIEWS_SINCE, days_since=$DAYS_SINCE (threshold: 5 reviews or 2 days)"
```

**>>> AUTO-TRIGGER DECISION (do NOT skip) <<<**

If `REVIEWS_SINCE >= 5` OR `DAYS_SINCE >= 2`:
1. Print "Triggering /air:learn (reviews: $((REVIEWS_SINCE + 1)), days: $DAYS_SINCE)"
2. Run `/air:learn` (full cleanup + KAIROS history regeneration)
3. After learn completes, RETURN — do not fall through to the counter increment below

Otherwise (threshold not met), increment the counter:
```bash
echo '{"last_cleanup": "'$LAST_CLEANUP'", "reviews_since": '$((REVIEWS_SINCE + 1))'}' > "$META_FILE"
```

Only skip wiki push if zero findings (clean self-review with nothing to learn).

### Self Cleanup

```bash
rm -f /tmp/self-review.diff /tmp/REVIEW.md /tmp/REVIEW-HISTORY.md /tmp/PROJECT-PROFILE.md /tmp/ACCEPTED-PATTERNS.md /tmp/SEVERITY-CALIBRATION.md /tmp/GLOSSARY.md
rm -rf /tmp/review-wiki-self
```
