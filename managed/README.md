# air Managed Agent

Automated code review on every PR — zero human trigger needed.

Uses Anthropic's Managed Agents API to run the air review pipeline in a cloud sandbox. Triggered by GitHub Actions on PR open/update. Posts reviews as `air-reviewer[bot]` via a GitHub App.

## Status

- Single-session sequential reviews: **working**
- Multi-agent parallel reviews (callable_agents): **pending** — agents are registered, waiting for multi-agent research preview access
- GitHub App authentication: **working** — posts as `air-reviewer[bot]` with 1-hour ephemeral tokens

## Prerequisites

- Anthropic API key with Managed Agents beta access
- Python 3.10+ with dependencies: `pip install -r requirements.txt`
- GitHub App with `contents: read`, `issues: write`, `pull_requests: write` permissions

## Setup (one-time)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# Create all resources (agents, environment, sub-agents, orchestrator)
python setup.py
```

This creates `config.json` with all resource IDs. **Do not commit this file** — it contains your agent IDs and GitHub App credentials. A `config.example.json` is provided as reference.

Add your GitHub App details to `config.json`:
```json
{
  "github_app": {
    "app_id": "YOUR_APP_ID",
    "installation_id": "YOUR_INSTALLATION_ID",
    "private_key_path": "~/path/to/your-app.private-key.pem"
  }
}
```

## Manual trigger

```bash
# Review a specific PR (generates App token automatically)
python review.py myorg/myrepo 123 --app-auth

# With explicit token
python review.py myorg/myrepo 123 --gh-token ghp_...

# Poll instead of stream
python review.py myorg/myrepo 123 --app-auth --poll
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

## Architecture

```
GitHub PR opened → GitHub Action → Anthropic Managed Agent session
                                          │
                                   Orchestrator agent
                                   (air-reviewer, Opus)
                                          │
                          ┌───────────────┤ (sequential now,
                          │               │  parallel when
                          │               │  multi-agent enabled)
                          ▼               ▼
                   4 review passes    verification pass
                   (code, simplify,   (review-verifier)
                    security, history)
                          │
                          ▼
                   Post PR comment
                   (as air-reviewer[bot])
                          │
                          ▼
                   Push wiki patterns
```

When multi-agent research preview is enabled, the orchestrator will spawn 4 parallel threads (one per reviewer) via `callable_agents` — no code changes needed, just a runtime feature flip.

## Security

- **GitHub App tokens**: 1-hour expiry, scoped to installed repos only
- **Token in session**: the `GH_TOKEN` appears in the Anthropic session message (stored in their systems). Mitigated by short expiry — even if leaked, the token is invalid within 1 hour.
- **Private key**: stored locally, never committed to git (`.gitignore`d)
- **Production recommendation**: replace PAT with GitHub App for bot identity and ephemeral tokens

## Cost

~$1.69 per review (same as CLI plugin). At 40 reviews/month: ~$67/month.
Session hours: ~$0.016 per review (~0.2 hr at $0.08/hr).

## Updating agents

When agent prompts change (new checklist items, patterns), re-run setup:

```bash
python setup.py
```

This creates new agent versions. Existing sessions are unaffected.
