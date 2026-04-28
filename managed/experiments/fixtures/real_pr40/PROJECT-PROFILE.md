# Project Profile — air

Last updated: 2026-04-25

## Overview

**air** is a Claude Code plugin for automated PR code review with two distribution paths:

1. **CLI Plugin** (`plugins/air/`) — runs locally in Claude Code, triggered manually with `/air:review` and `/air:learn`. Markdown agent prompts + JSON metadata + thin Python helpers in `lib/`.
2. **Managed Agent** (`managed/`) — Python orchestrator that runs in Anthropic's cloud, fans out 4 specialist agents via `asyncio.gather`, runs the verifier, and posts the review comment. Triggered by GitHub Actions on every PR.

Both paths use the same 5 agent prompts, push to the same wiki, and read from the same `.air-meta.json` counter. Distributed via the Claude Code marketplace (`/plugin marketplace add VorobiovD/air` then `/plugin install air@air`); CI consumers reference the reusable workflow `VorobiovD/air/.github/workflows/managed-review.yml@main`.

- **Version:** 1.8.0
- **Author:** Dmytro Vorobiov
- **Commands:** `/air:review` (PR review, self-review, re-review, respond), `/air:learn` (wiki cleanup + KAIROS history regeneration)

## Languages

| Language | Usage | Files |
|---|---|---|
| Markdown | Agent prompts, command orchestration, documentation | `plugins/air/agents/*.md`, `plugins/air/commands/*.md`, `managed/prompts/*.md`, `*.md` at root, `docs/*.md` |
| Python | Managed orchestrator, wiki/meta helpers, drift hook, tests | `managed/*.py`, `plugins/air/lib/*.py`, `plugins/air/lib/tests/*.py`, `plugins/air/hooks/pre-commit-drift.py` |
| JSON | Plugin/marketplace metadata, hook registration, release-please config | `plugin.json`, `marketplace.json`, `hooks.json`, `.release-please-config.json`, `.release-please-manifest.json` |
| YAML | GitHub Actions workflows | `.github/workflows/*.yml` |
| Bash (embedded) | Shell commands embedded in markdown prompts; pre-commit hook drift checks | `commands/*.md`, `hooks/builtin-checks.sh`, `.air-checks.sh` |

No Go, TypeScript, Terraform, SAM, Docker, or compiled code. The Python in `plugins/air/lib/` is **stdlib-only** (no `requirements.txt`); only `managed/` has third-party deps (`anthropic>=0.93.0`, `requests>=2.28.0`).

## Frameworks / Runtime

- **Claude Code plugin system** — markdown-based agent definitions with YAML frontmatter; subagents launched via `Task` tool; hooks registered through `hooks.json` (PreToolUse on Bash for `pre-commit-drift.py`)
- **Anthropic Managed Agents API** (`anthropic` Python SDK) — `managed/review.py` creates/lists/calls agents and streams sessions for the cloud reviewer
- **GitHub CLI (`gh`)** and **GitLab CLI (`glab`)** — all VCS API interactions go through the platform CLIs; `commands/platform-gitlab.md` documents field-name translations
- **GitHub Wiki** — pattern storage (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md, plus `.air-meta.json` for the shared trigger counter)
- **GitHub Actions** — `managed-review.yml` (reusable, consumed by other repos), `air-review.yml` (dogfood), `air-lib-tests.yml` (pytest CI for `plugins/air/lib/`), `release-please.yml` (automated tag + release on version bumps)
- **release-please** — `googleapis/release-please-action` for automated version bumps and changelog generation
- **Codex plugin** (optional, opt-in `--no-codex` to disable) — GPT-5.4 second-opinion reviewer; called via `codex-companion.mjs` for CLI and via `npm install -g @openai/codex` + `codex review` for managed; gated on `OPENAI_API_KEY`

## Project Structure

