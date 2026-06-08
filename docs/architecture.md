# air — Architecture, Decisions, and Roadmap

**Version:** 1.27.0 <!-- x-release-please-version -->

---

## What It Is

**air** is an automated code review system with two distribution paths:

1. **CLI Plugin** — runs locally in Claude Code, triggered manually with `/air:review`
2. **Managed Agent** — runs in Anthropic's cloud, triggered by GitHub Actions: request-driven (review on bot reviewer-request; recommended) or push-driven with a cooldown debounce — see `managed/README.md` "Enable on a repo"

Both paths use the same 6 agent prompts, same pattern source, same review format, and learn from each other. Pattern source is per-repo: a memory store (migrated repos — review sessions mount read-only, writes via `managed/pattern_writer.py`, git wiki kept as an exported mirror rendered deterministically by `managed/render_store_to_wiki.py` — throttled per-review + authoritative on learn, see "Store→wiki mirror render" below) or the legacy git wiki.

---

## Repository Structure

```
VorobiovD/air/
│
├── plugins/air/                    ← CLI PLUGIN (Claude Code marketplace)
│   ├── agents/                     ← SHARED agent prompts (single source of truth)
│   │   ├── code-reviewer.md           Bugs, logic, design, test coverage, author patterns
│   │   ├── simplify.md                3 sections: Code Reuse, Quality, Efficiency (16 items)
│   │   ├── security-auditor.md        31-item checklist + resource exhaustion
│   │   ├── git-history-reviewer.md    Blame, churn, previous PR comments, author patterns
│   │   └── review-verifier.md         False positive filter, confidence scoring, 6 verdicts
│   ├── commands/                   ← CLI-only orchestration
│   │   ├── review.md                 13-step pipeline (~879 lines, core orchestration)
│   │   ├── review-self.md            Self-review flow (--self mode, extracted)
│   │   ├── review-respond.md         Respond flow (--respond mode, extracted)
│   │   ├── learn.md                  Wiki maintenance + KAIROS history
│   │   └── platform-gitlab.md       GitLab CLI/API mappings
│   ├── hooks/                      ← CLI-only pre-commit drift check (v1.6.0+)
│   │   ├── hooks.json                PreToolUse registration on Bash
│   │   ├── pre-commit-drift.py       Narrows to `git commit`, routes custom/built-in
│   │   └── builtin-checks.sh         Zero-config manifest-version vs doc-mirror greps
│   ├── lib/                        ← Shared Python helpers (stdlib-only)
│   │   ├── meta.py                   `.air-meta.json` read/write + /air:learn trigger threshold
│   │   ├── wiki_git.py               clone + commit-meta-with-retry helpers
│   │   ├── pattern_lifecycle.py   # Deterministic author-pattern lifecycle ops
│   └── pr_conversation.py        merge GitHub PR comments/reviews into `<pr-conversation>` agent context
│   └── .claude-plugin/
│       └── plugin.json             Plugin manifest (version source of truth)
│
├── managed/                        ← MANAGED AGENT (Anthropic cloud)
│   ├── api.py                        Shared helpers: get_headers, list_agents, find_environment
│   ├── setup.py                      Creates/updates 6 specialist agents via API (no orchestrator agent)
│   ├── review.py                     Client-side orchestrator — fans out the specialists via asyncio.gather, runs verifier, posts comment
│   ├── learn.py                      Triggers wiki maintenance sessions (single-agent)
│   ├── render_store_to_wiki.py       Deterministic store→wiki mirror render (inverse of migrate split; throttled per-review + on learn)
│   ├── test-session.py               9-test verification (repo, auth, blame, comment, wiki)
│   ├── test-learn.py                 Wiki clone/push verification
│   ├── test-render.py                Render round-trip / overflow-inverse unit tests (pure, no API)
│   ├── test-parallel.py              Smoke test for parallel sub-agent execution (detects Research Preview access)
│   ├── prompts/
│   │   └── learn-orchestrator.md     Learn pipeline for cloud (review orchestrator.md deleted in v1.7.0 — replaced by review.py)
│   └── requirements.txt             anthropic>=0.93.0, requests>=2.28.0
│
├── .github/workflows/
│   ├── managed-review.yml           Reusable GitHub Action (teams reference this)
│   ├── air-review.yml               Dogfood caller for this repo (PR + workflow_dispatch)
│   └── release-please.yml           Automated tag + GitHub Release on version bumps
│
├── .claude-plugin/
│   └── marketplace.json              Plugin marketplace distribution
│
├── CLAUDE.md                         Project conventions (references both plugin and managed)
├── README.md                         User docs with CLI + CI setup guides
├── .gitignore                        Excludes: managed/config.json, *.pem, *.pyc
└── LICENSE
```

