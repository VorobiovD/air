# Review History — Auto-generated from PR Comments

Last generated: 2026-04-25
PRs analyzed: 30 (17 with review comments)

## Finding Frequency

| Finding pattern | Count | Last seen | PRs |
|---|---|---|---|
| Stale documentation / metadata after implementation changes | 47 | PR #39 | #39, #38, #36, #34, #33, #30, #28, #23, #22, #21, #20, #18, #14, #12, #11 |
| Async lifecycle gap (gather error semantics, missing `wait_for`, signal/atexit cleanup) | 35 | PR #38 | #38, #27, #23, #14 |
| Symmetric-branch / flow-routing gap (`review.md` ↔ `review-self.md` ↔ `review-respond.md`, create vs update path) | 30 | PR #39 | #39, #38, #33, #28, #23, #22, #21, #20, #18, #14, #12, #11 |
| Hardcoded counts / pinned strings duplicated across files (`claude-opus-4-7`, agent count, `## Code Review` marker) | 24 | PR #39 | #39, #38, #36, #28, #27, #23, #22, #21, #20, #14, #12 |
| Aspirational / inaccurate prose (cost claims, docstring promises, comments overstating guarantees) | 20 | PR #39 | #39, #38, #34, #30, #28, #22, #21, #20, #14 |
| API failure collapsed into "no data" with no operator signal (5xx / 404 / rate-limit) | 10 | PR #39 | #39, #38, #30, #28, #20, #14 |
| Untrusted input not escaped / not bounded / spoofable control-plane comment | 7 | PR #28 | #28, #23 |
| Pre-commit hook bypass coverage / cross-shell portability | 6 | PR #39 | #39, #22 |
| Pinning gap (floating `@v4` action tag, unpinned `npm install`, hardcoded private-key path) | 5 | PR #39 | #39, #38, #36, #14 |
| Missing HTTP status / response validation before parsing | 5 | PR #23 | #23, #14, #11 |
| Token-bearing wiki URL leaks through subprocess error paths | 4 | PR #39 | #39, #23 |
| Dead code / inert flag / redundant check | 3 | PR #39 | #39, #14 |
| Two-phase API loops with redundant fetches (pagination duplication) | 3 | PR #28 | #28, #23, #11 |
| Imports / module-level side effects (inline imports, atexit/signal at import time) | 2 | PR #39 | #39, #14 |
| Test coverage gap (race not covered, new helper without unit test) | 2 | PR #39 | #39 |
| Unguarded flag combination | 2 | PR #33 | #33, #30 |
| Bash precedence / quoting in shell chains | 2 | PR #30 | #30, #22 |
| Resource leak (orphan subprocess, unclosed client, in-progress sessions on shutdown) | 2 | PR #27 | #27, #23 |
| CI workflow permissions / `persist-credentials` / concurrency | 1 | PR #36 | #36 |
| Workflow input typing (`workflow_dispatch` bool vs string) | 1 | PR #30 | #30 |
| Tool/permission minimality violation | 1 | PR #14 | #14 |
| Self-review author identification mismatch | 1 | PR #11 | #11 |

## File Hot Spots