```
air/
├── CLAUDE.md                              # Project conventions and architecture
├── README.md                              # User docs (CLI + CI setup guides)
├── CHANGELOG.md                           # Auto-maintained by release-please
├── CONTRIBUTING.md
├── SECURITY.md
├── LICENSE
├── .air-checks.sh                         # Opt-in per-repo drift checks (consumed by hook)
├── .release-please-config.json            # release-please config (extra-files list)
├── .release-please-manifest.json
├── .github/
│   ├── PULL_REQUEST_TEMPLATE.md
│   ├── ISSUE_TEMPLATE/{bug_report,feature_request}.md
│   └── workflows/
│       ├── managed-review.yml             # Reusable Action consumed by team repos
│       ├── air-review.yml                 # Dogfood caller for this repo
│       ├── air-lib-tests.yml              # pytest CI for plugins/air/lib/
│       └── release-please.yml             # Automated version bump + tag + release
├── .claude-plugin/
│   └── marketplace.json                   # Marketplace distribution
├── docs/
│   └── architecture.md                    # Architecture, decisions, roadmap
├── managed/                               # MANAGED AGENT (Anthropic cloud)
│   ├── api.py                             # Shared helpers: get_headers, list_agents, find_environment, _paginate
│   ├── setup.py                           # Creates/updates 5 specialist agents (no orchestrator agent — client-side now)
│   ├── review.py                          # Client-side orchestrator: fans out 4 specialists via asyncio.gather, verifier, post comment, signal-handled cleanup
│   ├── learn.py                           # Triggers wiki maintenance sessions; calls meta.py reset
│   ├── test-session.py                    # 9-test verification (repo, auth, blame, comment, wiki)
│   ├── test-learn.py                      # Wiki clone/push verification
│   ├── test-parallel.py                   # Smoke test for parallel sub-agent execution
│   ├── prompts/
│   │   └── learn-orchestrator.md          # Learn pipeline for cloud (single-agent flow)
│   ├── README.md                          # Per-org setup, secrets, optional Codex
│   └── requirements.txt                   # anthropic>=0.93.0, requests>=2.28.0
└── plugins/air/                           # CLI PLUGIN (Claude Code marketplace)
    ├── .claude-plugin/
    │   └── plugin.json                    # Manifest (version source of truth)
    ├── README.md                          # Mirrors root README — drift-checked
    ├── agents/                            # 5 specialist prompts (single source for both paths)
    │   ├── code-reviewer.md               # Bugs, logic, design, comment compliance (model: opus)
    │   ├── security-auditor.md            # 31-item checklist + resource exhaustion (model: opus)
    │   ├── simplify.md                    # Code Reuse, Code Quality, Efficiency — read-only (model: sonnet)
    │   ├── git-history-reviewer.md        # Blame, churn, previous PR feedback (model: sonnet)
    │   └── review-verifier.md             # False positive filter, confidence scoring, 6 verdicts (model: opus)
    ├── commands/                          # CLI orchestration
    │   ├── review.md                      # Main 13-step pipeline (~947 lines)
    │   ├── review-self.md                 # Self-review flow (--self / --full / --fix), extracted in v1.4
    │   ├── review-respond.md              # Respond flow (--respond), extracted in v1.4
    │   ├── learn.md                       # Wiki maintenance + KAIROS history (~449 lines)
    │   └── platform-gitlab.md             # GitLab CLI/API field mappings (reference)
    ├── hooks/                             # Pre-commit drift-check hook (v1.6.0+)
    │   ├── hooks.json                     # PreToolUse registration on Bash
    │   ├── pre-commit-drift.py            # Narrows to `git commit`, routes custom/built-in
    │   └── builtin-checks.sh              # Zero-config: manifest version vs README badge, doc-mirror grep
    └── lib/                               # Shared Python helpers (stdlib-only, called by CLI + managed)
        ├── meta.py                        # `.air-meta.json` read/write + /air:learn trigger threshold (5 reviews / 2 days)
        ├── wiki_git.py                    # Clone + commit-meta-with-retry; _redact() for token URLs in error logs
        └── tests/
            ├── test_meta.py               # 25 pytest cases: trigger branches, JSON evolution, boundary values
            └── test_wiki_git.py           # Pytest: clone, commit_meta retry, configure_identity
```

## Services / Components