---

## What's Shared vs Separate

| Component | CLI Plugin | Managed Agent | Shared? |
|---|---|---|---|
| Agent prompts (5 files) | Loaded as subagent_type | Read by setup.py → API agents | **YES — single source** |
| Wiki patterns (6 files) | git clone/push locally | git clone/push from sandbox | **YES — same wiki** |
| Review output format | Defined in review.md | Templated in review.py verifier prompt | Equivalent — same markdown shape |
| Orchestrator logic | review.md (markdown → Claude Code) | review.py (upstream prep) → air-coordinator session (callable_agents) | Mirror architectures since v1.9.0 |
| Learn logic | learn.md | learn-orchestrator.md | NO — duplicated (single-agent flow, not fanned out) |
| Auth | User's local gh auth | Bot PAT via github_repository resource | Different |
| Trigger | Manual: /air:review | Automatic: GitHub Action on PR | Different |
| Modes | --self, --respond (+ --dry-run), --full, --re-review, --fresh, --rewrite, --closed, --dry-run | auto, fresh, re-review, closed | CLI has more |
| Respond self-check | Scales by diff size: < 50 lines = code-reviewer + verifier only | Same (in orchestrator) | YES — same logic |
| Cross-repo wiki | Reads TARGET repo's wiki (skip write only) | N/A | Changed in v1.4.0 |
| Codex (GPT-5.4) | Optional 5th reviewer (CLI subprocess) | Optional 5th source — GHA subprocess, output bundled into the mounted verifier-task.md (inline fallback: coordinator user message) | YES — both gated on OPENAI_API_KEY |
| GitLab | Supported via platform-gitlab.md | Not yet | CLI only |

---

## CLI Plugin Pipeline (13 steps)

```
/air:review [number] [flags]
  │
  ├── Step 1: Parse arguments (PR number, flags, cross-repo detection)
  ├── Step 2: Smart default (check existing reviews, auto re-review)
  ├── Step 3: Load context (CLAUDE.md, wiki patterns, project memory, session context)
  ├── Step 3.5: First-run project discovery (PROJECT-PROFILE.md + GLOSSARY.md + `.air-checks.sh` [v1.6.0+])
  ├── Step 4: Fetch PR data (batched API, diff, commits, blame, churn, previous PR comments, current PR conversation)
  ├── Step 5: Pre-flight checks (state, draft, CI, conflict markers, file complexity, pure-promotion detection)
  ├── Step 6: Re-review mode (inter-diff, developer responses, FIXED/NOT FIXED tracking)
  │
  ├── Step 7: Parallel review ← the in-scope reviewers launched simultaneously (UI/copy joins on user-facing diffs)
  │   ├── Phase A: Codex (background, GPT-5.4)
  │   └── Phase B: the core agents via Agent tool (+ ui-copy-reviewer on user-facing diffs)
  │       ├── code-reviewer (+ author pattern matching)
  │       ├── simplify (reuse, quality, efficiency)
  │       ├── security-auditor (31-item checklist + author patterns)
  │       ├── git-history-reviewer (blame, churn + author patterns)
  │       └── ui-copy-reviewer (jargon / AI-fluff + static UX/a11y — conditional)
  │
  ├── Step 8: Verification (review-verifier filters false positives, bootstrap calibration defaults when no SEVERITY-CALIBRATION.md exists)
  ├── Step 9: Console attribution (severity table, drops/downgrades — never posted)
  ├── Step 10: Consolidate (deduplicate, strengths, wiki drift collection)
  ├── Step 11: Format (clickable links, sequential numbering, code blocks)
  ├── Step 12: Post (new comment or PATCH, own-PR guard, review verdict)
  └── Step 13: Learn (author pattern lifecycle, graduated resistance, wiki push)
```

