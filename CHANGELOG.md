# Changelog

## [1.1.0] - 2026-04-09

### Added
- GitLab support — auto-detected from git remote URL, including self-hosted instances
- `glab` CLI integration alongside `gh` with complete command/field/URL mappings
- Platform reference document (`platform-gitlab.md`) for all GitLab-specific translations
- `AIR_PLATFORM` environment variable override for non-standard GitLab domains
- Cross-platform cross-repo reviews (review a GitLab MR from a GitHub repo and vice versa)
- Test coverage analysis in code-reviewer agent
- Deeper project discovery — traces architecture, maps entry points, documents test infrastructure
- `--respond` flag for automated developer responses to review findings
- Pipeline guards: "DO NOT edit files" between Steps 7-12, mandatory verification in Step 8
- Own-PR guard moved to start of Step 12 with platform-neutral field names
- Safe API response parsing (pipe to parser, not shell variables)

### Fixed
- Wiki clone on first run — separated clone from file copies so empty wikis don't report "not found"
- `--full` flag: argument-hint, skip target clarification, --fix guard, wording
- Review command renamed from `/air:review-pr` to `/air:review`

## [1.0.0] - 2026-04-08

### Added
- 5 specialized review agents: code-reviewer, security-auditor, simplify, git-history-reviewer, review-verifier
- Optional Codex (GPT-5.4) second-opinion reviewer
- 13-step PR review pipeline with batched API calls
- Wiki-backed pattern learning (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, GLOSSARY.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md)
- Re-review mode with FIXED/NOT FIXED tracking and graduated dispute resistance
- Self-review mode with fix plan generation and auto-apply
- Full codebase review mode (`--full`) for first-time audits
- Respond mode (`--respond`) for automated developer responses to reviews
- Cross-repo review support
- Auto-trigger wiki cleanup every 5 reviews or 2 days
- First-run project discovery (PROJECT-PROFILE.md + GLOSSARY.md generation)
- Security audit with 31-item checklist, tailored per project
- Verification agent filtering false positives with confidence scoring