| Component | File | Role |
|---|---|---|
| CLI Orchestrator | `plugins/air/commands/review.md` | 13-step pipeline: parse, fetch, review, verify, post, learn |
| CLI Self-Review | `plugins/air/commands/review-self.md` | `--self` / `--full` / `--fix` modes — local diff or full-codebase audit, output to console |
| CLI Respond | `plugins/air/commands/review-respond.md` | `--respond` mode — auto-classify findings as fixed/disputed/acknowledged from local commits |
| CLI Learn | `plugins/air/commands/learn.md` | Wiki cleanup + KAIROS history + counter reset |
| Managed Orchestrator | `managed/review.py` | Cloud-side: 4 parallel specialists + verifier + post comment, asyncio.gather + signal/atexit cleanup |
| Managed Learn | `managed/learn.py` | Cloud-side wiki maintenance trigger; resets `.air-meta.json` post-run |
| Managed Setup | `managed/setup.py` | Creates/updates 5 Anthropic API agents from the same prompts |
| Code Reviewer | `plugins/air/agents/code-reviewer.md` | Bugs, error handling, design, comment rot (Opus) |
| Security Auditor | `plugins/air/agents/security-auditor.md` | 31-item security checklist + resource exhaustion (Opus) |
| Simplify | `plugins/air/agents/simplify.md` | Code Reuse, Code Quality, Efficiency — 3 dimensions, read-only (Sonnet) |
| Git History Reviewer | `plugins/air/agents/git-history-reviewer.md` | Blame, churn, recurring issues (Sonnet) |
| Review Verifier | `plugins/air/agents/review-verifier.md` | False-positive filter, confidence score, 6-verdict classification (Opus) |
| Codex (optional) | external (`codex-companion.mjs` / `npm @openai/codex`) | GPT-5.4 second-opinion specialist; gated on `OPENAI_API_KEY` |
| Drift Hook | `plugins/air/hooks/pre-commit-drift.py` + `builtin-checks.sh` | Pre-commit guard against doc/version drift; runs `.air-checks.sh` if present, plus built-in version-mirror checks |
| Wiki/Meta Helpers | `plugins/air/lib/meta.py`, `plugins/air/lib/wiki_git.py` | Counter logic, wiki clone/push with `pull --rebase` retry, token redaction in error logs |

## Test Locations

- **Pytest (CI-run)**: `plugins/air/lib/tests/test_meta.py` (25 cases for `should_trigger_learn`, `cmd_check`, JSON evolution), `plugins/air/lib/tests/test_wiki_git.py` (clone, `commit_meta` retry, identity config). Wired through `.github/workflows/air-lib-tests.yml`.
- **Manual smoke tests** (`managed/`): `test-session.py` (9-test API verification), `test-learn.py` (wiki round-trip), `test-parallel.py` (parallel sub-agent execution).
- **Dogfooding**: `air-review.yml` runs the managed reviewer on this repo's own PRs.

No live `/air:review` integration tests — testing is dogfood-driven (run on PRs, observe).

## CI/CD Setup

| Workflow | Purpose |
|---|---|
| `managed-review.yml` | Reusable workflow consumed by other repos via `uses:` — runs `managed/review.py` against an open PR |
| `air-review.yml` | Dogfood caller (PR + workflow_dispatch with `pr_number` / `closed` string inputs) |
| `air-lib-tests.yml` | Pytest CI for `plugins/air/lib/` on PRs touching `lib/**` |
| `release-please.yml` | release-please-action: opens release PR on every push to main; merging that PR tags + creates a GitHub Release |

Plugin distribution: push to `main`. Marketplace consumers receive updates automatically. Managed-agent consumers reference `@main` (or pin to a release SHA) in their workflow `uses:`.

## Deploy Mechanism

- **CLI Plugin**: `git push origin main` → users on `/plugin marketplace add VorobiovD/air` + `/plugin install air@air` get updates automatically through Claude Code's plugin system.
- **Managed Agent**: consumers add `.github/workflows/air-review.yml` referencing `VorobiovD/air/.github/workflows/managed-review.yml@main` plus `ANTHROPIC_API_KEY` and `AIR_BOT_TOKEN` org secrets. First PR auto-bootstraps the 5 specialist agents via `setup.py`; subsequent PRs reuse them.
- **Releases**: release-please opens a release PR with version bump + CHANGELOG; merging tags `v1.x.y` and creates a GitHub Release. `extra-files` in `.release-please-config.json` lists the files that carry version markers.

