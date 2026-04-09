# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**air** is a Claude Code plugin for automated PR code review. It is **not** a compiled application — there is no build system, no test suite, no dependencies to install. The entire codebase is markdown files and JSON metadata.

Two commands: `/air:review-pr` (13-step review pipeline) and `/air:learn` (wiki cleanup/regeneration).

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
│   ├── review-pr.md     # Main pipeline — the core of the plugin
│   └── learn.md         # Wiki maintenance
└── .claude-plugin/
    └── plugin.json      # Plugin metadata (name, version, author)

.claude-plugin/
└── marketplace.json     # Marketplace distribution definition
```

## Architecture

**Review pipeline** (`commands/review-pr.md`): Parses args, fetches PR data via `gh` CLI, runs 5 agents + optional Codex in parallel, passes results through a verification agent that filters false positives (confidence < 60 = dropped), then posts a consolidated GitHub comment.

**Agents** (`agents/*.md`): Stateless markdown prompt files. Each is a specialized reviewer personality that receives the same rich context block (PR diff, blame data, wiki patterns, project memory). All run on Opus.

**Verification** (`agents/review-verifier.md`): Post-review quality gate. Reads actual source at flagged lines, classifies findings as CONFIRMED/DOWNGRADED/IMPROVEMENT/PRE-EXISTING/ACCEPTED PATTERN/FALSE POSITIVE using git blame decision tree.

**Wiki storage**: Patterns learned from reviews are stored on the repo's GitHub Wiki (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md). Auto-cleanup every 5 reviews tracked in `~/.claude/review-learn-meta.json`.

## Development Workflow

- Edit agent files (`agents/*.md`) or command files (`commands/*.md`) directly
- Reload in Claude Code with `/reload` or reconnect
- Test with `/air:review-pr <pr-number>` on a repo with PRs
- `--dry-run` flag prints to console without posting to GitHub
- After receiving a review, fix findings and run `/air:review-pr --respond` to auto-classify, self-check, and reply

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
- Security auditor uses a 28-item checklist; PROJECT-PROFILE.md controls which items apply per repo
- Version is in `plugins/air/.claude-plugin/plugin.json` (currently 1.0.0)
- Install via `/plugin marketplace add VorobiovD/air` then `/plugin install air@air`
