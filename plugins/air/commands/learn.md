---
description: Clean up, deduplicate, and reorganize the wiki REVIEW.md using AI. Also regenerates REVIEW-HISTORY.md from PR comment history.
argument-hint: [--dry-run] [--history-only] [--refresh-profile]
---

Fetch REVIEW.md from the wiki, clean it up using AI, generate REVIEW-HISTORY.md from PR comment history, and push both back.

Note: `/air:review` auto-triggers this command every 15 reviews or every 14 days (whichever comes first). You can also run it manually for immediate cleanup.

> **Store-backed repos (managed-agent fleet):** when a repo's patterns live in an Anthropic memory store (the source of truth), the canonical `/air:learn` is the managed `air-learner` session, which curates the store; a deterministic Python step (`managed/render_store_to_wiki.py`) then exports the git-wiki mirror. **This CLI command never reads or renders the store** — it operates on the git wiki directly. On a store-backed repo it still works on whatever the wiki holds, but the store stays untouched and the managed render will overwrite the wiki on its next run. For store repos, run the managed learn; reserve this CLI flow for legacy wiki-only repos.

**Flags:**
- `--dry-run` — preview changes without pushing to wiki
- `--history-only` — only regenerate REVIEW-HISTORY.md, don't touch REVIEW.md
- `--refresh-profile` — re-run the full Opus deep scan for PROJECT-PROFILE.md + GLOSSARY.md (same as first-run discovery). Use when the project has changed significantly (new language, new service, major restructure). Overwrites existing profile and glossary with fresh scan results.

## Platform Detection

Same as `/air:review` — detect platform from git remote URL (or `AIR_PLATFORM` env var override). See review.md "Platform Detection" section for full logic. Sets `PLATFORM`, `PLATFORM_DOMAIN`, and `CLI`.

All `gh` commands below are written for GitHub. On GitLab, translate using platform-gitlab.md — same as review.md.

## Step 0: Initialize Session Temp Directory

Before any `/tmp` write, mint a per-invocation session dir so a `/air:learn` run and a parallel `/air:review` (or two parallel `/air:learn`) don't overwrite each other's wiki pattern files. Capture the printed path and substitute it into every `$AIR_TMP` reference downstream.

```bash
find /tmp -maxdepth 1 -name 'air-*' -mtime +1 -exec rm -rf {} + 2>/dev/null
# Sweep KAIROS cache entries older than 30 days — the cache is persistent across runs
# (reused for KAIROS history regeneration) but needs bounded growth.
find "$HOME/.cache/air/kairos" -type f -name '*.json' -mtime +30 -delete 2>/dev/null
AIR_TMP=$(mktemp -d "/tmp/air-learn-XXXXXX")
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
# Plugin root for the meta.py reset invocation in Step 7. Same derivation
# as review.md / review-self.md Step 0. If the env override and glob both
# miss, the var is cleared below and Step 7 skips the counter reset (the
# wiki review-comment regeneration in Steps 4-6 still runs).
if [ -z "${AIR_PLUGIN_ROOT:-}" ]; then
  AIR_PLUGIN_ROOT=$(ls -1d ~/.claude/plugins/cache/air/air/*/ 2>/dev/null | sort -V | tail -1 | sed 's:/$::')
fi
if [ -z "$AIR_PLUGIN_ROOT" ] || [ ! -d "$AIR_PLUGIN_ROOT" ]; then
  echo "warning: AIR_PLUGIN_ROOT not resolvable; meta.py reset in Step 7 will be skipped" >&2
  AIR_PLUGIN_ROOT=""
fi
echo "$AIR_TMP"
```

## Step 1: Fetch from wiki

```bash
# GitHub: gh repo view --json nameWithOwner --jq '.nameWithOwner'
# GitLab: glab api "projects/$(echo $REMOTE_PATH | sed 's|/|%2F|g')" 2>/dev/null | jq -r '.path_with_namespace'
CURRENT_REPO=$(gh repo view --json nameWithOwner --jq '.nameWithOwner' 2>/dev/null)
WIKI_URL="https://$PLATFORM_DOMAIN/$CURRENT_REPO.wiki.git"
cd "$AIR_TMP" && git clone --depth 1 "$WIKI_URL" review-wiki-learn 2>/dev/null
```