**Additional modes (extracted into separate files):**
- `--self` / `--self --fix` — (`review-self.md`) review local changes, generate fix plan, optionally auto-apply. Never posts a PR comment; wiki patterns still push.
- `--respond` — (`review-respond.md`) auto-classify findings, self-check (scaled by diff size: < 50 lines uses code-reviewer + verifier only), post response. Supports `--dry-run`.
- `--full` — review entire codebase (all files, console only)

**Pre-commit drift check (v1.6.0+, CLI-only):** The plugin registers a `PreToolUse` hook on `Bash` via `hooks/hooks.json`. The wrapper at `hooks/pre-commit-drift.py` narrows to `git commit` calls (handles `git -C <path> commit`, respects `--no-verify`), locates the repo root, and runs either the repo's executable `.air-checks.sh` (custom rules) or `hooks/builtin-checks.sh` (zero-config auto-detection of manifest-version vs doc mirrors). Non-zero exit blocks the commit with output shown to Claude. Step 3.5 and `/air:learn` Step 4.65 generate/augment `.air-checks.sh` from the wiki's `PROJECT-PROFILE.md`. Custom scripts receive `$AIR_PLUGIN_ROOT` in env so they can delegate to built-ins.

---

## Managed Agent Pipeline (v1.9.0 — multi-agent coordinator)

```
PR opened (or air-machine requested as reviewer) → GitHub Action → managed/review.py
  │
  ├── [1] Sync 6 specialist agents + air-coordinator + air-solo-reviewer (setup.py: find by name → create or PATCH)
  ├── [2] Fetch PR metadata + diff from GitHub API (via AIR_BOT_TOKEN on the runner)
  ├── [3] Build PR Context block (Python)
  ├── [4] Optional codex pass (Pattern B, GHA-side sequential): `codex review --base <sha>`
  │       output html-escaped + length-capped (prompt-injection blast radius), bundled into
  │       verifier-task.md (file-handoff) or the coordinator user message (inline fallback)
  │
  ├── [4.5] File-handoff upload (v1.18.0, EXPERIMENTAL — AIR_FILE_HANDOFF=1, off by default):
  │       PR context, diff, and verifier task + codex findings upload via the Files API and
  │       mount at /workspace/context/; coordinator user message shrinks to a pointer note.
  │       BLOCKED on runtime: callable-agent threads run in isolated containers — file
  │       resources + cross-thread writes don't propagate (verified 2026-06-03). Inline is
  │       the production shape; upload failure also falls back to inline.
  │
  ├── [5] One air-coordinator session (callable_agents multi-agent runtime, beta header
  │       `managed-agents-2026-04-01-research-preview`):
  │     ├── TURN 1: dispatches the 4 Claude specialists in parallel as sub-agents
  │     │     (air-code-reviewer, air-simplify, air-security-auditor, air-git-history-reviewer)
  │     │     — file-handoff: each delegation is a short pointer; specialists read the mounted
  │     │     context/diff and write findings to /workspace/findings/<name>.md (1-line ack back;
  │     │     simplify replies inline — no write tool)
  │     ├── TURN 2: dispatches air-review-verifier — file-handoff: pointer at the context/diff/
  │     │     task mounts + the findings directory; inline fallback: embeds all findings
  │     │     + codex findings + the verifier_task template
  │     └── TURN 3: outputs verifier verdict verbatim + bash to update wiki REVIEW.md
  │
  └── [6] Python posts the review comment via GitHub API + runs the /air:learn epilogue when
        the wiki-backed counter threshold fires
```

The Python driver does upstream prep (fetch PR data, state gates, build context, optionally run codex), then hands off to a single **`air-coordinator` session** that dispatches the specialists in parallel + verifier as `callable_agents` sub-agents within one Anthropic session — mirroring the local CLI's Claude Code orchestrator. This replaced v1.7's client-side `asyncio.gather` over 5 separate sessions once Anthropic granted research-preview access for `callable_agents` on 2026-04-25 (beta header `managed-agents-2026-04-01-research-preview`).

