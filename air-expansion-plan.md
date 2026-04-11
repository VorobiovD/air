# air — Expansion Plan: Managed Agents, Cowork, and Team Rollout

**Author:** Dmytro Vorobiov  
**Date:** 2026-04-09  
**Status:** Draft  
**Current version:** 1.2.0 (Claude Code CLI plugin)

---

## Where We Are

air is a Claude Code CLI plugin — two slash commands (`/air:review`, `/air:learn`) that run locally on a developer's machine. It requires `gh` CLI authenticated, a local git checkout, and Claude Code running in a terminal. This works well for individual developers but can't scale to teams without everyone installing and configuring it independently.

## Where We Want to Be

Three distribution paths, each serving a different use case:

```
                          ┌─────────────────────────┐
                          │   air core engine        │
                          │   (agents + verifier +   │
                          │    learning pipeline)    │
                          └────┬──────┬──────┬──────┘
                               │      │      │
               ┌───────────────┘      │      └───────────────┐
               │                      │                      │
    ┌──────────▼──────────┐ ┌────────▼─────────┐ ┌─────────▼──────────┐
    │  CLI Plugin (today) │ │  Managed Agent   │ │  Cowork Plugin     │
    │                     │ │                  │ │                    │
    │  /air:review 123    │ │  API / webhook   │ │  manual in desktop │
    │  developer machine  │ │  Anthropic cloud │ │  Cowork sandbox    │
    │  gh CLI auth        │ │  vault-managed   │ │  paste diff / MCP  │
    │  wiki via git push  │ │  git push works  │ │  Confluence store  │
    └─────────────────────┘ └──────────────────┘ └────────────────────┘
```

---

## Phase 1: Managed Agent — Automated Reviews on Every PR

**Timeline:** 2–3 weeks  
**Impact:** Highest — fully automated, zero human trigger needed  
**Prerequisite:** Anthropic API key with Managed Agents beta access

### Why This First

Managed Agents solves every problem we identified in the architecture review. Auth is vault-managed (git push just works), sandboxes have full tooling (Bash, file ops, web), sessions are long-running (our 9–15 min pipeline fits perfectly), and the API is triggerable from webhooks. This isn't a workaround — it's the ideal runtime for air.

### Architecture

```
GitHub PR opened
  │
  ▼
GitHub Action / webhook listener
  │
  ▼
POST /v1/sessions  ──────────────────────────────────────┐
  │  agent: air-reviewer (Opus)                          │
  │  environment: air-env (gh, git, jq pre-installed)    │
  │  event: "Review PR #N on owner/repo"                 │
  │                                                      │
  ▼                                                      │
┌─────────────────────────────────────────┐              │
│  Managed Agent Session                  │              │
│                                         │              │
│  1. Clone repo (git auth pre-wired)     │   Anthropic  │
│  2. Fetch PR via gh CLI                 │   Cloud      │
│  3. Clone wiki (same token, git push)   │              │
│  4. Load context (REVIEW.md, profile)   │              │
│  5. Run 4 review agents (subprompts)    │              │
│  6. Run verifier agent                  │              │
│  7. Post PR comment via gh              │              │
│  8. Push learned patterns to wiki       │              │
│  9. Session → idle                      │              │
└─────────────────────────────────────────┘──────────────┘
```

### What to Build

**1.1 — Agent Definition**

Create the air-reviewer agent via the API. The system prompt is essentially review.md's orchestration logic, adapted from "instructions for Claude Code" to "instructions for an autonomous agent."

```python
agent = client.beta.agents.create(
    name="air-reviewer",
    model="claude-opus-4-6",
    system=REVIEW_SYSTEM_PROMPT,  # adapted from review.md
    tools=[
        {"type": "agent_toolset_20260401"},  # bash, read, write, edit, glob, grep, web
    ],
)
```

Key adaptation: review.md currently says "launch Agent tool with subagent_type: code-reviewer." In Managed Agents, there's no Agent tool — instead, the system prompt instructs Claude to run multiple sequential review passes within a single session, or uses the multi-agent research preview feature if available.

**1.2 — Environment Definition**

```python
environment = client.beta.environments.create(
    name="air-env",
    config={
        "type": "cloud",
        "networking": {"type": "unrestricted"},  # needs github.com access
        "setup_commands": [
            "apt-get update && apt-get install -y jq",
            "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg",
            "echo 'deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main' | tee /etc/apt/sources.list.d/github-cli.list",
            "apt-get update && apt-get install -y gh",
        ],
    },
)
```