If the clone succeeded (the directory `$AIR_TMP/review-wiki-learn/.git` exists), copy whichever pattern files exist. Each copy is independent — on a first run the wiki may have no pattern files yet:
```bash
WIKI_DIR="$AIR_TMP/review-wiki-learn"
if [ -d "$WIKI_DIR/.git" ]; then
  cp "$WIKI_DIR/REVIEW.md" "$AIR_TMP/REVIEW.md" 2>/dev/null
  cp "$WIKI_DIR/REVIEW-HISTORY.md" "$AIR_TMP/REVIEW-HISTORY.md" 2>/dev/null
  cp "$WIKI_DIR/PROJECT-PROFILE.md" "$AIR_TMP/PROJECT-PROFILE.md" 2>/dev/null
  cp "$WIKI_DIR/ACCEPTED-PATTERNS.md" "$AIR_TMP/ACCEPTED-PATTERNS.md" 2>/dev/null
  cp "$WIKI_DIR/SEVERITY-CALIBRATION.md" "$AIR_TMP/SEVERITY-CALIBRATION.md" 2>/dev/null
  cp "$WIKI_DIR/GLOSSARY.md" "$AIR_TMP/GLOSSARY.md" 2>/dev/null
fi
```

If the clone failed (no `.git` directory): print "Wiki not found — create at https://$PLATFORM_DOMAIN/$CURRENT_REPO/-/wikis (GitLab) or https://$PLATFORM_DOMAIN/$CURRENT_REPO/wiki (GitHub)" and STOP.

**GitLab note:** After `CURRENT_REPO` is set, resolve the project ID for API calls: `PROJECT_ID=$(glab api "projects/$(echo $CURRENT_REPO | sed 's|/|%2F|g')" 2>/dev/null | jq -r '.id')`