**Review-architecture axis (`AIR_REVIEW_MODE` / `review_mode` input / `review.py --mode`):** `full` (default) runs the coordinator above; `solo` replaces step [5] with ONE `air-solo-reviewer` session (its system prompt assembled at sync from the 6 specialist `.md` files — `setup.py:assemble_solo_prompt()`, zero-drift, no standalone file; the agent is created only when a run uses solo/both), which applies all lenses + self-verifies + folds Codex in one pass (~$2–4 / ~7 min vs ~$10 / ~25 min on qai-be #994); `both` runs the coordinator AND the solo session **concurrently** (wall-clock ≈ the slower of the two — keeps it inside the GHA cap; a solo failure can't take down the gating coordinator review), with the coordinator review gating + driving the verdict/learn and the solo review posted alongside as a non-gating `## Code Review (solo — experimental)` comment for comparison. Solo posts the same `APPROVE`/`REQUEST_CHANGES` verdict as full (it can gate/approve), but is **not gate-safe** — it downgrades blocker severity, so that verdict isn't a trustworthy hard gate; enable only where a single agent's verdict is acceptable. Default is `full`. Managed-only — the CLI runs its agents locally and has no solo equivalent. `air-solo-reviewer` is not pinnable. The required-agents gate is conditional on the mode (full-only repos never require the solo agent).

---

## Agent Prompts (Shared, Single Source of Truth)

**code-reviewer.md** — Bugs, logic errors, error handling, design, test coverage. Checks orphan imports on deleted files, reference updates on renames. Greps `CLAUDE.md` plus `**/*CONTEXT*.md` / `**/*HANDOFF*.md` / `**/*GOTCHAS*.md` files (any depth) for diff-scope keywords (path-keyed gotcha cross-reference). Paired-doc drift check — flags missing companion-doc updates when a PR adds a row to an enumerated structure (IAM keys, secrets, callers, sub-modules). Gate-output symmetry check — flags asymmetric admission-vs-payload patterns where an aggregate-predicate scope admits a parent but the eager-load returns unfiltered children (cross-tenant data-leak class; blocker-grade for PHI / multi-tenant data). Author pattern annotations are inline in the output format section (for EVERY finding, check against known patterns). Parameter sprawl and leaky abstractions under Design & Architecture.

**simplify.md** — Three sections:
- Code Reuse: active codebase search via Grep/Glob, reinvented utilities, missed shared modules
- Code Quality: dead code, copy-paste with variation, stringly-typed code, unnecessary comments, redundant state
- Efficiency: N+1 patterns, missed concurrency, hot-path bloat, TOCTOU, overly broad operations, no-op updates, unbounded structures

**security-auditor.md** — 31-item checklist:
- Sensitive data (6), injection (4), auth (3), input validation (3), data exposure (3), operational security (4), silent failures (5), resource exhaustion (3)
- PROJECT-PROFILE.md controls which checks apply per repo
- Author pattern annotations inline in output format (security-relevant patterns are high-signal)

**git-history-reviewer.md** — Blame analysis (stale code, absent authors, integration boundaries), churn patterns (5+ commits/6mo = design smell), previous PR review comments, author pattern matching.

**review-verifier.md** — Post-review quality gate. Reads actual source at flagged lines. 6 verdicts: CONFIRMED, DOWNGRADED, IMPROVEMENT, PRE-EXISTING, ACCEPTED PATTERN, FALSE POSITIVE. Confidence scoring (0-100), default threshold 60. SEVERITY-CALIBRATION.md overrides per-agent thresholds when sufficient data exists.

---

## Author Pattern Lifecycle

Patterns in REVIEW.md are behavioral profiles that evolve over time:

```
### alice
- **Shell injection risk** (3x: #45, #52, #67 | last 2 PRs: 2 clean): Misses escapeshellarg() on user input
- **Empty array guard** (1x: #67 | new): Uses implode() on arrays without checking empty first
```

Format: `**<Pattern name>** (<Nx>: <PR refs> | last <N> PRs: <M> clean): <Description>`

Lifecycle:
- **Create** — `(1x: #PR | new)` — generalize from specific incident to behavioral tendency
- **Strengthen** — increment count, add PR ref, reset clean counter to 0
- **Decline** — 5 consecutive clean PRs → append `(declining)`
- **Archive** — 10 consecutive clean PRs → move to `### <author> (archived)`
- **Never delete** — archived patterns stay permanently as historical context

3 of 4 review agents (code-reviewer, security-auditor, git-history-reviewer) annotate findings with `[matches author pattern: <name> (<Nx>)]`. The orchestrator uses annotations to drive lifecycle transitions in Step 13.

---

## Wiki Storage (6 pages per repo)

| Page | Purpose | Updated by |
|---|---|---|
| REVIEW.md | Curated patterns: common findings, author profiles, service gotchas | Every review (Step 13) + learn |
| REVIEW-HISTORY.md | Auto-generated analytics: finding frequency, file hot spots, author trends | Learn (KAIROS) |
| PROJECT-PROFILE.md | Project characteristics: languages, architecture, review focus rules, applicable security checks | First-run discovery + learn refresh |
| GLOSSARY.md | Domain terminology: prevents false findings on intentional naming | First-run + learn |
| ACCEPTED-PATTERNS.md | Team-approved patterns that suppress matching findings | Developer disputes (graduated resistance) |
| SEVERITY-CALIBRATION.md | Per-agent confidence thresholds from dispute rates | Learn (when 10+ data points) |

### Store→wiki mirror render (store-backed repos)

For migrated repos the store is the source of truth and the wiki is an **exported mirror**. `managed/render_store_to_wiki.py` renders it deterministically (no AI session) — the inverse of `migrate_wiki_to_store.py`'s split: it reassembles REVIEW.md by driving off `/review-misc.md` as the structural spine (the verbatim catch-all that holds the H1 + `## Author Patterns` heading/intro + tail sections) and injecting the reassembled `## Common Findings`, `## Service-Specific Patterns`, and sorted per-author `### <login>` blocks at their anchor positions, plus pass-through of the shared whole-file memories (overflow chunks reassembled). REVIEW-HISTORY.md (not in the store) and the counter are skipped.

It runs in two places, both store-id-gated and best-effort (a failure never fails the review/learn):
- **Throttled per-review** (`review.py::_maybe_render_mirror`): `meta.py mirror-due` is a cheap meta read; only when the mirror is stale by ≥`MIRROR_INTERVAL_HOURS` (1h) does it clone+render+push and stamp `mirror-rendered`. Within the window it's a no-op — no git op. This keeps the wiki fresh between learns (the raw post-`pattern_writer` store).
- **Authoritatively on learn** (`learn.py`, after the curation session): always renders the freshly curated store and stamps `mirror-rendered`, resetting the per-review throttle. The AI session pushes only REVIEW-HISTORY.md (single file, before the deterministic render — disjoint files, sequenced first to avoid a non-ff race).

Managed-only: the CLI has no store render, so a CLI-only store repo sees a stale wiki between managed runs (accepted gap; CLI store writes are a later phase). Losslessness (`split(render(store)) == store`, modulo whitespace) is covered by `managed/test-render.py`.

---

## Authentication (Managed Agent)

**Decision: Machine bot account with classic PAT**

- Bot account: `air-machine` (regular GitHub account used as bot)
- Classic PAT with `repo` scope (fine-grained PATs don't support wiki push or GraphQL comments)
- Token passed two ways:
  - `github_repository` resource: mounts repo with auth (clone/push, token in API request, not conversation)
  - `GH_TOKEN` in session message: for `gh` CLI (comments, review verdicts). Visible in Anthropic session logs — accepted tradeoff with minimal-permission bot account.

**Alternatives evaluated and rejected:**

| Option | Why rejected |
|---|---|
| GitHub App | Complex onboarding (private key, JWT, installation tokens) |
| Fine-grained PAT | Doesn't support wiki push or GraphQL comments |
| Vault + MCP OAuth | Read-only — can't write comments or push wiki |
| GITHUB_TOKEN | Free but can't push to wiki repos |
| Centralized token service | Requires hosting infrastructure |

---

## Agent Management

**Self-bootstrapping:** First PR on any org auto-creates agents. No manual setup.py needed.

**Find by name:** `GET /v1/agents` → Python driver looks up each of the 6 specialists (`air-code-reviewer`, `air-simplify`, `air-security-auditor`, `air-git-history-reviewer`, `air-ui-copy-reviewer`, `air-review-verifier`) by name. No config files, no stored IDs. Each org's API key isolates their agents.

**Auto-update:** Every run calls setup.py which PATCHes each agent with the latest prompt from the air repo. Uses `version` field for optimistic concurrency. When you merge a prompt change to main, the next PR on any org picks it up automatically.

**Duplicates:** If race condition creates multiples, newest is used (dict overwrite on reversed API list). Harmless — clean up manually if needed.

**Agent inventory per org:**

| Agent | Model | Tools | Purpose |
|---|---|---|---|
| air-code-reviewer | Opus | read, grep, glob, bash | Code quality review |
| air-simplify | Sonnet | read, grep, glob | Reuse, quality, efficiency (no bash) |
| air-security-auditor | Opus | read, grep, glob, bash | 31-item security audit |
| air-git-history-reviewer | Haiku | read, grep, glob, bash | Blame, churn, history |
| air-review-verifier | Opus | read, grep, glob, bash | False-positive filtering + emits the final review comment markdown (v1.7.0+) |
| air-learner | Sonnet | all (agent_toolset) | Wiki maintenance |
| air-test | Sonnet | all (agent_toolset) | Quick 9-test verification |

**Note:** `air-reviewer` (server-side orchestrator) was removed in v1.7.0 — `managed/review.py` is now the orchestrator (client-side). Existing deployments can safely archive or leave the old `air-reviewer` agent — it's orphaned but harmless.

Model tiering introduced in v1.5.0: judgment-heavy reviewers stay on Opus, mechanical / pattern-matching reviewers (simplify, git-history-reviewer) run on Sonnet for ~5× cheaper input. Models are declared in each agent's frontmatter (`plugins/air/agents/<name>.md`) and resolved to API IDs via `managed/setup.py::MODEL_ALIASES`.

---

## Team Onboarding (per org)

| Step | Who | Time | What |
|---|---|---|---|
| 1 | Org admin | 2 min | Create bot GitHub account |
| 2 | Org admin | 1 min | Add bot as collaborator (Write) to repos |
| 3 | Org admin | 2 min | Generate classic PAT (`repo` scope) on bot account |
| 4 | Org admin | 2 min | Set `ANTHROPIC_API_KEY` + `AIR_BOT_TOKEN` as org secrets |
| 5 | Any dev | 1 min | Add workflow YAML to repo |

First PR auto-bootstraps. No setup scripts, no config files, no CLI installation required.

---

## Review Output Format

Both CLI and managed produce identical format:

```
## Code Review

<one-line summary>

### Security Audit: <pass>/<total> PASS

| Check | Result |
|---|---|

### Blockers
**1. <description>**
[`file#Lstart-Lend`](link) — <explanation>
```code block if helpful```

### Medium / Low / Nits
(same format, sequential numbering)

### Pre-existing Issues
(not introduced by PR, don't block merge)

### Related PRs   <!-- only when concurrent open PRs touch the same files -->

### Strengths
- <1-3 specific observations>

---
<N> findings for this PR. Blockers should be fixed before merge.
Reviewed at: <SHA>
> After fixing, run `/air:review --respond` to verify and reply.
```

---

## Respond Format

```
## Review Response

<conclusion — e.g., "All 6 findings fixed.">

Responding to review at <SHA>.

### Fixed
**#1 — <description>**
fixed: <how it was fixed>

### Disputed / Acknowledged / Partially Fixed
(grouped by status)

### Additional Changes / Self-check Notes
(if applicable)

---
Changes: +N/-N across M files.
Responded at: <SHA>
```

---

## Cost

Measured from real Managed Agents session usage (~340 review sessions, May–June 2026; token rates: Opus 4.8 $5/$25, Sonnet 4.6 $3/$15, Haiku 4.5 $1/$5 per MTok):

| Session | Tokens (median) | Cost (median) | Cost (heavy PR) |
|---|---|---|---|
| Review — coordinator + 4 specialists + verifier (+ UI/copy specialist on user-facing diffs) | ~5M cache-read, ~0.5M cache-write, ~80K output | **~$5–9** | $15–30 (30M cache-read) |
| Learn epilogue — full wiki cleanup | ~15M cache-read, ~0.3M cache-write, ~65K output | ~$8–11 on Opus (pre-v1.15.0); ~40% less on Sonnet | $20+ |
| Session runtime ($0.08/h) | 10–45 min | ~$0.02–0.06 | — |

Cost ranges span Sonnet-rate (floor) to Opus-rate (ceiling) bounds — sub-agent usage is lumped into the coordinator session, so the exact model mix isn't separable. The dominant driver is cache-read volume: the multi-agent loop re-reads the PR context + wiki block on every tool-use turn. Cost levers in order: review density (push-triggered re-reviews), learn cadence (cut ~3× in v1.15.0: 15-review/14-day, was 5/2), learner model (Opus → Sonnet in v1.15.0). The fast-mode premium ($10/$50 on the Messages API) is not billed on Managed Agents sessions. Earlier revisions of this table quoted "$15/$75" Opus rates (wrong — Opus 4.5→4.8 bill $5/$25) and per-agent one-shot estimates (~50× below real agentic-session reads).

---

## Known Limitations

**Managed Agent:**
- Parallel execution via server-side `callable_agents` (v1.9.0+, multi-agent coordinator). Specialists fan out concurrently as sub-agents within one Anthropic session; wall-clock ≈ slowest specialist + verifier (~10-25 min depending on PR size). Beta header `managed-agents-2026-04-01-research-preview`.
- GH_TOKEN visible in Anthropic session logs (mitigated by bot account minimal permissions, rotatable).
- Wiki push can timeout in sandbox (5-min command limit). The coordinator's TURN 3 bash has a one-shot rebase-retry to recover from concurrent reviewer pushes; failures are logged, not fatal (the review comment is already posted).
- Codex (GPT-5.4) runs as a GHA-side subprocess sequentially before the coordinator (Pattern B); output is html-escaped and length-capped before being bundled into the mounted verifier-task.md (or the coordinator's user message on inline fallback). Gated on `OPENAI_API_KEY` GHA secret.
- GitHub-only — no GitLab support yet.
- `github_repository` resource only clones the PR branch — base branch must be fetched separately (`git fetch origin main`).

**CLI Plugin:**
- review.md reduced to 879 lines (from 1276) — --self and --respond extracted to separate files. Still long; further extraction planned.
- Subagents CANNOT spawn other subagents (Claude Code hard limit, nesting depth = 1).
- Plugin auto-update unreliable — marketplace pulls repo but doesn't always re-install to cache.
- Auto-trigger for /air:learn sometimes skipped due to prompt length (mitigated with >>> markers and explicit RETURN in Step 13).

**Both:**
- Agent prompts are shared via `plugins/air/agents/*.md` — single source of truth.
- Orchestrator logic is now implementation-specific by design (review.md for CLI, review.py for managed) since one is a Claude Code markdown instruction and the other is Python code. No prose duplication between `orchestrator.md` and `review.md` anymore — the managed orchestrator prompt was deleted in v1.7.0.

---

## CLI Orchestrator Research

Subagents cannot nest in Claude Code — only the main session can use the Agent tool. The current architecture (review.md as orchestrator → Agent tool → sub-agents) is the correct pattern. Inconsistency comes from review.md being too long (1276 lines), not from wrong architecture.

**Fix:** Slim review.md to ~300 lines by extracting verbose sections (format rules, wiki learning protocol, resistance levels, author pattern lifecycle) into reference files. Same architecture, less prompt bloat, more consistent execution.

**Agent Teams** (experimental, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`): alternative with peer-to-peer communication between teammates. Known bug: teammates lack Agent tool (issue #31977). Not production-ready.

**Deferred idea:** CLI triggers Managed Agent (cloud execution from terminal) — would unify execution model but requires internet and can't handle --self mode (local uncommitted changes).

---

## Roadmap

| Priority | Item | Effort | Impact | Status |
|---|---|---|---|---|
| **Done** | Wiki-backed shared `/air:learn` counter | 0.5 day | Managed reviews now contribute to the learn cadence | `plugins/air/lib/meta.py` owns threshold logic; CLI + managed both bump the same wiki `.air-meta.json` |
| **Future** | Managed per-review wiki writes (Layer 1) | ~3 days | Managed contributes patterns every review instead of only via periodic deep pass | Requires `json-patterns` verifier contract + module expansion (`wiki_learn.py`, `author_patterns.py`, `review_md.py`, `learned_patterns.py`) |
| **Future** | CLI Step 13 sub-steps 2 + 2.5 migration to Python | ~2 days | Deterministic author-pattern lifecycle; saves ~15–20K tokens per CLI review | Depends on the module expansion landing first |
| **Future** | LLM-sanitization helper for disputed findings | ~1 day | Closes CLI/managed asymmetry on ACCEPTED-PATTERNS.md (sub-step 3) | Small Python helper calling Anthropic API; both orchestrators call it |
| **Future** | Inter-diff + respond + self-review logic unification | ~3 days | Shared helpers for non-orchestration logic paths | Modular — can ship one at a time |
| **Future** | GitLab platform support in managed | ~1 week | Managed works on GitLab MRs | Abstract `fetch_pr_*` + `github_repository` resource shape |
| **Done** | Orphan-session cleanup on driver shutdown | 0.5 day | Token savings + cleaner ops | v1.8.0 — atexit + SIGTERM/SIGHUP interrupts tracked session ids |
| **Done** | Auto-detect re-review mode (managed) | 1 day | Cost + feedback loop | v1.8.0 — inter-diff + prior review + dev comments as context |
| **Done** | `--closed` opt-in for closed/merged PRs | 0.5 day | Post-merge audit, pattern backfill | v1.8.0 — state gate + commit-checkout + workflow_dispatch |
| **Done** | Parallel execution for managed reviews | 1 day | 12 min → ~5-8 min | v1.7.0 — client-side asyncio fan-out (Python driver), replaces server-side callable_agents dependency |
| **Blocked** | Server-side parallel sub-agents (callable_agents) | Would simplify review.py | Marginal — client-side fan-out already parallel | Waiting for Research Preview access; `managed/test-parallel.py` detects readiness |
| **Done** | Slim review.md (1319 → 879 lines) | 1 day | CLI consistency | v1.4.0 — extracted --self + --respond |
| **Done** | Respond self-check scaling | 0.5 day | Token savings | v1.4.0 — < 50 lines = fewer agents |
| **Done** | Cross-repo wiki read | 0.5 day | Pattern context | v1.4.0 — reads target wiki, skip write |
| **Done** | Severity calibration bootstrap | 0.5 day | New project UX | v1.4.0 — default thresholds table |
| **Done** | Pure-promotion PR detection | 0.5 day | Workflow | v1.4.0 — warn and offer skip |
| **Done** | Auto-trigger visibility | 0.5 day | Reliability | v1.4.0 — markers + explicit RETURN |
| **Done** | Respond --dry-run | 0.5 day | Preview | v1.4.0 |
| **High** | Reduce orchestrator duplication | 1 day | Maintenance burden | |
| **High** | Further slim review.md (879 → ~300) | 1 day | CLI consistency | Extract verbose sections to reference file |
| **Medium** | GitLab in managed agent | 2-3 days | Platform coverage | |
| **Medium** | Wiki push reliability | 1 day | Sandbox timeout handling | |
| **Low** | Codex in managed agent | 1 day | Second model opinion | |
| **Deferred** | CLI triggers Managed Agent | 1 week | Unified execution model | |
| **Deferred** | Cowork plugin | 1-2 weeks | Non-CLI users | |
| **Deferred** | Slack/Confluence integrations | 1 week | Team visibility | |
| **Deferred** | Agent Teams (experimental) | Research | Alternative parallelism | |

---

## Key Decisions Made

| Decision | Rationale |
|---|---|
| Bot account over GitHub App | Simpler onboarding — no private key, no JWT, no installation IDs |
| Classic PAT over fine-grained | Fine-grained doesn't support wiki push or GraphQL comments |
| Agents found by name, not stored | Eliminates config files, enables self-bootstrapping |
| Newest agent per name used | Dict overwrite on reversed API list; duplicates are harmless |
| GH_TOKEN in session message | github_repository handles clone; gh CLI needs env var; no API support for session env vars |
| Auto-update on every run | setup.py PATCHes agents with latest prompts; ~2s overhead per run |
| Codex skipped in managed | Requires OpenAI plugin/key in sandbox; optional in CLI too |
| Client-side fan-out for managed reviews (v1.7.0) | callable_agents gated behind Research Preview — Python driver parallelizes via asyncio.gather instead |
| Code blocks in review findings | Improves clarity — both CLI and managed include them |
| Author patterns never deleted | Archived after 10 clean PRs but stay permanently as historical context |
| --self and --respond extracted to separate files | Reduces review.md cognitive load; each mode is self-contained with its own reference to shared steps |
| Respond self-check scales by diff size | < 50 lines = code-reviewer + verifier only; saves ~60% tokens on small fix diffs |
| Cross-repo reads target wiki | Reading patterns from unfamiliar repo is highest value; writing skipped to avoid pollution |
| Bootstrap severity calibration | Default thresholds (security=70, simplify=55, others=60) until project accumulates 10+ data points |
| Author pattern annotations inline in output format | Moved from separate section at end of agent file to where findings are produced — increases compliance from ~60% to expected higher |
