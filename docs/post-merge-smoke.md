# Post-merge smoke runbook

Every merge to `main` deploys fleet-wide immediately (callers pin
`managed-review.yml@main`, no `agent_versions`). There is no staging tier, so
this runbook is the deploy verification. Run it after any merge touching
`managed/`, `plugins/air/lib/`, or the workflows; skip for docs-only merges.
Total cost: **$0 for steps 1–3**; step 4 is one review (~$1–3).

## 1. Main push-CI green (~2 min, $0)

```bash
gh run list --repo VorobiovD/air --branch main --limit 4 \
  --json workflowName,status,conclusion \
  --jq '.[] | "\(.workflowName): \(.status)/\(.conclusion // \"-\")"'
```

Expect `managed tests`, `air-lib tests`, and `drift checks` all
`completed/success` at the merge SHA. `managed tests` auto-collects every
`test-*.py` (pyproject `python_files`), so a hand-list can't silently drop a
suite.

## 2. Learn-cron list smoke ($0, no model calls)

Exercises `learn_cron.py` + `meta.py` (store registry, due-detection, locks) +
`env.report_env` end-to-end without curating anything:

```bash
gh workflow run "air learn (reusable)" --repo VorobiovD/air -f mode=list
# then, once completed:
gh run view <run-id> --repo VorobiovD/air --log | grep -E '\[cron\]|\[env\]|Traceback'
```

Expect a `[cron] N repo(s) due (list-only, no learns)` line, **no** `[env]
warning` lines (a warning names a typo'd `AIR_*` variable), no tracebacks.

## 3. Telemetry greps on the next live review ($0)

Whatever review runs next (or the step-4 smoke), pull its log and check:

```bash
gh run view <run-id> --repo VorobiovD/air --log | \
  grep -E '\[env\]|\[cost\] TOTAL|\[gate\]|\[pin\]|verifier_extracted|Traceback'
```

Healthy run: `verifier_extracted=True`, `[cost] TOTAL … cache-read ≥80%`,
no `[env]` warnings, no `Traceback`, `[pin]`/`[ledger]` lines only on
re-reviews. A `[gate] blocker-class lens did not complete — failing closed`
line means a specialist died (usually an upstream 529) — the fail-close is
correct; re-dispatch after ~20 min rather than treating the verdict as real.

## 4. Fresh-review smoke on a real PR (~$1–3)

Dispatch a fresh review of a PR with **no prior air review** (a review of an
already-reviewed PR at head skips with a backfill — useless as a smoke). The
open release-please PR is usually ideal:

```bash
gh workflow run "air review" --repo VorobiovD/air -f pr_number=<N> -f closed=false
```

Expect: run success, sane verdict, step-3 telemetry clean.

## 5. Before/after metrics (when the change could affect cost/behavior)

Compare the `[cost] TOTAL` lines and wall times of post-merge review runs
against the days before (step-3 greps over recent run logs): avg cost, wall,
cache-read ratio, and the verdict mix should hold.

Two deeper regression checks when a change touches gating or review behavior:

- **Gate replay ($0, deterministic):** extract the pre-change gate via
  `git show <old-sha>:plugins/air/lib/verdict.py`, load old + new as modules,
  and run `should_request_changes` / `count_blockers` /
  `count_category_floored` / `extract_prior_statuses` over the recently
  POSTED `## Code Review` bodies (fetch via
  `gh api repos/<repo>/issues/<pr>/comments`). Require **zero divergences** —
  the gate contract is frozen; any flip is a finding.
- **Same-PR rerun (~$2/PR):** pick a PR reviewed before the change, create a
  worktree at its original head, and re-run with the new code:
  `AIR_TARGET_REPO=<worktree> python3 managed/review.py <repo> <pr>
  --mode messages-api --dry-run --fresh --closed --no-codex`. Compare verdict
  + blocker set against the posted review (findings vary run-to-run; the
  verdict and blocker count must not).

## Rollback levers

| Change type | Rollback |
|---|---|
| Behavior behind a kill switch | set the caller/org variable (`AIR_LEDGER_PIN=0`, `AIR_CATEGORY_FLOOR=0`, `AIR_REVIEW_FORMAT=legacy`, `AIR_WIKI_CAP=0`, `AIR_RELATED_PRS=0`, `AIR_POST_VERIFIER_BODY=0`, `AIR_HEADLESS_PATTERNS=0`, …) — instant, no deploy |
| Mode/architecture | `gh variable delete AIR_REVIEW_MODE --repo <caller>` (falls back to `full`) |
| Anything else | `git revert` the merge commit on main — the next fleet run picks it up |

A billing/API failure leaves CI green by design on some paths — check the PR
comment/verdict, not just the workflow badge.
