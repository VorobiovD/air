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

**Client-side orchestration (v1.7.0+)**: the Python driver is the orchestrator. Anthropic's parallel-sub-agents feature (`callable_agents`) is gated behind a Managed Agents multiagent Research Preview, so we fan out client-side from `review.py` via `asyncio.gather` instead.

```
PR opened
  │
  ▼
GitHub Action triggers `python review.py <repo> <pr>`
  │
  ├── Syncs 5 specialist agents (creates on first run, updates prompts on subsequent runs)
  ├── Fetches PR metadata + diff via GitHub API
  │
  ▼
Python driver orchestrates
  │
  ├── asyncio.gather 4–5 specialist sessions in parallel (each its own container):
  │     ├── air-code-reviewer      — bugs, design, test coverage
  │     ├── air-simplify           — reuse, quality, efficiency
  │     ├── air-security-auditor   — 31-item checklist
  │     ├── air-git-history-reviewer — blame, churn, recurring patterns
  │     └── codex (opt-in)         — OpenAI Codex (adds 5th source if OPENAI_API_KEY is set)
  │     (each Claude specialist clones repo + wiki, reads patterns, returns findings;
  │      codex runs as a subprocess in the runner against a locally-cloned target repo)
  │
  ├── Collects findings from all 4–5
  ├── Runs final session sequentially: air-review-verifier
  │     (verifies each finding, drops false positives, formats final review)
  │
  └── Posts the review comment to the PR directly via GitHub API
```

**Wall-clock:** ~5-8 minutes (specialists run concurrently; wall time ≈ slowest specialist + verifier, regardless of whether Codex is enabled). Without client-side fan-out (prior server-side orchestrator with `callable_agents`) the sub-agent calls returned permission-denied because the feature was access-gated.

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

Claude-only (default): ~$2.30 per review (model tiering at Opus 4.7 + Sonnet 4.6 pricing). At 40 reviews/month: ~$90/month.

With Codex enabled (`OPENAI_API_KEY` set): +$1–2 per review depending on diff size and Codex's default model (gpt-5.4 at the time of writing). Opt-out with the `no_codex` workflow input or `--no-codex` on manual invocation.
