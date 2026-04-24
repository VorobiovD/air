## Self-Review Flow (--self mode)

When `--self` is passed, this is a completely different flow. No PR needed. Reviews local changes before push.

### Self Step 0: Initialize Session Temp Directory

Before any `/tmp` write, mint a per-invocation session dir so parallel `/air:review --self` runs (or a review + self-review in two Claude Code sessions) don't corrupt each other's wiki files and diffs. Claude Code's Bash tool starts a fresh shell per call, so `export` doesn't persist — capture the literal path from the command below and substitute it into every `$AIR_TMP` reference downstream.

If the orchestrator already minted `$AIR_TMP` (e.g. `/air:review --self` routed through review.md Step 0), reuse it — don't double-mint. Otherwise mint a fresh dir:

```bash
if [ -z "$AIR_TMP" ]; then
  find /tmp -maxdepth 1 -name 'air-*' -mtime +1 -exec rm -rf {} + 2>/dev/null
  AIR_TMP=$(mktemp -d "/tmp/air-self-XXXXXX")
fi
echo "$AIR_TMP"
```

### Self Step 1: Get the diff

```bash
git diff HEAD > $AIR_TMP/self-review.diff
```

If the diff is empty, try staged only:
```bash
git diff --cached > $AIR_TMP/self-review.diff
```

If still empty: "No changes to review. Stage or modify files first." and STOP.

Print summary: "<N> files changed, +<additions>/-<deletions>"

### Self Step 2: Load Context

Same as regular Step 3 — clone the wiki (into `$AIR_TMP/review-wiki-self`) and copy ALL wiki pages to `$AIR_TMP/` (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md, GLOSSARY.md). Also read CLAUDE.md from the repo root and the current repo's `.claude/agents/` for any repo-specific review rules. Run Step 3.5 (first-run project discovery — see `commands/review.md` Step 3.5) if PROJECT-PROFILE.md doesn't exist.

Also generate blame summaries and churn data for the changed files (same as Step 4's "Git history context") so all agents — including git-history-reviewer — have the data they need.

### Self Step 3: Full Review (4 agents + Codex)

Same quality as PR review. Construct a PR Context block (same structure as Step 7 in `commands/review.md`) with the self-review diff summary, blame summaries, churn data, and — critically — the two-field wiki contract:

```
- Wiki files directory: <literal $AIR_TMP path — e.g. /tmp/air-self-AbCdEf>
- Wiki files available in that directory: <list which of REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md, GLOSSARY.md actually exist>
```

The 5 agents require the literal `Wiki files directory:` field to locate wiki patterns — without it they proceed without patterns. Pass this context block to all agents. Launch ALL reviewers in parallel:

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
WIKI_DIR="$AIR_TMP/review-wiki-self"
WIKI_URL="https://$PLATFORM_DOMAIN/$CURRENT_REPO.wiki.git"
if [ ! -d "$WIKI_DIR/.git" ]; then
  cd "$AIR_TMP" && git clone --depth 1 "$WIKI_URL" review-wiki-self 2>/dev/null
fi
cp "$AIR_TMP/REVIEW.md" "$WIKI_DIR/REVIEW.md"
cp "$AIR_TMP/ACCEPTED-PATTERNS.md" "$WIKI_DIR/ACCEPTED-PATTERNS.md" 2>/dev/null
cd "$WIKI_DIR" && git add REVIEW.md ACCEPTED-PATTERNS.md .air-meta.json && { git diff --quiet --cached || git commit -m "review: self-review patterns $(date +%Y-%m-%d)"; } && git push
```
4. Bump the shared wiki-backed review counter and check the auto-trigger threshold. Counter state lives in `.air-meta.json` at the wiki root so CLI and managed runs share the same number — both contribute to the cadence:

```bash
python3 "$AIR_PLUGIN_ROOT/lib/meta.py" bump --wiki-dir "$WIKI_DIR" --pr-number 0
if python3 "$AIR_PLUGIN_ROOT/lib/meta.py" check --wiki-dir "$WIKI_DIR"; then
  echo "Auto-trigger: threshold not met — self-review done"
else
  echo "Auto-trigger: running /air:learn"
  # Run /air:learn. Its own epilogue resets the counter via `meta.py reset`.
fi
```

Threshold rules (enforced in `meta.py`): `reviews_since >= 5`, or `days_since_cleanup >= 2` AND `reviews_since > 0`. A self-review counts as a review for counter purposes — no PR number needed (pass 0).

Include `.air-meta.json` in the wiki-push from sub-step 3 above (add it to the `git add` line).

Only skip wiki push if zero findings (clean self-review with nothing to learn).

### Self Cleanup

```bash
[ -n "$AIR_TMP" ] && rm -rf "$AIR_TMP"
```