Git auth: The environment gets a GitHub PAT via the vault. The token is wired into the git remote during sandbox init — `git clone`, `git push` (including wiki) work without the agent handling credentials.

`gh` auth: Set `GH_TOKEN` environment variable from vault. The `gh` CLI respects this automatically.

**1.3 — GitHub Action Trigger**

```yaml
# .github/workflows/air-review.yml
name: air review
on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger air review
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          # Create session with PR context
          SESSION=$(curl -fsSL https://api.anthropic.com/v1/sessions \
            -H "x-api-key: $ANTHROPIC_API_KEY" \
            -H "anthropic-version: 2023-06-01" \
            -H "anthropic-beta: managed-agents-2026-04-01" \
            -H "content-type: application/json" \
            -d '{
              "agent": "'$AIR_AGENT_ID'",
              "environment_id": "'$AIR_ENV_ID'",
              "title": "Review PR #${{ github.event.pull_request.number }}"
            }')

          SESSION_ID=$(echo $SESSION | jq -r '.id')

          # Send the review task
          curl -fsSL "https://api.anthropic.com/v1/sessions/$SESSION_ID/events" \
            -H "x-api-key: $ANTHROPIC_API_KEY" \
            -H "anthropic-version: 2023-06-01" \
            -H "anthropic-beta: managed-agents-2026-04-01" \
            -H "content-type: application/json" \
            -d '{
              "events": [{
                "type": "user.message",
                "content": [{
                  "type": "text",
                  "text": "Review PR #${{ github.event.pull_request.number }} on ${{ github.repository }}. Post your review as a PR comment. Push learned patterns to wiki."
                }]
              }]
            }'

          # Poll for completion (or use SSE stream)
          # Session emits session.status_idle when done
```

**1.4 — Adapt review.md for Managed Agent Context**

The current review.md is written as instructions for Claude Code's orchestrator. For Managed Agents, adapt:

| Current (CLI plugin) | Managed Agent adaptation |
|---|---|
| `Agent` tool with `subagent_type` | Sequential review passes within single session, or multi-agent API (research preview) |
| `/tmp/` file passing | Same — sandbox has `/tmp/` |
| `gh pr checkout` | `git clone` + `git checkout` (repo pre-cloned with auth) |
| Wiki clone via `git clone repo.wiki.git` | Same — git auth pre-wired in sandbox |
| `gh pr comment --body-file` | Same — `gh` installed, `GH_TOKEN` set |
| `CROSS_REPO` detection | Passed as session event parameter |
| `--dry-run`, `--self`, `--respond` flags | Different agents or session parameters for each mode |

The agent prompt files (code-reviewer.md, security-auditor.md, etc.) need zero changes — they're stateless prompts that receive context and return findings.

**1.5 — Shared Agent + Environment Management**

Create a small management script (Python or shell) that:
- Creates/updates the agent definition when review.md changes
- Creates/updates the environment when dependencies change
- Stores agent_id and environment_id in a config file
- Teams reference these IDs in their GitHub Actions

### Cost Estimate (Managed Agent)

| Component | Per review | Monthly (40 reviews) |
|---|---|---|
| Opus tokens (~162k) | ~$1.66 | ~$66 |
| Session hours (~0.2 hr) | ~$0.016 | ~$0.64 |
| GitHub Actions runner | ~$0.01 | ~$0.40 |
| **Total** | **~$1.69** | **~$67** |

Essentially the same cost as today's CLI usage, plus negligible session-hour fees.

---

## Phase 2: Cowork Plugin — Manual Reviews in Desktop App

**Timeline:** 1–2 weeks (can run in parallel with Phase 1)  
**Impact:** Medium — gives non-CLI users access to air's review engine  
**Prerequisite:** None beyond existing Cowork access

### Why This Too

Not everyone uses the terminal. PMs doing code review, tech leads triaging PRs on their phone, new team members who haven't set up `gh` — they all benefit from being able to paste a diff into Cowork and get air-quality findings.

### Architecture

