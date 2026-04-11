# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**air** is a Claude Code plugin for automated PR code review, with an optional Managed Agent mode for CI automation.

The CLI plugin (`plugins/air/`) is markdown files and JSON metadata — no build system or dependencies. The Managed Agent (`managed/`) adds Python scripts for Anthropic's cloud API + a GitHub Actions workflow.

Two CLI commands: `/air:review` (13-step review pipeline) and `/air:learn` (wiki cleanup/regeneration). For CI: `managed/review.py` or the reusable GitHub Action.

## Project Structure

```
plugins/air/
├── agents/              # 5 review agent definitions (markdown prompts)
│   ├── code-reviewer.md
│   ├── security-auditor.md
│   ├── simplify.md
│   ├── git-history-reviewer.md
│   └── review-verifier.md
├── commands/            # CLI command orchestration
│   ├── review.md              # Main pipeline — the core of the plugin
│   ├── learn.md               # Wiki maintenance
│   └── platform-gitlab.md     # GitLab CLI/API/field mappings (reference, not a command)
└── .claude-plugin/
    └── plugin.json      # Plugin metadata (name, version, author)

.claude-plugin/
└── marketplace.json     # Marketplace distribution definition

managed/                          # Managed Agent (CI automation)
├── api.py                        # Shared API helpers
├── setup.py                      # Creates/updates agents + environment via API
├── review.py                     # Triggers review sessions
├── test-session.py               # Quick 9-test verification script
├── prompts/orchestrator.md       # System prompt for cloud orchestrator
├── requirements.txt              # Python dependencies
└── README.md

.github/workflows/
└── managed-review.yml            # Reusable GitHub Action for teams
```

## Architecture

**Review pipeline** (`commands/review.md`): Parses args, detects platform (GitHub/GitLab) from git remote, fetches PR/MR data via `gh` or `glab` CLI, runs 5 agents + optional Codex in parallel, passes results through a verification agent that filters false positives (confidence < 60 = dropped), then posts a consolidated comment. GitLab-specific command mappings are in `commands/platform-gitlab.md`.

**Agents** (`agents/*.md`): Stateless markdown prompt files. Each is a specialized reviewer personality that receives the same rich context block (PR diff, blame data, wiki patterns, project memory). All run on Opus.

**Verification** (`agents/review-verifier.md`): Post-review quality gate. Reads actual source at flagged lines, classifies findings as CONFIRMED/DOWNGRADED/IMPROVEMENT/PRE-EXISTING/ACCEPTED PATTERN/FALSE POSITIVE using git blame decision tree.

**Wiki storage**: Patterns learned from reviews are stored on the repo's wiki (GitHub or GitLab) (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md). Auto-cleanup every 5 reviews tracked in `~/.claude/review-learn-meta.json`.

## Development Workflow

- Edit agent files (`agents/*.md`) or command files (`commands/*.md`) directly
- Reload in Claude Code with `/reload` or reconnect
- Test with `/air:review <pr-number>` on a repo with PRs
- `--dry-run` flag prints to console without posting online
- After receiving a review, fix findings and run `/air:review --respond` to auto-classify, self-check, and reply

## Key Design Decisions

- **All agents run in parallel** — bottleneck is the slowest agent, not the sum
- **Batched API calls** — Step 4 uses 3 `gh` calls total (metadata, diff, commits), not one per field
- **Graduated dispute resistance** — security findings require compensating controls, style nits are readily accepted
- **Cross-repo reviews** skip local git data (blame, churn, wiki patterns) gracefully
- **Self-review mode** (`--self`) outputs a fix plan grouped by file; `--self --fix` auto-applies
- **Re-review mode** generates inter-diff from `REVIEWED_AT_SHA`, tracks FIXED/NOT FIXED per finding
- **Respond mode** (`--respond`) automates the developer side — classifies findings, verifies fixes match suggestions, self-checks for regressions, posts parseable response

## Conventions

- Agent prompts are human-readable instructions, not minified — edit freely
- Findings must score 60+ confidence from verifier to appear in output
- Conflict markers in PR diff = automatic blocker finding
- Security auditor uses a 31-item checklist; PROJECT-PROFILE.md controls which items apply per repo
- Version is in `plugins/air/.claude-plugin/plugin.json` (currently 1.3.0)
- Install via `/plugin marketplace add VorobiovD/air` then `/plugin install air@air`