If `--history-only` was passed, skip to Step 4 (only regenerate history, don't touch REVIEW.md).

## Step 2: Analyze REVIEW.md

Read the entire file and analyze every pattern entry for:

- **Semantic duplicates** — patterns that describe the same issue in different words. Merge into one, keeping the best wording. Examples:
  - "raw third-party response proxied to callers" + "unfiltered external API forwarded in error details" → one pattern
  - For author patterns, merge duplicates within the same author only. Combine occurrence counts and PR refs. Use the higher clean counter.
- **Stale patterns (Common Findings and Service-Specific only)** — patterns that were fixed project-wide and no longer relevant. Check recent PRs if needed. Mark as potentially stale but don't remove without confirming.
- **Author patterns: NEVER remove or mark as stale.** Author patterns are behavioral tendencies managed by the review pipeline's clean-PR tracking (review.md Step 13). A single fix proves awareness, not behavior change. The lifecycle handles transitions automatically:
  - Active patterns have `(<Nx>: <PR refs> | last <N> PRs: <M> clean)` metadata
  - After 5 clean PRs → marked `(declining)`
  - After 10 clean PRs → moved to `### <author> (archived)` subsection
  - Archived patterns stay permanently — never delete them
  - The ONLY valid operations on author patterns during learn are: merging semantic duplicates (within the same author), fixing formatting to match lifecycle format, and migrating legacy entries
- **Legacy author pattern format** — if any author patterns use the old format (specific incidents like "PR #3470 compared $mif instead of $mif_id" without lifecycle metadata), migrate to the new format:
  ```
  - **<Pattern name>** (<Nx>: <PR refs> | last 0 PRs: 0 clean): <Generalized behavioral tendency>
  ```
  Generalize the incident into a behavioral tendency. Count PR refs for occurrence count. Set clean counter to 0 (unknown history).
- **Misplaced patterns** — author patterns that should be service patterns (or vice versa). When moving an author pattern to Common Findings, strip lifecycle metadata (counts, PR refs, clean counter).
- **Vague patterns** — entries too generic to be actionable. Make them specific or remove. For author patterns, rewrite to be a specific behavioral tendency rather than removing.
- **Patterns in wrong section** — if learned from one author but applies to everyone, move to Common Findings.
- **Accepted patterns / false positive calibration in REVIEW.md** — if REVIEW.md has a section named "Accepted Patterns", "False Positive Calibration", or similar, migrate ALL entries to `$AIR_TMP/ACCEPTED-PATTERNS.md` (create if it doesn't exist). Format: `- **<pattern>**: <description> (migrated from REVIEW.md)`. Then DELETE that entire section from REVIEW.md. ACCEPTED-PATTERNS.md is the sole store for suppression patterns.

## Step 3: Reorganize REVIEW.md

- Alphabetize authors within Author Patterns
- **Author Patterns structure:** Each author has a `### <author-login>` subsection. Archived authors have a separate `### <author-login> (archived)` subsection at the bottom of Author Patterns. Ensure every author pattern entry uses the lifecycle format: `- **<Pattern name>** (<Nx>: <PR refs> | last <N> PRs: <M> clean): <Description>`. Fix any entries that don't match.
- Group related patterns within each section (security together, config together, etc.)
- Ensure Common Findings and Service-Specific sections don't exceed ~15 patterns — promote the most general ones to Common Findings. **Do NOT cap Author Patterns** — each author's patterns are their own namespace and must be preserved through the lifecycle.
- **Cap each pattern entry's inline narrative** at the 3 most recent PR examples (~1,500 chars of prose). **Counts are never dropped; long PR-ref enumerations are windowed to the most-recent ~8 refs (count preserved).** Move older example narratives verbatim to `REVIEW-ARCHIVE.md` (create if missing) and leave a `(older examples: see REVIEW-ARCHIVE.md)` marker in the entry. Rationale: every review session loads REVIEW.md into 3-5 agent contexts; single entries have grown >15K chars (one line), overflowing agent tool-output limits and dominating session token cost. (Step 6 runs `wiki_cap.py` after the push as a deterministic byte-ceiling backstop — this prose-trim reduces what it must drop.)
- If a compliance reference section exists (e.g., HIPAA Quick Reference for healthcare projects), keep it unchanged (it's a reference, not learned patterns)

Generate the cleaned-up REVIEW.md content.

## Step 3.5: Refresh PROJECT-PROFILE.md

**If `--refresh-profile` was passed:** Run the full Opus deep scan (same as `/air:review` Step 3.5 first-run discovery). This overwrites the existing PROJECT-PROFILE.md and GLOSSARY.md with fresh results. Use when the project has changed significantly — new language, new service, major restructure, or when agents have flagged wiki drift.

**Otherwise (default):**

**If `$AIR_TMP/PROJECT-PROFILE.md` does NOT exist** (first run on this project): Run the full Opus deep scan (same as `/air:review` Step 3.5 first-run discovery — same agent prompt, same three outputs). Print "No PROJECT-PROFILE.md found — running first-run discovery." The scan generates three artifacts:
- `$AIR_TMP/PROJECT-PROFILE.md` (pushed to wiki in Step 6)
- `$AIR_TMP/GLOSSARY.md` (pushed to wiki in Step 6)
- `$REPO_ROOT/.air-checks.sh` (written mode 644 to the repo — see Step 4.65 Branch A for the full generation rules; the deep-scan agent should reuse them here rather than re-deriving)

Skip the lightweight refresh below AND Step 4.65 Branch A (generation was just done). Step 4.65 Branch B (augmentation with pattern-derived suggestions) still runs normally.

**If `$AIR_TMP/PROJECT-PROFILE.md` exists** (lightweight refresh): File-based detection only (~2s, no Opus agent):
```bash
# Detect new/removed manifest files
ls go.mod package.json requirements.txt composer.json Makefile Dockerfile *.tf template.yaml 2>/dev/null
# Detect new top-level service directories
ls -d */ 2>/dev/null | head -20
```

Update the `## Languages` and `## Services` sections in `$AIR_TMP/PROJECT-PROFILE.md` — REPLACE those sections in place; never append a per-pass changelog narrative (global anti-bloat rule). The profile describes the repo's CURRENT structure; the header is a single `Last updated: <date>` line. If the existing profile already carries accumulated "Nth pass / since previous pass" narrative, strip it to the current-state description (PROJECT-PROFILE.md has bloated past 170KB this way). Do NOT touch:
- "Review Focus Rules" section — manually curated after initial generation
- "Applicable Security Checks" section — unless a new language/framework was detected (e.g., SQL files appeared for the first time → add the SQL injection check)

**Auto-trigger deep refresh:** If the lightweight scan detects a significant change (new manifest file type that didn't exist before, e.g., `package.json` appearing in a Go-only project), automatically escalate to a full Opus deep scan for this run. Print: "New framework detected — running full profile refresh."

## Step 4: Generate REVIEW-HISTORY.md (KAIROS)

Fetch all review comments from recent closed/merged PRs and extract finding history.

**IMPORTANT — two-phase approach to avoid API timeouts:** A naive loop of 30 PRs × 2 API calls each = 60+ sequential calls, which easily exceeds 2-minute shell timeouts. Use this two-phase strategy:

**Phase 1: Identify PRs with reviews and cache their issue comments.**
```bash
# Fetch last 30 closed/merged PRs
# GitLab: use projects/$PROJECT_ID/merge_requests?state=merged&per_page=30&order_by=updated_at&sort=desc, use .iid not .number
RECENT_PRS=$(gh api "repos/$CURRENT_REPO/pulls?state=closed&per_page=30&sort=updated&direction=desc" --jq '.[] | select(.merged_at != null) | .number' 2>/dev/null)

# Fetch issue comments for each PR, cache to temp file, check for air reviews
# Note: gh api fetches full comment bodies — the jq filter runs client-side on the full response
REVIEWED_PRS=""
# Namespace cache by repo so PR #5 in repo A doesn't collide with PR #5 in repo B
KAIROS_CACHE="$HOME/.cache/air/kairos/$(echo $CURRENT_REPO | tr / _)"
mkdir -p "$KAIROS_CACHE"
for PR_NUM in $RECENT_PRS; do
  gh api "repos/$CURRENT_REPO/issues/$PR_NUM/comments" > "$KAIROS_CACHE/$PR_NUM.json" 2>/dev/null
  HAS_REVIEW=$(cat "$KAIROS_CACHE/$PR_NUM.json" | python3 -c "
import json, sys
comments = json.loads(sys.stdin.buffer.read())
print(sum(1 for c in comments if c['body'].startswith('## Code Review')))
" 2>/dev/null)
  if [ "$HAS_REVIEW" -gt 0 ]; then
    REVIEWED_PRS="$REVIEWED_PRS $PR_NUM"
  fi
done
```

**Phase 2: Fetch inline comments + extract air reviews from cached data.**
```bash
for PR_NUM in $REVIEWED_PRS; do
  # Get review comments (inline code comments) — this is the only new API call per reviewed PR
  # GitLab: projects/$PROJECT_ID/merge_requests/$PR_NUM/discussions
  gh api "repos/$CURRENT_REPO/pulls/$PR_NUM/comments" --jq '.[] | {pr: '$PR_NUM', path: .path, body: (.body | split("\n")[0][:200])}' 2>/dev/null

  # Extract air reviews from cached Phase 1 data (no API call)
  cat "$KAIROS_CACHE/$PR_NUM.json" | python3 -c "
import json, sys
pr_num = int(sys.argv[1])
comments = json.loads(sys.stdin.buffer.read())
for c in comments:
    if c['body'].startswith('## Code Review'):
        print(json.dumps({'pr': pr_num, 'body': c['body']}))
" "$PR_NUM"
done
# KAIROS cache persists under $HOME/.cache/air/kairos/<repo>/ (reused across runs).
# Cleanup is the Step 0 find-mtime+30 sweep; no per-run cleanup.
```

Phase 1 makes 30 API calls (one per PR) and caches the responses. Phase 2 reuses the cached issue comments (0 extra calls) and only fetches inline review comments for reviewed PRs (typically 3-10 calls). Total: ~33-40 calls instead of 60.

**Sensitive data safety:** Do NOT fetch `diff_hunk` from review comments — it may contain secrets, credentials, PII, or other sensitive data. Only fetch `path` and `body` (first 200 chars).

**Rate limiting:** If any API call returns 403/429, pause for 5 seconds and retry once. Cap total API calls at 100. If Phase 1 alone approaches the cap, reduce `per_page` to 15.

**Anti-bloat (parity with managed `learn-orchestrator.md` Step 4):** the bloat that pushed REVIEW-HISTORY.md past 550KB is the **per-PR narrative** (≈30 lines/PR for every PR ever) — that is what gets windowed; the aggregate tables are bounded by pattern/author/file count and stay CUMULATIVE.
- **Timeline table — WINDOWED to the fetched ~30 PRs only.** Drop older per-PR rows + per-PR narrative prose (git history retains them). Big size win.
- **Finding Frequency table — CUMULATIVE lifetime aggregate.** Carry forward prior lifetime counts and ADD the new window; do NOT reset to the 30-PR window (losing "Asymmetric refactor: 169x across 85 PRs" destroys the load-bearing signal). One row per pattern — bounded.
- **Author Trends + File Hot Spots — CUMULATIVE**, same carry-forward, one row each per author / hot file.
- Preserve any **Observations** narrative explaining cross-PR reasoning (not regenerable).
- Header is the title + `Last generated: <date>` + `PRs analyzed: <count>` — never a per-pass changelog narrative. Same no-narrative rule applies to GLOSSARY.md and PROJECT-PROFILE.md.

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

| Author | Total findings | Blockers | Most common pattern | Clean PRs (consecutive) | PRs reviewed |
|---|---|---|---|---|---|
| alice | 12 | 2 | Missing input validation | 2 | 15 |
| bob | 9 | 1 | Broad exception handling | 0 | 12 |
| ... | ... | ... | ... | ... | ... |

"Clean PRs" = consecutive merged PRs by this author where no findings matched their REVIEW.md author patterns. "PRs reviewed" = total merged PRs by this author in the analyzed set.

**Reconciliation (windowed-safe — keep identical to managed `learn-orchestrator.md` Step 4):** REVIEW.md author-pattern counters are authoritative and CUMULATIVE; the windowed Author Trends clean-PR count is corroboration only, never the source of truth. For each author with patterns in REVIEW.md, compare `Clean PRs (consecutive)` from REVIEW-HISTORY.md against `last <N> PRs: <M> clean` in REVIEW.md, then:
1. If the window shows MORE clean PRs than REVIEW.md records (counters missed during incremental learns), bump REVIEW.md up to the window value and apply lifecycle transitions if thresholds are now met (5 → declining, 10 → archive).
2. If the window shows FEWER clean PRs, reset DOWN only when a triggered pattern inside the window explains the gap; NEVER lower a counter merely because older clean PRs fell outside the fetched window.
3. When in doubt, keep the higher REVIEW.md value.
4. Print any reconciliation adjustments in the Step 5 report.

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

Output format for `$AIR_TMP/SEVERITY-CALIBRATION.md`:
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

## Step 4.65: Generate or augment .air-checks.sh

Skip if `CROSS_REPO=true`. Skip if `$REPO_ROOT` is empty (not invoked from inside a git repo — no sensible place to write the script). Otherwise branch on existence of `$REPO_ROOT/.air-checks.sh`:

### Branch A — file does NOT exist: generate it

Bootstrap from the already-loaded `$AIR_TMP/PROJECT-PROFILE.md` (no second deep-scan needed — everything required is already in the profile). Generate a `.air-checks.sh` tailored to what the profile describes:

- Standard header: `#!/bin/bash`, `set -u`, `status=0`, `fail()` helper that writes `  [FAIL] <msg>` to stderr and sets `status=1`
- Invoke built-ins near the top:
  ```
  if [ -n "${AIR_PLUGIN_ROOT:-}" ] && [ -x "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" ]; then
    "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" || status=1
  fi
  ```
- Project-specific extras derived from PROJECT-PROFILE.md:
  - If the profile lists mirror doc files (e.g., plugin-level README alongside root, docs/README.md duplicating sections): add consistency greps
  - If the profile mentions numbered-item conventions ("31-item checklist", "5 agents"): add count checks
  - If the profile lists sentinel strings that must stay consistent across files: add byte-identity greps
  - Skip generic version-check logic — built-ins handle that
- Ends with: `exit $status`
- Include a commented banner (canonical form — must match what `/air:review` Step 3.5 emits so users see the same sentinel whether the file came from the review flow or the learn flow):
  ```
  # Generated by air (<source>, <date>). Review, chmod +x, and commit to enable pre-commit drift checks.
  ```
  where `<source>` is `/air:review` (when called from review.md Step 3.5) or `/air:learn` (when called from learn.md Step 4.65 Branch A). Keep the rest of the sentence byte-identical.
- Include a commented "How to customize" section at the bottom

**Safety rails for generation:**
- Write to `$REPO_ROOT/.air-checks.sh` with mode `644` (non-executable — user must `chmod +x` to enable)
- Skip if PROJECT-PROFILE.md doesn't exist (can't tailor without the profile)
- Print: `"Generated .air-checks.sh at $REPO_ROOT/.air-checks.sh (from PROJECT-PROFILE.md). Review it, 'chmod +x' to enable, then commit."`

### Branch B — file exists: augment with pattern-derived suggestions

Inspect recurring Author Patterns in the cleaned REVIEW.md — specifically entries with `(Nx: ...)` where `N >= 3`. For each pattern with a concrete drift shape that can be codified as a shell grep (most commonly "Stale documentation references" with a specific mirror-file or version-string shape), propose a commented-out check to append at the bottom of `.air-checks.sh`.

**Rules:**

- Read the existing `.air-checks.sh`. Parse its content to identify:
  - Checks already active (uncommented `grep` / `fail` lines)
  - Suggestions already appended in prior runs (commented lines starting with `# Suggested by /air:learn`)
- De-duplicate: if a suggestion's grep pattern is already present (active OR commented), do NOT re-append.
- For each new qualifying pattern, append a block like:
  ```
  # Suggested by /air:learn (<date>): based on <N> recurring findings in "<Pattern name>".
  # Uncomment to enable:
  # <grep command> \
  #   || fail "<specific failure message>"
  ```
- Cap at **3 new suggestions per run** — avoid flooding. If more qualify, pick the N highest-count patterns.
- Only derive suggestions from Author Patterns whose description mentions specific file paths, version strings, sentinel phrases, or mirror-file concepts. Skip vague patterns ("code quality", "error handling") — they don't map cleanly to greps.

**Output:**
- Append to `$REPO_ROOT/.air-checks.sh` (not the wiki).
- Preserve the file's existing mode (don't chmod).
- Print: `"Appended N drift-check suggestions to .air-checks.sh (commented). Review + uncomment to enable."`

**Example** — if REVIEW.md has:
```
- **Stale documentation references** (5x: #1, #11, #12, #20, #21 | 0 clean): ... specifics include "Project Structure tree in CLAUDE.md not updated when new top-level directories are added" ...
```
then append (choose examples that go BEYOND what `builtin-checks.sh` already covers — version-string / shields-badge / "currently" / "**Version:**" drift is already built-in; focus on structural, sentinel-string, or count invariants):
```
# Suggested by /air:learn (2026-04-23): based on 5 recurring findings in "Stale documentation references".
# Uncomment to enable:
# for d in $(ls -d plugins/*/ 2>/dev/null); do
#   name=$(basename "$d")
#   grep -q "^├── $name/\|^│   ├── $name/" CLAUDE.md \
#     || fail "CLAUDE.md Project Structure tree is missing plugin directory '$name'"
# done
```

**Do NOT suggest checks that duplicate `builtin-checks.sh`** — version badges, "currently X.Y.Z" lines, and `**Version:** X.Y.Z` headers are already covered there. Appending such suggestions leads to double-reporting on any uncomment. Favor structural/sentinel checks that the built-ins can't do.

The user reads the suggestion on their next pull/pre-commit, decides whether to uncomment, and commits.

## Step 4.7: Refresh GLOSSARY.md (bounded, surgical — terse by default, keep rules/gotchas)

**Only run if `$AIR_TMP/GLOSSARY.md` exists** (first-run already created it). Skip if `--refresh-profile` already regenerated it this run (Step 3.5). Bounded remediation pass (parity with managed `learn-orchestrator.md` Step 4.7) — the glossary is read into 3-5 agent contexts every review, so its size is direct cost. It is NOT a changelog.

Scan `$AIR_TMP/REVIEW.md`, `$AIR_TMP/ACCEPTED-PATTERNS.md`, `CLAUDE.md`, and `README.md` from the repo root for domain-specific terms (proper nouns / service + tool names; abbreviated terms like JWT, OTP; business domain terms like guardrail, variant, tenant). Each term is ONE table row: `| `Term` | Definition | source/PR |`.

Rules (surgical — NOT a blanket truncation, NOT append-only):
- **Definitions are terse by DEFAULT (~200 chars: what the term IS).** BUT keep IN FULL any definition that encodes a non-obvious **governance rule, gotcha, or safety property** — that knowledge lives nowhere in the code and is the glossary's whole value (e.g. "Octane workers hold stale Mongo/IAM clients across rotations — run `octane:reload`"; "`->visibleTo($user)` mandated on every user-facing query per AGENTS.md §14.5"; "Pennant caches the resolved closure — env flips need `pennant:purge`"; per-env security-gate defaults + failure modes).
- **What to TRIM is per-PR changelog prose** restating a term's review history ("introduced #959, then #961 L3 deferred…", deferred-finding annotations, round-by-round notes) → collapse to a single source/PR ref. Test: *is this a rule/gotcha that lives only here (KEEP), or PR-history a reviewer re-derives from the next PR (TRIM)?*
- ADD genuinely new terms. PRESERVE the full term set + each term's source; drop only terms no longer referenced anywhere.
- STRIP the header to the title + a single `Last updated: <date>, HEAD <sha>` line. Delete any accumulated "Nth cleanup pass / since the previous pass / new terms this pass" preamble (global anti-bloat rule).

Surgical, not blanket: repo-A's glossary reached 261KB mostly from an 18KB header essay + entries padded with PR-by-PR history — strip those and the governance-bearing rows for the same ~300 terms fit comfortably, without losing a rule or gotcha.

```markdown
# Project Glossary — Domain Terms

Last updated: <date>, HEAD <sha>

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
- Author patterns: N active, N declining, N archived (across N authors)
- Migrated N legacy author patterns to lifecycle format
PROJECT-PROFILE.md: <refreshed / skipped (no profile yet)>
SEVERITY-CALIBRATION.md: <recalculated from N data points / skipped (insufficient data)>
GLOSSARY.md: <N new terms added / no new terms>
- Flagged N potentially stale patterns (Common Findings / Service-Specific only)

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
WIKI_DIR="$AIR_TMP/review-wiki-learn"
if [ ! -d "$WIKI_DIR/.git" ]; then
  cd "$AIR_TMP" && git clone --depth 1 "$WIKI_URL" review-wiki-learn 2>/dev/null
fi
cp "$AIR_TMP/REVIEW.md" "$WIKI_DIR/REVIEW.md"
cp "$AIR_TMP/REVIEW-HISTORY.md" "$WIKI_DIR/REVIEW-HISTORY.md" 2>/dev/null
cp "$AIR_TMP/PROJECT-PROFILE.md" "$WIKI_DIR/PROJECT-PROFILE.md" 2>/dev/null
cp "$AIR_TMP/ACCEPTED-PATTERNS.md" "$WIKI_DIR/ACCEPTED-PATTERNS.md" 2>/dev/null
cp "$AIR_TMP/SEVERITY-CALIBRATION.md" "$WIKI_DIR/SEVERITY-CALIBRATION.md" 2>/dev/null
cp "$AIR_TMP/GLOSSARY.md" "$WIKI_DIR/GLOSSARY.md" 2>/dev/null
cp "$AIR_TMP/REVIEW-ARCHIVE.md" "$WIKI_DIR/REVIEW-ARCHIVE.md" 2>/dev/null
# Deterministic bloat-cap backstop (the advisory→enforced counterpart to the
# soft size caps above): hard-caps each wiki file by bytes in place, trimming
# only mechanical bloat (over-long glossary defs, ref-lists, narrative) — never
# a rule/term/count, fail-open. Logs [cap] lines to stderr. Skipped (no-op) if
# the plugin root is unresolved.
if [ -n "${AIR_PLUGIN_ROOT:-}" ] && [ -f "$AIR_PLUGIN_ROOT/lib/wiki_cap.py" ]; then
  python3 "$AIR_PLUGIN_ROOT/lib/wiki_cap.py" --dir "$WIKI_DIR" >&2
fi
cd "$WIKI_DIR" && git add REVIEW.md REVIEW-HISTORY.md PROJECT-PROFILE.md ACCEPTED-PATTERNS.md SEVERITY-CALIBRATION.md GLOSSARY.md REVIEW-ARCHIVE.md && { git diff --quiet --cached || git commit -m "review-learn: cleanup + calibration $(date +%Y-%m-%d)"; } && git push
```

## Step 7: Update meta

After successful push, reset the shared auto-trigger counter so the next review sees `reviews_since: 0` and the cadence restarts. **Store-backed repos** keep the counter in the per-repo pattern memory store — reset it there and skip the wiki commit below entirely:

```bash
if [ -n "$AIR_PLUGIN_ROOT" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  AIR_STORE_ID=$(python3 "$AIR_PLUGIN_ROOT/lib/meta.py" find-store --repo "$CURRENT_REPO")
  if [ -n "$AIR_STORE_ID" ]; then
    python3 "$AIR_PLUGIN_ROOT/lib/meta.py" reset --store-id "$AIR_STORE_ID" --pr-number 0
    # Counter lives in the store — no wiki .air-meta.json commit needed.
    # RETURN from Step 7 here.
  fi
fi
```

Legacy repos reset the wiki copy (`.air-meta.json`) — both CLI and managed read this same file; `meta.py reset` is the canonical API. Skip cleanly if `$AIR_PLUGIN_ROOT` couldn't be resolved (Step 0 prints a warning) — counter staying elevated just means the next review re-triggers learn, which is the safe failure mode.

```bash
WIKI_DIR="$AIR_TMP/review-wiki-learn"
if [ -n "$AIR_PLUGIN_ROOT" ] && [ -d "$WIKI_DIR/.git" ]; then
  python3 "$AIR_PLUGIN_ROOT/lib/meta.py" reset --wiki-dir "$WIKI_DIR" --pr-number 0
  # Use commit_meta from wiki_git (it implements pull --rebase retry on
  # non-fast-forward — between Step 6's push and this one a concurrent CI
  # review could land its own bump on .air-meta.json upstream). Pass paths
  # via env (not shell-interpolation into Python source) so quotes and
  # backslashes in paths can't corrupt the literal.
  AIR_PLUGIN_ROOT="$AIR_PLUGIN_ROOT" WIKI_DIR="$WIKI_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['AIR_PLUGIN_ROOT'] + '/lib')
import wiki_git
sys.exit(0 if wiki_git.commit_meta(os.environ['WIKI_DIR'], 'meta: reset counter after /air:learn') else 1)
" || echo "warning: meta reset push failed — counter stays elevated, will retrigger" >&2
else
  echo "Skipping counter reset (AIR_PLUGIN_ROOT unresolved or wiki dir missing)" >&2
fi
```

## Cleanup

```bash
[ -n "$AIR_TMP" ] && rm -rf "$AIR_TMP"
```