```
User in Cowork desktop app
  │
  ├── Standalone: paste diff or point to local repo
  │     → full agent pipeline runs in Cowork sandbox
  │     → findings printed to conversation (no posting)
  │     → patterns saved to Confluence (if connected)
  │
  └── Connected (if GitHub MCP ever ships):
        → fetch PR diff via MCP tool
        → post comment via MCP tool
        → everything else same as standalone
```

### What to Build

**2.1 — Plugin Structure**

```
air-cowork/
├── .claude-plugin/
│   └── plugin.json
├── commands/
│   └── review.md          # Cowork-adapted orchestrator
├── skills/
│   └── air-review/
│       └── SKILL.md        # Auto-triggered skill description
├── agents/                 # Same agent files, unchanged
│   ├── code-reviewer.md
│   ├── security-auditor.md
│   ├── simplify.md
│   ├── git-history-reviewer.md
│   └── review-verifier.md
├── .mcp.json               # Pre-configured MCP servers (future)
├── CONNECTORS.md           # ~~source control placeholder docs
└── README.md
```

**2.2 — Cowork Orchestrator (commands/review.md)**

Simplified version of the CLI review.md:

- **Input:** User pastes a diff, provides a PR URL (fetched via web), or points to a local repo folder
- **Context loading:** If workspace folder is a git repo → blame, churn, log all work. If not → skip gracefully
- **Agent launch:** Same 4 agents + verifier. No Codex (not available in sandbox)
- **Pattern storage:**
  - If Confluence connected → store patterns as Confluence pages via `createConfluencePage` / `updateConfluencePage` MCP tools
  - If not → store locally in workspace `.air/` directory
  - If `~~source control` connected (future GitHub MCP) → post to PR
- **Output:** Findings printed to conversation. If `~~source control` connected, offer to post as PR comment

**2.3 — Graceful Degradation Matrix**

```markdown
# CONNECTORS.md

| Capability | No connectors | ~~source control | ~~knowledge base |
|---|---|---|---|
| Fetch PR diff | Paste diff manually | Auto-fetch from PR URL | — |
| Post review comment | Copy from conversation | Auto-post to PR | — |
| Store patterns | Local .air/ directory | — | Confluence pages |
| Notify team | — | — | — |
| Fetch PR metadata | Manual input | Auto-fetch | — |
```

**2.4 — Plugin Distribution**

Two options:
- **Marketplace:** Add to the existing `VorobiovD/air` marketplace. Users install via `Customize > Plugins > Marketplace`
- **Org-managed:** For enterprise, publish to a private GitHub repo. Org admins enable via `Admin > Plugins`

### Cost Estimate (Cowork Plugin)

Same token cost as CLI (~$1.66/review). No session-hour fee. Included in Team/Pro subscription seat.

---

## Phase 3: Team Rollout and Sharing

**Timeline:** 1 week after Phase 1 is working  
**Impact:** Multiplier — goes from "Dmytro's tool" to "team infrastructure"

### 3.1 — Packaging for Other Teams

**For teams using GitHub Actions (most teams):**

Provide a reusable GitHub Action that any repo can add in 3 lines:

```yaml
# In any team's repo: .github/workflows/air-review.yml
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

The reusable workflow handles agent/environment creation, session management, and cleanup. Teams don't need to understand Managed Agents internals.

**For teams using GitLab CI:**

Same pattern with a reusable `.gitlab-ci.yml` include. The `platform-gitlab.md` reference document already has all the CLI/API mappings.

**For non-developer teams (via Cowork):**

Install the Cowork plugin from the marketplace. No setup beyond connecting Confluence for pattern persistence.

### 3.2 — Shared vs Per-Repo Configuration

```
Organization level (shared):
├── Agent definition (air-reviewer)     ← one agent, all repos use it
├── Environment definition (air-env)    ← one environment template
└── Anthropic API key                   ← org-level secret

