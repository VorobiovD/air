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

## Enable on a repo

Add this file:

```yaml
# .github/workflows/air-review.yml
name: air review
on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      AIR_BOT_TOKEN: ${{ secrets.AIR_BOT_TOKEN }}
```

First PR auto-bootstraps the agents. Subsequent PRs reuse them.

## How it works

```
PR opened
  │
  ▼
GitHub Action triggers
  │
  ├── Checks if agents exist (by name via API)
  ├── If not → creates environment + 5 sub-agents + orchestrator
  ├── If yes → updates prompts to latest from air repo
  │
  ▼
Creates Managed Agent session
  │
  ├── Repo mounted via github_repository resource
  │   (token in API request, NOT in conversation)
  ├── PR branch pre-checked-out at /workspace/repo
  ├── gh CLI authenticated as bot account
  │
  ▼
Orchestrator runs review pipeline
  │
  ├── Fetches PR data, loads wiki context
  ├── Delegates to 4 reviewer sub-agents
  │   (parallel when multi-agent enabled, sequential for now)
  ├── Runs verification agent
  ├── Posts review as bot account
  └── Pushes learned patterns to wiki
```

## Manual trigger

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export AIR_BOT_TOKEN=ghp_...

pip install -r requirements.txt
python review.py myorg/myrepo 123        # stream mode
python review.py myorg/myrepo 123 --poll  # poll mode
```

## Agent updates

When agent prompts change in the air repo, the workflow auto-updates deployed agents on the next PR (compares and patches via API). No manual step needed.

## Security

- **Repo clone/push**: authenticated via `github_repository` resource (token in API request, not conversation)
- **gh CLI (comments, verdicts)**: `GH_TOKEN` passed in session message. This is visible in Anthropic's session logs. Mitigated by: bot account with minimal permissions, classic PAT with `repo` scope only, rotatable.
- **Permissions**: `repo` scope on bot account (needed for wiki push — fine-grained PATs don't support wiki)
- **Agent access**: each org's agents are isolated under their own Anthropic API key

## Cost

~$1.69 per review. At 40 reviews/month: ~$67/month.
