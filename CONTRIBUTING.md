# Contributing to air

## How It Works

air is a Claude Code plugin — the entire codebase is markdown files and JSON metadata. There is no build system, no dependencies, and no test suite. "Code" means editing markdown prompts that an LLM executes.

## What You Can Contribute

### Agent improvements (`plugins/air/agents/*.md`)
- Better detection rules for specific languages or frameworks
- Reduced false positives in security checks
- New checks for the 31-item security checklist

### Pipeline improvements (`plugins/air/commands/review.md`)
- Bug fixes in shell commands or flow routing
- New flags or modes
- Performance improvements (fewer API calls, better parallelism)

### Documentation
- README improvements, usage examples, FAQ
- CLAUDE.md updates when the architecture changes

## Development Workflow

1. Fork the repo
2. Create a branch: `git checkout -b feat/your-change`
3. Edit the markdown files directly
4. Test manually: run `/air:review --dry-run` on a repo with PRs
5. Open a PR with a clear description of what changed and why

## Guidelines

- Agent prompts are human-readable instructions — keep them clear, not minified
- Shell commands must handle failures gracefully (`2>/dev/null`, `|| true`, explicit error checks)
- Every new flag needs: argument-hint entry, Step 1 definition, routing block, README usage example
- The two README files (root and `plugins/air/`) must stay in sync
- No hardcoded repo names, endpoints, or credentials in any file

## Testing

There is no automated test suite. Test manually:

```bash
/air:review 123 --dry-run     # Review a PR without posting
/air:review --self             # Self-review local changes
/air:review --respond          # Test respond flow after a review
/air:learn --dry-run              # Preview wiki cleanup
```

## Questions

Open an issue if something is unclear. Label it `question`.
