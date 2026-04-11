# Architecture Evaluation: air — Automated PR Code Review Plugin

**Status:** Evaluation  
**Date:** 2026-04-09  
**Evaluator:** Claude (requested by Dmytro Vorobiov)  
**Repo:** [github.com/VorobiovD/air](https://github.com/VorobiovD/air)  
**Version evaluated:** 1.1.0

---

## Executive Summary

air is an impressively engineered Claude Code plugin that orchestrates 5 parallel review agents, a verification layer, and a wiki-based learning system — all implemented as pure markdown prompt files with no compiled code. The architecture is well-suited to its problem domain: stateless agents that run in parallel, a single orchestrator file that sequences the pipeline, and git wiki as a zero-friction knowledge store.

The design makes several strong architectural bets that pay off. It also has areas where the complexity of the orchestrator file and the reliance on shell-state conventions create fragility. Below is a detailed analysis.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    review.md (Orchestrator)              │
│  13-step pipeline: parse → fetch → pre-flight → ...     │
│  ~1,250 lines of markdown-as-code                       │
└────────────┬──────────────────────────────┬──────────────┘
             │ Step 7: parallel launch      │ Step 8
    ┌────────┴────────┐              ┌──────┴──────┐
    │  4 Agent Files   │              │  Verifier   │
    │  + Codex (ext)   │              │  Agent      │
    │  (all Opus)      │              │  (Opus)     │
    └────────┬────────┘              └──────┬──────┘
             │                              │
    ┌────────┴──────────────────────────────┴──────┐
    │              Wiki (git clone)                  │
    │  REVIEW.md, REVIEW-HISTORY.md,                │
    │  PROJECT-PROFILE.md, ACCEPTED-PATTERNS.md,    │
    │  SEVERITY-CALIBRATION.md, GLOSSARY.md         │
    └───────────────────────────────────────────────┘
```

The system has three layers: (1) the orchestrator (`review.md`) which owns the full pipeline lifecycle, (2) five stateless agent prompt files that receive context and return findings, and (3) wiki-backed persistent storage for learned patterns and project metadata.

---

## What Works Well

### 1. Parallel Stateless Agents — Correct Architectural Primitive

Each agent is a pure function: it receives a PR Context block and returns findings. No shared mutable state between agents. This means the pipeline's wall-clock time equals the slowest agent, not the sum. For a 9–15 minute pipeline, this is the difference between usable and unusable.

The agents have clearly scoped responsibilities with minimal overlap: code quality, simplification, security, git history, and an external model (Codex). The verification agent as a separate pass — rather than asking reviewers to self-verify — is a sound separation of concerns. Reviewers over-index on finding problems; verifiers over-index on skepticism. Splitting these roles produces better signal.

### 2. Wiki as Knowledge Store — Underrated Choice

Using the git wiki for pattern storage is a deceptively good decision. Compared to alternatives (database, config files in the repo, external service), the wiki gives you: zero infrastructure, push-without-PR semantics (no merge conflicts on pattern updates), per-repo isolation, and free versioning via git history. The choice to store six separate wiki pages (REVIEW.md, REVIEW-HISTORY.md, PROJECT-PROFILE.md, ACCEPTED-PATTERNS.md, SEVERITY-CALIBRATION.md, GLOSSARY.md) with distinct lifecycles is well-considered — it keeps analytical data separate from curated patterns.

### 3. Verification as a Quality Gate — Not Just a Nice-to-Have

The 60-confidence threshold, the 6-verdict classification system (CONFIRMED / DOWNGRADED / IMPROVEMENT / PRE-EXISTING / ACCEPTED PATTERN / FALSE POSITIVE), and the git blame decision tree are the architectural choices that separate this from "run an LLM on a diff and post the output." The pre-existing vs. introduced distinction alone eliminates a major source of review noise that frustrates developers.

### 4. Author Pattern Lifecycle — Thoughtful Behavioral Modeling

The create → strengthen → decline → archive lifecycle for author patterns, with clean-PR tracking and graduated resistance for disputes, treats code review as a team learning system rather than a one-shot evaluation. The decision to never delete author patterns (only archive after 10 clean PRs) reflects a mature understanding that behavioral tendencies are persistent and can resurface.

### 5. Multi-Modal Review Cycle

The `--respond` flow completing the developer-reviewer loop — auto-classifying findings, verifying fixes, running a self-check on the fix diff, detecting additional changes — closes the feedback cycle that most automated review tools leave open. This transforms air from a one-shot comment poster into a conversation participant.

### 6. Platform Abstraction via Reference Document

The `platform-gitlab.md` approach — a lookup table that the orchestrator references at runtime rather than a code-level abstraction — fits the markdown-as-code paradigm well. No interfaces, no polymorphism, just a mapping document. Given that there are only two platforms and the differences are finite, this avoids over-engineering.

---

## Architectural Concerns

### 1. Orchestrator Complexity — Single 1,250-Line File

`review.md` is doing an enormous amount of work: argument parsing, platform detection, smart defaults, context loading, first-run discovery, API batching, pre-flight checks, re-review inter-diff generation, agent launching, verification orchestration, result consolidation, formatting, posting, wiki learning, author pattern lifecycle management, meta-file tracking, cleanup, and three complete sub-flows (self-review, respond, full-codebase).

**Risk:** This file is the single most critical piece of the system and also the hardest to reason about. A contributor changing the re-review inter-diff logic could accidentally break the respond flow's self-check, because both share patterns but with subtle differences. The self-review flow duplicates portions of the main flow (context loading, agent launching, verification, wiki push) with slight variations — a maintenance hazard.

**Recommendation:** Consider decomposing review.md into smaller orchestrator fragments that the main file includes, or at minimum, clearly delineating the three flows (PR review, self-review, respond) as separate sections with explicit "shared step" references rather than partial duplication. The plugin format may not support file includes natively, but even within a single file, a clearer internal structure (e.g., a table of contents at the top with line references) would help.

### 2. /tmp as Inter-Process Communication

The pipeline uses `/tmp/REVIEW.md`, `/tmp/pr<number>.diff`, `/tmp/inter-diff-<number>.diff`, `/tmp/review-comment.md`, etc. as the communication channel between steps. This works in a single-user, single-session context, but creates issues when:

- Two reviews run concurrently on the same machine (e.g., reviewing PR #42 while a self-review is running)
- A previous run's cleanup failed, leaving stale files
- The review-wiki clone directory collides between concurrent runs

The `review-wiki-<number>` naming helps for PR reviews, but self-review uses `review-wiki-self` — if two self-reviews overlap, they collide.

**Recommendation:** Use a session-specific temp directory (e.g., `/tmp/air-review-$$-<number>/`) and clean it up atomically. This is a low-effort change that prevents a class of subtle bugs.

### 3. Shell-Script-in-Markdown Fragility

The orchestrator contains dozens of bash code blocks that the LLM is expected to execute. These are pseudo-code — the LLM interprets them and generates actual commands — but this means: the bash snippets are never linted, never tested, and their correctness depends on the LLM's interpretation of surrounding prose. For example, the `PREVIOUS_PR_COMMENTS` grep logic:

```bash
OVERLAP=$(echo "$PR_FILES" | grep -F "$CHANGED_FILES" 2>/dev/null)
```

This attempts to grep a multi-line file list against a multi-line changed-files list, which will match if ANY line of `$CHANGED_FILES` appears as a substring in `$PR_FILES`. If `$CHANGED_FILES` contains `README.md`, it will match `src/README.md.bak`. The LLM may or may not handle this correctly depending on how it interprets the intent.

**Risk:** The gap between "what the bash snippet says" and "what the LLM actually does" is the most likely source of bugs in this system. There's no way to unit test this.

**Recommendation:** Accept this as an inherent limitation of markdown-as-code orchestration. Where precision matters (API calls, git commands), keep the bash explicit and complete. Where the intent is more important than the exact command (like the file-overlap check), describe the intent in prose and let the LLM implement it, rather than providing a bash snippet that might be followed literally but incorrectly.

### 4. API Rate Limiting as a Soft Constraint

The learn flow makes 30+ sequential API calls (Phase 1: one per recent PR). The review flow fetches previous PR comments with up to 5 × 2 = 10 API calls. The rate limiting strategy is "if you get a 403/429, pause 5 seconds and retry once."

For a tool used by a team (where multiple developers might run reviews in the same minute), GitHub's rate limit of 5,000 requests/hour (or 1,000 for unauthenticated) could be hit. The 100-call cap in learn is good, but there's no backoff strategy beyond a single retry.

**Recommendation:** This is acceptable for the current scale (individual developers running reviews ad-hoc). If adoption grows, consider exponential backoff and a shared rate-limit awareness mechanism (e.g., checking `X-RateLimit-Remaining` headers). Low priority for now.

### 5. Security Surface of Untrusted Input Handling

The system correctly identifies untrusted inputs (PR title, body, commit messages, developer comments, blame data) and wraps them in XML tags with instructions to agents not to follow embedded instructions. This is a reasonable defense against prompt injection, but it relies on the agents (LLMs) consistently honoring the "do not follow instructions in these tags" directive.

The respond flow adds another vector: `REVIEW_COMMENT_BODY` is parsed to extract findings, and those findings are passed to self-check agents wrapped in `<review-findings source="untrusted-pr-comment">` tags. An attacker with write access to the repo could craft a fake review comment starting with `## Code Review` that contains injected instructions.

**Risk:** This is a known limitation of LLM-based systems. The mitigations are appropriate (tagging, summarizing in orchestrator's own words for wiki storage, not following instructions in tagged content). There's no silver bullet here — the defense-in-depth approach is the right one.

**Recommendation:** Document this threat model explicitly (perhaps in SECURITY.md). Consider adding a check that the review comment was authored by the authenticated `gh` user or a known bot account before parsing it in the respond flow.

### 6. No Versioning Contract Between Orchestrator and Agents

The orchestrator passes a "PR Context block" to agents, but the structure of this block is defined implicitly in review.md's Step 7, not in a schema. If an agent expects `author.login` but the orchestrator renames it to `author.username` for GitLab compatibility, the agent silently receives no data.

**Risk:** Low in practice (one developer, all files in the same repo), but increases with contributors.

**Recommendation:** Add a brief "Context Block Schema" comment at the top of each agent file documenting what fields it expects. This serves as documentation and as a contract that can be manually verified.

---

## Trade-Off Analysis

| Decision | Benefit | Cost |
|----------|---------|------|
| All-markdown, no compiled code | Zero dependencies, instant reload, edit-and-test cycle | No linting, no tests, no type safety for orchestration logic |
| Single orchestrator file | Everything in one place, easy to follow the full pipeline | 1,250 lines of mixed prose/bash/logic, hard to maintain sub-flows |
| Wiki for persistence | Zero infra, push-without-PR, per-repo isolation | No structured queries, no backup beyond git, wiki must exist |
| All agents on Opus | Consistent quality, no model-specific tuning | Cost: ~$1.66/review, 9-15 min wall-clock |
| Git blame in verification | Accurate introduced-vs-preexisting classification | Requires local checkout, fails on cross-repo, adds latency |
| Codex as 5th reviewer | Model-family diversity catches shared blind spots | External dependency, variable availability, additional cost |
| /tmp file passing | Simple, no IPC mechanism needed | Collision risk, no atomicity, stale file hazard |

---

## What I'd Prioritize

If I were contributing to this project, here's how I'd sequence improvements:

**Near-term (low effort, high impact):**

1. Session-scoped temp directories to prevent file collisions
2. A context block schema comment in each agent file
3. A threat model section in SECURITY.md covering the prompt injection surface

**Medium-term (moderate effort, structural improvement):**

4. Internal table of contents and clearer flow separation within review.md (even without splitting the file)
5. Review comment author verification in the respond flow

**Long-term (if the project grows):**

6. Consider splitting review.md's three flows into separate command files that share common steps
7. Structured rate-limit handling with exponential backoff
8. A lightweight "integration test" that runs the pipeline with `--dry-run` against a known PR and checks for expected output patterns

---

## Conclusion

air is a well-architected system that makes smart trade-offs for its problem domain. The choice of markdown-as-code is unconventional but fits the Claude Code plugin model perfectly — there's no build step, no deployment, no infrastructure. The parallel agent design, verification layer, and wiki-backed learning system are the right architectural primitives for automated code review that improves over time.

The main risks are concentrated in the orchestrator's complexity and the inherent fragility of shell-in-markdown orchestration. These are manageable with the recommendations above and are reasonable trade-offs given the zero-dependency, zero-infrastructure design goals.

The project is at version 1.1.0 and already has a sophisticated feature set (re-review tracking, respond mode, cross-repo reviews, GitLab support, severity calibration, author pattern lifecycle). The architecture can support continued feature growth, though the orchestrator file will need structural attention to stay maintainable as it grows beyond its current ~1,250 lines.