## Manifest Files

Present:
- `plugins/air/.claude-plugin/plugin.json` — version source of truth
- `.claude-plugin/marketplace.json` — marketplace listing
- `plugins/air/hooks/hooks.json` — Claude Code hook registration
- `.release-please-config.json`, `.release-please-manifest.json` — release-please state
- `managed/requirements.txt` — managed-only Python deps (anthropic, requests)
- `.github/PULL_REQUEST_TEMPLATE.md`, `.github/ISSUE_TEMPLATE/*.md`

Absent: `go.mod`, `package.json`, `Cargo.toml`, `composer.json`, `Makefile`, `Dockerfile`, `*.tf`, `template.yaml`, `samconfig.toml`, `buildspec.yml`. Top-level `requirements.txt` deliberately absent — `plugins/air/lib/` is stdlib-only by design so it can be invoked from CLI markdown without an install step.

---

## Review Focus Rules

File-pattern-specific checks for review agents:

### `plugins/air/agents/*.md` (Agent Definitions)

- **Prompt injection resistance:** All user-controlled inputs (PR title, body, commit messages, developer comments, blame output, **prior review bodies in re-review mode**) must be wrapped in XML tags with untrusted-input warnings AND `html.escape`-d before interpolation. Raw user content flowing into agent instructions is a finding. The re-review prior-review-body feed is a fresh injection vector — see PR #28.
- **Untrusted control plane:** When agent behavior is gated on PR comments (e.g., re-review mode looking for prior `## Code Review` comments), filter by bot identity (`user.login`, `performed_via_github_app`) before treating a comment as authoritative. Any-author selection is spoofable.
- **Instruction clarity:** Each agent should have clear, unambiguous instructions. Vague directives like "check for issues" without specifics are a finding.
- **Severity definitions:** Confirm agents use the standard 4-level severity (blocker/medium/low/nit) consistently. No ad-hoc severity scales.
- **Tool scope:** Verify the `tools:` frontmatter only grants what the agent actually needs. `Bash` should be permitted only for `git blame` and `git log` (documented in a frontmatter comment). Read-only agents (simplify) must not have `Bash`.
- **Wiki file reads:** Each agent should read PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, and SEVERITY-CALIBRATION.md from the orchestrator's session temp directory (`Wiki files directory:` field in PR Context) when available. Missing reads mean the agent ignores learned patterns.
- **Confidence thresholds:** The verifier must use 60 as the default cutoff. Check that SEVERITY-CALIBRATION.md per-agent overrides are respected when present.
- **Model tiering (v1.5.0+):** Agents are no longer all-Opus. `code-reviewer`, `security-auditor`, `review-verifier` run on Opus; `simplify`, `git-history-reviewer` run on Sonnet. Cost-claim documentation must reflect the per-model boundary; prompt-cache claims must say "within each model family", not aggregate across the fan-out.

### `plugins/air/commands/*.md` (CLI Orchestration)

