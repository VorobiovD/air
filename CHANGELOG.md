# Changelog

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
- Security audit with 28-item checklist, tailored per project
- Verification agent filtering false positives with confidence scoring