Repository level (per-repo):
├── CLAUDE.md                           ← repo-specific conventions
├── Wiki patterns                       ← repo-specific learned patterns
│   ├── REVIEW.md
│   ├── PROJECT-PROFILE.md
│   ├── ACCEPTED-PATTERNS.md
│   ├── SEVERITY-CALIBRATION.md
│   └── GLOSSARY.md
└── .github/workflows/air-review.yml    ← 3-line workflow file
```

The agent definition and environment are shared across the org. Each repo gets its own wiki patterns (already how air works — patterns are per-repo). Teams opt in by adding the workflow file.

### 3.3 — Onboarding a New Team

Step-by-step for a team lead:

1. **Add the workflow file** — copy the 3-line YAML into their repo's `.github/workflows/`
2. **Set the API key** — add `ANTHROPIC_API_KEY` as a repo or org secret
3. **First PR** — air runs, detects no wiki, generates PROJECT-PROFILE.md and GLOSSARY.md automatically (Step 3.5 first-run discovery)
4. **Done** — patterns accumulate from every review, auto-cleanup triggers every 5 reviews

No installation. No CLI setup. No auth configuration for individual developers. The team lead adds one file and one secret.

### 3.4 — Visibility and Adoption Tracking

**Slack integration (you already have it connected):**

Add an optional Slack notification at the end of each review:

```
air reviewed PR #123 on myorg/myrepo
├── 2 blockers, 3 medium, 1 low
├── Security audit: 7/7 PASS
└── View: https://github.com/myorg/myrepo/pull/123
```

This goes to a `#code-reviews` channel and gives leadership visibility into adoption and finding patterns across the org.

**Confluence dashboard (you already have it connected):**

Weekly auto-generated page summarizing:
- PRs reviewed per repo
- Finding frequency across the org
- Top recurring patterns
- Author trends (anonymized for org-wide view)

This could be a scheduled Cowork task that pulls from REVIEW-HISTORY.md across repos.

### 3.5 — Enterprise Controls

For org admins who need governance:

| Control | How |
|---|---|
| Which repos have air enabled | Audit `air-review.yml` across repos via GitHub search |
| Cost visibility | Anthropic API dashboard shows per-session costs |
| Pattern quality | `/air:learn --dry-run` previews wiki state without pushing |
| Disable for a repo | Remove the workflow file or add `if: false` condition |
| Custom security checklist | Edit PROJECT-PROFILE.md in the repo's wiki |
| Override confidence threshold | Set in SEVERITY-CALIBRATION.md (auto-managed) |

---

## Implementation Sequence

```
Week 1-2: Phase 1a — Managed Agent prototype
  ├── Adapt review.md into Managed Agent system prompt
  ├── Create agent + environment definitions
  ├── Test with a real PR on VorobiovD/air repo
  └── Validate: wiki push, PR comment, pattern learning all work

Week 2-3: Phase 1b — GitHub Action integration
  ├── Build reusable workflow
  ├── Test on 2-3 repos
  ├── Handle edge cases (large PRs, cross-repo, GitLab)
  └── Document the 3-line setup for teams

Week 2-3 (parallel): Phase 2 — Cowork plugin
  ├── Create air-cowork plugin structure
  ├── Adapt orchestrator for paste-diff + Confluence storage
  ├── Test standalone and with Confluence connected
  └── Publish to marketplace

Week 4: Phase 3 — Team rollout
  ├── Onboard 2-3 teams
  ├── Set up Slack notifications
  ├── Collect feedback, tune confidence thresholds
  └── Write internal announcement / docs
```

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Managed Agents beta instability | Medium | High | Keep CLI plugin as fallback. Session failures → retry once |
| Token costs surprise at scale | Low | Medium | $1.69/review is predictable. Set org-level spend limits |
| Wiki push fails from sandbox | Low | High | Test in Phase 1a. Fallback: repo files via GitHub API |
| Cowork sandbox blocks git/network | Medium | Medium | Standalone mode (paste diff) always works |
| Teams resist adding workflow file | Low | Low | Zero-config: one file, one secret, 2 minutes |
| GitHub MCP connector never ships | High | Low | Managed Agent path doesn't need it. Cowork degrades gracefully |
| Multi-agent not available in Managed Agents | Medium | Medium | Sequential review passes in single session (slightly slower, same quality) |

---

## Decision: What to Build First

**Recommendation: Phase 1 (Managed Agent) first.**

It delivers the highest value (fully automated, zero-friction adoption for teams), solves the hardest technical problems (auth, wiki, long-running execution), and uses the newest Anthropic infrastructure that's specifically designed for this use case. The Cowork plugin is a nice-to-have for manual reviews but doesn't change the team's workflow the way automated reviews on every PR do.

Phase 2 (Cowork) runs in parallel and is lower effort since it's a simplified version of what already exists.

Phase 3 (rollout) is mostly documentation and communication — the technical work is in Phases 1 and 2.