| File/directory | Findings | PRs touching |
|---|---|---|
| `managed/review.py` | 90 | #39, #38, #33, #30, #28, #27, #23, #20, #14 |
| `plugins/air/commands/review.md` | 31 | #39, #33, #30, #22, #21, #20, #18, #12, #11, #8 |
| `managed/setup.py` | 21 | #23, #20, #14 |
| `plugins/air/commands/learn.md` | 18 | #39, #22, #21, #11 |
| `plugins/air/commands/review-self.md` | 18 | #39, #21, #18 |
| `.github/workflows/managed-review.yml` | 15 | #38, #36, #33, #30, #23, #14 |
| `docs/architecture.md` | 15 | #38, #36, #34, #33, #30, #23, #22, #21, #20 |
| `CLAUDE.md` | 14 | #39, #36, #34, #22, #21, #20, #18, #14 |
| `README.md` | 12 | #39, #34, #33, #30, #23, #22, #21, #20, #12 |
| `plugins/air/README.md` | 10 | #39, #34, #33, #30, #23, #21, #20, #12 |
| `managed/README.md` | 9 | #38, #30, #28, #27, #21, #20, #14 |
| `.air-meta.json` | 8 | #39 |
| `managed/api.py` | 7 | #28, #23, #20, #14 |
| `plugins/air/commands/review-respond.md` | 7 | #33, #21, #18 |
| `plugins/air/.claude-plugin/plugin.json` | 7 | #36, #23, #22, #21, #20 |
| `plugins/air/lib/wiki_git.py` | 7 | #39 |
| `managed/config.json` | 6 | #14 |
| `.air-checks.sh` | 6 | #39, #22 |
| `plugins/air/hooks/pre-commit-drift.py` | 6 | #39, #22 |
| `plugins/air/hooks/builtin-checks.sh` | 6 | #36, #34, #23, #22 |

## Author Trends

| Author | Total findings | Blockers | Clean PRs | PRs reviewed | Most common pattern |
|---|---|---|---|---|---|
| VorobiovD | 234 | 11 | 0 | 17 | Stale documentation / metadata sync |

## Timeline

| PR | Date | Title | Author | Findings | Blockers | Iterations |
|---|---|---|---|---|---|---|
| #39 | 2026-04-24 | feat: wiki-backed shared /air:learn counter | VorobiovD | 30 | 3 | 4 |
| #38 | 2026-04-24 | feat: add Codex as a 5th managed-review specialist (opt-in) | VorobiovD | 11 | 0 | 1 |
| #36 | 2026-04-24 | feat: automate releases with release-please | VorobiovD | 10 | 0 | 2 |
| #34 | 2026-04-24 | chore: bump to v1.8.0 | VorobiovD | 4 | 0 | 1 |
| #33 | 2026-04-24 | fix: address 9 findings from PR #30's post-merge review | VorobiovD | 4 | 0 | 2 |
| #30 | 2026-04-24 | feat: allow opt-in review of closed/merged PRs | VorobiovD | 9 | 0 | 1 |
| #28 | 2026-04-24 | feat: auto-detect re-review mode + skip-if-unchanged gating | VorobiovD | 20 | 3 | 2 |
| #27 | 2026-04-23 | fix: interrupt orphan sessions on driver shutdown | VorobiovD | 31 | 0 | 4 |
| #23 | 2026-04-23 | refactor: client-side orchestrator for managed reviews (v1.7.0) | VorobiovD | 20 | 3 | 1 |
| #22 | 2026-04-23 | feat: pre-commit drift-check hook (v1.6.0) | VorobiovD | 14 | 0 | 1 |
| #21 | 2026-04-23 | fix: session-scoped /tmp to prevent parallel-run collisions (v1.5.1) | VorobiovD | 10 | 2 | 1 |
| #20 | 2026-04-23 | perf: model tiering + prompt-cache discipline (v1.5.0) | VorobiovD | 14 | 0 | 1 |
| #18 | 2026-04-23 | feat: 10 improvements from real-world usage feedback | VorobiovD | 6 | 0 | 1 |
| #14 | 2026-04-11 | feat: Managed Agent for automated PR reviews | VorobiovD | 35 | 0 | 5 |
| #12 | 2026-04-09 | feat: expand agent checklists — reuse, quality, efficiency | VorobiovD | 7 | 0 | 1 |
| #11 | 2026-04-09 | feat: author pattern lifecycle + review pipeline fixes | VorobiovD | 7 | 0 | 1 |
| #8 | 2026-04-09 | feat: add GitLab support (dual-platform) | VorobiovD | 2 | 0 | 2 |

PRs in the analysis window with **no review comments** (chore / version-bump / fix passes that bypassed review): #35, #32, #31, #29, #26, #24, #19, #17, #16, #15, #13, #10, #9. Excluded from author totals.
