# air Managed Agent

Automated code review on every PR — zero human trigger needed.

Uses Anthropic's Managed Agents API to run the full air review pipeline in a cloud sandbox. Triggered by GitHub Actions on PR open/update.

## Prerequisites

- Anthropic API key with Managed Agents beta access
- Python 3.10+ with `anthropic` package
- GitHub PAT with repo + wiki push permissions (for vault)

## Setup (one-time)

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...

# Create all resources (agent, environment, vault, sub-agents)
python setup.py --github-token ghp_...
```

This creates `config.json` with all resource IDs. Commit this file — teams reference it.

## Manual trigger

```bash
# Review a specific PR
python review.py myorg/myrepo 123

# Force fresh review
python review.py myorg/myrepo 123 --mode fresh

# Poll instead of stream
python review.py myorg/myrepo 123 --poll
```

## Team setup (automated via GitHub Actions)

Add this to any repo:

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
```

That's it. Every PR gets reviewed automatically.

## Architecture

```
GitHub PR opened → GitHub Action → Anthropic Managed Agent session
                                          │
                     ┌────────────────────┤
                     │                    │
              Orchestrator         5 Sub-agents
              (air-reviewer)       (callable_agents)
                     │                    │
                     │    ┌───────────────┼───────────────┐
                     │    │               │               │
                     ▼    ▼               ▼               ▼
              code-reviewer    simplify    security    git-history
                     │               │               │
                     └───────────────┼───────────────┘
                                     │
                              review-verifier
                                     │
                              Post PR comment
                              Push wiki patterns
```

Sub-agents share the sandbox filesystem — wiki files in /tmp/ are accessible to all.

## Cost

~$1.69 per review (same as CLI plugin). At 40 reviews/month: ~$67/month.

## Updating

When agent prompts change (new checklist items, new patterns), re-run setup:

```bash
python setup.py
```

This creates new versions of the agents. Existing sessions are unaffected.