- **Shell command correctness:** All embedded bash must handle failures gracefully (`2>/dev/null`, `|| true`, explicit error checks). `&&`/`||` are equal-precedence and left-associative — guard `cd` separately rather than chaining `cd && a && b || true` (PR #39 blocker). Commands must not silently corrupt state on failure.
- **Symmetric branches:** When `review.md` and `review-self.md` (and `review-respond.md`) share a flow shape, gate logic must be mirrored. New flags / new sub-steps added to one must propagate to the others. Bash-comment-only directives in one branch where the sibling has an explicit `>>> AUTO-TRIGGER DECISION <<<` block are a flow-routing gap.
- **Flag combination guards:** New flags that route through existing flows must reject incompatible combos (e.g., `--closed` + `--self` / `--full` / `--respond`, `--respond` + `--fresh` / `--rewrite` / `--re-review`). The guard must run BEFORE the flow-diverters that would short-circuit past it.
- **API call efficiency:** Step 4 batches `gh pr view` into a single multi-field call. Re-review mode (Step 6) deduplicates `find_prior_review` and `fetch_comments_since` against the same endpoint. New steps that re-fetch what an earlier step already got are a finding.
- **Temp file cleanup:** All `/tmp/` writes use the session-scoped `$AIR_TMP=$(mktemp -d "/tmp/air-XXXXXX")` so parallel runs don't collide. Hard-coded `/tmp/air-…` paths bypass the GC; new bare-`/tmp/` writes are findings (PR #21 found 5 such regressions).
- **Race conditions in parallel agents:** All 5 reviewers (4 agents + Codex) must launch in a single parallel batch. Sequential launches are a performance bug.
- **Wiki push safety:** `git diff --quiet --cached || git commit && git push` to avoid empty commits. Wiki push for `.air-meta.json` should go through `wiki_git.commit_meta` so it inherits the `pull --rebase` retry; hand-rolled push chains in new code race with concurrent CI.
- **Counter/meta file handling:** Reads/writes go through `lib/meta.py` (`bump`, `check`, `reset`). New auto-trigger sites must use `meta.py`, not write `~/.claude/review-learn-meta.json` directly (legacy file, removed in v1.8.0). The wiki-backed `.air-meta.json` is the single source of truth.
- **Cross-repo guards:** When `CROSS_REPO=true`, verify wiki, blame, churn, and learn operations are all skipped.
- **`AIR_PLUGIN_ROOT` derivation:** Step 0 of every command that calls `python3 "$AIR_PLUGIN_ROOT/lib/…"` must include the canonical glob-and-fallback derivation block, plus a guard that empties the var when unresolvable (so downstream invocations skip cleanly instead of expanding to `/lib/…`). New commands missing this block are a finding.

### `plugins/air/lib/*.py` (Python Helpers)

- **Stdlib-only:** No third-party imports. Anything that needs `requests` belongs in `managed/`.
- **Token redaction:** Any subprocess error path that may surface the wiki URL (which embeds `x-access-token:<token>@github.com`) must route through `_redact()` from `wiki_git.py`. The full `CalledProcessError.__str__` echoes the `cmd` list; redact the entire message, not just `e.stderr`.
- **Pytest coverage:** Each new public function in `meta.py` / `wiki_git.py` should ship with at least one happy-path test and one boundary/error test. Tests in `lib/tests/` are auto-run by `air-lib-tests.yml`.
- **Constants:** Filenames like `.air-meta.json` should have one canonical definition. Duplicating the literal across `meta.py` and `wiki_git.py` is drift waiting to happen.

### `managed/*.py` (Managed Orchestrator)

- **Sub-agent fan-out:** `asyncio.gather(*tasks)` — all 4 specialists must launch in one batch. `return_exceptions=True` is required to keep one specialist's failure from cancelling siblings; without it, a crash orphans billing on the still-running sessions.
- **Per-session timeout:** Every `run_session` call must have an outer `asyncio.wait_for`. Hung streams without a timeout stall the entire review.
- **Signal/atexit cleanup:** Live sessions must be tracked in `LIVE_SESSIONS` and interrupted on SIGINT/SIGTERM/SIGHUP. Module-import-time `signal.signal(...)` registration is fine; module-import-time `atexit.register(...)` is also fine — but the cleanup function itself must not call blocking HTTP on the main thread while the event loop is alive (PR #27).
- **Untrusted input escaping:** `build_pr_context` must `html.escape` every user-influenced string interpolated into XML-tagged blocks (PR title, body, comments, **prior review body**). Truncation must happen BEFORE escape, otherwise the escape can push the string back over the budget.
- **API failure surfacing:** GitHub helpers (`find_prior_review`, `fetch_comments_since`, `fetch_inter_diff`) must distinguish "fetch failed" from "no data" — a 502 / 5xx / rate-limit must not collapse into the same return as "nothing found", or the orchestrator silently runs in the wrong mode.
- **Pagination:** Use a single `_github_paginate` helper rather than duplicating `Link: rel="next"` extraction across helpers.
- **Subprocess env:** Don't pass full `os.environ` to subprocesses (`setup.py`, `learn.py`); pass only the variables the child needs. Don't run `learn.py` as a detached `Popen` from a CI job — the runner tears down before it finishes; either run synchronously (`subprocess.run` with `--poll`) or dispatch a separate workflow.
- **Marker constants:** `## Code Review` is referenced in 3+ sites (writer in verifier prompt, reader in `find_prior_review`, reader in `partition` post-process). One module-level constant.

### `plugins/air/hooks/*.{py,sh}` (Pre-commit Drift Hook)

- **Subprocess safety:** The hook is registered on PreToolUse for Bash, so it runs in the developer's shell. Any auto-execution of repo-checked-in scripts (`.air-checks.sh`) is a supply-chain escalation; user must opt in (executable bit, repo trust prompt, or both).
- **Bypass coverage:** Drift detection must catch `git commit -m`, `git -C <path> commit`, `git --git-dir=X commit`, and reject `--no-verify` only when present as a flag (not when the commit message contains the substring).
- **Cross-shell portability:** Glob patterns must work in macOS bash 3.2 (no `globstar`); use `find` over `**` when recursion is required.

### `.github/workflows/*.yml` (CI Workflows)

- **Pinning:** Pin third-party actions to a SHA (or at minimum a major); floating `@v4` tags can be retagged. `googleapis/release-please-action@v4` should be pinned (PR #36).
- **Permissions:** Every workflow needs an explicit top-level `permissions:` block with the minimum needed (`contents: read` for test-only jobs, `contents: write` + `pull-requests: write` for release-please).
- **Checkout credential persistence:** `actions/checkout@v4` defaults to `persist-credentials: true`, leaving `GITHUB_TOKEN` in `.git/config`. Set `with: persist-credentials: false` for jobs that don't push.
- **Concurrency:** Release / version-bump workflows need a `concurrency:` group to prevent parallel runs from racing.
- **Workflow input types:** `workflow_dispatch` boolean inputs come through as the literal strings `"true"` / `"false"`. Reusable callers must pass strings (not bools) to typed `workflow_call` inputs (PR #31).

### `*.json` (Metadata)

- **Schema validity:** `plugin.json` must have `name`, `description`, `version`, `author` fields. `marketplace.json` must conform to the marketplace schema. Trailing newline is conventional.
- **Version consistency:** `plugin.json` is the single source of truth. release-please's `extra-files` list must enumerate every file that carries the version (root README badge, plugin README badge, `docs/architecture.md`, `CLAUDE.md`). Files added since `extra-files` was last edited are silent drift.

### `README.md` / `CLAUDE.md` / `docs/architecture.md` (Documentation)

- **Mirror parity:** Root README and `plugins/air/README.md` are mirrors. Major sections (badges, "What's New", architecture diagram) added to one must be added to the other in the same PR.
- **Accuracy vs implementation:** Documented behavior must match command files. Stale step counts, outdated flag descriptions, agent-count mismatches (architecture says "4 specialists" while code wires 5) are findings.
- **Fixed-cost figures drift:** Inline cost figures (`~$2.30`, `~$1.69`) calcify on the next pricing/tiering bump. Either compute from a constants table or note them as illustrative.
- **No secrets or internal URLs:** No API keys, internal endpoints, or infrastructure identifiers.

---

## Applicable Security Checks

From the 31-item security checklist in `plugins/air/agents/security-auditor.md`:

### Applicable Checks

**Checks: 8, 9, 13, 17, 20, 21, 22, 23, 29**

| # | Check | Why it applies |
|---|---|---|
| 8 | Command injection | Embedded bash in markdown prompts and `subprocess.run`/`subprocess.Popen` calls in `managed/*.py` and `lib/*.py` build shell commands from PR data (numbers, SHAs, repo names, paths). Malformed input could inject. The `python3 -c "$INTERPOLATED"` heredoc pattern in `learn.md` Step 7 is a fresh interpolation vector — use env vars + `os.environ['…']` for paths. |
| 9 | Template / prompt injection | PR title, body, commit messages, developer comments, **prior review body**, **inter-diff** are interpolated into agent prompts. Without `html.escape` + bounded length + tag-anchored sanitization, adversarial content alters agent behavior. The re-review prior-body channel and the comment-based "prior SHA" lookup form a control plane that must be filtered by bot identity. |
| 13 | Secrets management | Plugin handles `GH_TOKEN` / `AIR_BOT_TOKEN` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. Wiki clone URLs embed the token. `_redact()` in `wiki_git.py` strips token-bearing URLs from error logs; new error paths must route through it. CI logs are masked by the secret redactor; local terminal output is **not** — bare `e.stderr` prints leak tokens to developers. |
| 17 | No infrastructure secrets in code | Markdown files and Python source must not contain real endpoints, ARNs, account IDs, or API keys. Hardcoded private-key paths (PR #14) are a finding. |
| 20 | Temp file hygiene | All `/tmp/` writes go through `$AIR_TMP=$(mktemp -d ...)`; `find /tmp -maxdepth 1 -name 'air-*' -mtime +1 -exec rm -rf {} + 2>/dev/null` sweeps stale dirs at the start of each run. New bare `/tmp/air-…` paths bypass GC. KAIROS cache lives at `$HOME/.cache/air/kairos/` with TTL bounded by Step 0's `find -mtime +30 -delete`. |
| 21 | Tool/permission minimality | Agent frontmatter grants `Read`, `Grep`, `Glob`, `Bash`. Each agent should only have what it uses. `Bash` is restricted to `git blame` and `git log`. Read-only agents (`simplify`) must not have `Bash`. CI workflows need explicit `permissions:` blocks (least-privilege). |
| 22 | External API exposure | Calls go to GitHub/GitLab via the platform CLIs and to Anthropic via the SDK. Codex (OpenAI) is **opt-in** and gated on `OPENAI_API_KEY`. `npm install -g @openai/codex` is unpinned (PR #38) — a fresh supply-chain surface; pin to a known version when feasible. |
| 23 | Hardcoded paths/versions | Codex integration uses `find` + `sort -V | tail -1` to locate the plugin and pick the latest version. The `~/.claude/plugins/cache/air/air/*/` glob silently no-ops on alternative install layouts (PR #39). Floating `@v4` tags on third-party Actions are pinning drift. |
| 29 | Resource exhaustion | `managed/review.py` runs long-lived async sessions; SIGTERM cleanup must interrupt them or billing leaks. Codex `npx` subprocess can hang; `asyncio.wait_for` is required. Atexit/signal handlers must not deadlock or block. |

### Skipped Checks

| # | Check | Reason |
|---|---|---|
| 1-6 | Sensitive data / compliance (all 6) | No regulated or personal data handling. air reviews other people's code; it does not process PII/PHI in its own pipeline. |
| 7 | SQL injection | No database. |
| 10 | Path traversal | User-controlled paths are bounded to `/tmp/air-XXXXXX` (mktemp) and `~/.cache/air/kairos/<repo-hash>`; PR data flows through `gh` CLI, not raw filesystem. |
| 11 | API key validation | No HTTP server. |
| 12 | IAM scope | No AWS resources. |
| 14 | Handler boundaries | No HTTP handlers. |
| 15 | Pattern validation | Inputs are PR numbers / URLs parsed by `gh`/`glab`. Re-review SHA regex (`{7,40}` hex prefix) is intentional but must be anchored against full-SHA equality at compare time. |
| 16 | YAML loading | No YAML parsing in plugin code. Frontmatter is handled by Claude Code runtime; workflow YAML is parsed by GitHub Actions. |
| 18 | Error detail leakage | GitHub error bodies are bounded (`:200` / `:500` slices in `managed/review.py`). Local-terminal stderr was a token-leak vector; covered by check 13. |
| 19 | CORS | No web server. |
| 24-28 | Silent failures | Silent failures ARE in scope where they affect orchestrator semantics (e.g., `fetch_inter_diff` collapsing 5xx into "no diff"), but covered by check 9 (control-plane integrity) rather than item-by-item under 24-28. |
| 30-31 | Resource exhaustion (event listeners, connection pools) | Subset of 29; coverage above. |
