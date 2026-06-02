# air Managed Agent

Automated code review on every PR — zero human trigger needed.

## Setup (per org, one-time)

1. Create a GitHub bot account (e.g., `air-reviewer-bot`)
2. Add it as collaborator (Write) to your repos
3. Generate a classic PAT on the bot account with `repo` scope
   (fine-grained PATs don't support wiki push)
4. Add two org secrets:
   - `ANTHROPIC_API_KEY` — your Anthropic API key with Managed Agents access
   - `AIR_BOT_TOKEN` — the bot's PAT
5. Optional: `OPENAI_API_KEY` — adds Codex as a 5th reviewer. Skipped cleanly if unset.

## Enable on a repo

Add this file:

```yaml
# .github/workflows/air-review.yml
name: air review
on:
  pull_request:
    types: [opened, synchronize, reopened]
  workflow_dispatch:
    inputs:
      pr_number:
        description: 'PR number to review (works on closed/merged PRs)'
        required: true
        type: string
      closed:
        description: 'Allow review of closed/merged PR'
        required: false
        type: string
        default: 'true'

jobs:
  review:
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    with:
      pr_number: ${{ inputs.pr_number }}
      closed: ${{ inputs.closed }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      AIR_BOT_TOKEN: ${{ secrets.AIR_BOT_TOKEN }}
```

First PR auto-bootstraps the agents. Subsequent PRs reuse them.

The `workflow_dispatch` trigger lets you review any PR on-demand from the Actions tab — including closed or merged PRs (post-merge audits, wiki-pattern backfills from history). For `pull_request` triggers, `pr_number` / `closed` defaults apply (current PR, state gate enforced).

## How it works

**Multi-agent coordinator (v1.9.0+)**: the Python driver does upstream prep (fetch PR data, state gates, build context, optionally run codex), then hands off to a single `air-coordinator` session that dispatches the specialists in parallel + verifier as `callable_agents` sub-agents within one Anthropic session — mirroring the local CLI's architecture. This replaced v1.7's client-side `asyncio.gather` over 5 separate sessions once Anthropic granted research-preview access for `callable_agents` on 2026-04-25.

```
PR opened (or air-machine requested as reviewer)
  │
  ▼
GitHub Action triggers `python review.py <repo> <pr>`
  │
  ├── Syncs 5 specialist agents + air-coordinator (creates on first run, updates prompts otherwise)
  ├── Fetches PR metadata + diff via GitHub API
  ├── Fetches current PR conversation (issue comments + reviews + inline comments) and bot identity
  │     concurrently — humans + other AI bots are surfaced to specialists as <pr-conversation>
  │     so findings can flag overlap with [already raised by @<author>]
  ├── Optional: runs `codex review --base <sha>` as a subprocess (Pattern B: GHA-side, sequential
  │     before the coordinator) — output threaded into the coordinator's user message as
  │     <codex-findings>...</codex-findings>, html-escaped and length-capped
  │
  ▼
Single air-coordinator session (callable_agents multi-agent runtime)
  │
  ├── TURN 1: dispatches specialists in parallel as sub-agents (one Anthropic session, one container):
  │     ├── air-code-reviewer       — bugs, design, test coverage
  │     ├── air-simplify            — reuse, quality, efficiency
  │     ├── air-security-auditor    — 31-item checklist
  │     └── air-git-history-reviewer — blame, churn, recurring patterns
  │
  ├── TURN 2: dispatches air-review-verifier with the 4 specialist findings + codex findings +
  │           the verifier_task template (verifies each finding, drops false positives, emits markdown)
  │
  └── TURN 3: outputs verifier's response verbatim + bash tool call to update the wiki (REVIEW.md
              author-pattern entry on recurring findings, with one-shot rebase-retry on push)
  │
  ▼
Python driver posts the review comment to the PR via GitHub API,
then bumps the wiki-backed counter and runs the /air:learn epilogue
synchronously when the threshold fires.
```

**Wall-clock:** ~10-25 min depending on PR size (specialists run concurrently in the coordinator; wall ≈ slowest specialist + verifier + optional codex). Beta header `managed-agents-2026-04-01-research-preview` unlocks `callable_agents`.

## Manual trigger

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export AIR_BOT_TOKEN=ghp_...

pip install -r requirements.txt
python review.py myorg/myrepo 123             # post review comment
python review.py myorg/myrepo 123 --dry-run   # print comment, skip post
python review.py myorg/myrepo 123 --fresh     # force full review (ignore re-review auto-detect)
python review.py myorg/myrepo 123 --closed    # review a closed/merged PR (default refuses)
python review.py myorg/myrepo 123 --no-codex  # skip Codex even if OPENAI_API_KEY is set
```

## Agent updates

When agent prompts change in the air repo, the workflow auto-updates deployed agents on the next PR (compares and patches via API). No manual step needed.

## Security

- **Repo clone**: authenticated via `github_repository` resource (token in API request, not conversation)
- **Wiki access from sessions**: each specialist session clones the wiki itself using `GH_TOKEN` injected via the user message — same token as the repo auth. Visible in Anthropic's session logs; mitigated by bot account with minimal permissions, classic PAT with `repo` scope only, rotatable.
- **Comment posting**: done client-side by `review.py` via GitHub API using `AIR_BOT_TOKEN` from the runner env — never sent to Anthropic.
- **Permissions**: `repo` scope on bot account (needed for wiki push — fine-grained PATs don't support wiki)
- **Agent access**: each org's agents are isolated under their own Anthropic API key

## Cost

Claude-only (default), **measured** from real session usage (~340 review sessions, May–June 2026): median **~$5–9 per review**, heavy PRs $15–30. Learn epilogue sessions: ~$8–11 each on Opus (pre-v1.15.0; ~40% less on Sonnet). The dominant driver is cache-read volume (~5M cached tokens read per median review session; 30M on large PRs) — output tokens and the $0.08/session-hour runtime are minor. The fast-mode premium is not billed on Managed Agents sessions. Real May 2026 total at ~300 reviews + 130 learns across repos: ~$2.5–4K — push-triggered re-review density is the biggest cost lever, followed by learn cadence (cut ~3× in v1.15.0).

With Codex enabled (`OPENAI_API_KEY` set): +$1–2 per review depending on diff size and Codex's default model (gpt-5.4 at the time of writing). Opt-out with the `no_codex` workflow input or `--no-codex` on manual invocation.
