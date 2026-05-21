# Air — External Commitments

**Purpose:** Track requests/commitments from outside the air repo team that involve air. These are intentionally separated from the master roadmap (`docs/improvement-roadmap.md`) so the internal roadmap stays scoped to air-team work.

**Audience:** dima.v (air maintainer) + anyone deciding whether to ship an air change driven by external need.

_Last updated: 2026-05-21._

---

## C1 — Cowork plugin: air install-spec follow-through

**Requestor:** Christina Cephus (`@cephus`)
**Surfaced:** Slack DM, 2026-05-19
**Artifact:** `claude-sentinel#8` — draft `AIR_REVIEW_INSTALL_SPEC.md`

**Context:** Christina is building Cowork (paste-diff + Confluence integration) and asked "how that air review redesign is going". Has drafted an install spec referencing air's review pipeline. The original Cowork direction was sketched in `docs/legacy/air-expansion-plan.md §Phase 2` (Cowork paste-diff plugin built on air's foundation).

**Status:** Deferred in air's scope. The original air-expansion-plan flagged Cowork as Phase 2 work but it never made it into shipped phases. Currently NOT in the master roadmap because:
- Cowork is a separate plugin/product, not an air-side change
- Air's reusable workflow + agent prompts are already public; install-spec authoring is a Cowork-side task
- No air-side blocker; the unblock is documentation alignment

**What air owes:**
1. **Verify the install-spec accurately describes air's contract.** If `AIR_REVIEW_INSTALL_SPEC.md` references behaviors that have changed since v1.13.0 (or that didn't exist when Christina drafted it), correct them. Estimated: ~1 hour read-and-comment.
2. **Decide on supported integration shape.** Cowork could embed air via (a) calling air's reusable GHA workflow, (b) running air's CLI plugin locally with a paste-diff source, or (c) pulling specific agent prompts as building blocks. Each has different stability promises and breaking-change implications.
3. **Reply to Christina.** Slack DM has been outstanding since 2026-05-19. Acknowledge + give a realistic next-step ETA.

**Risk if dropped:** External commitment perception. Christina shipped Cowork in alpha and is now waiting on direction — silence reads as deprioritization.

**Decision needed:**
- [ ] Is air-side install-spec maintenance in scope for the air team, or does Cowork own it?
- [ ] Which integration shape (a/b/c above) does air officially support?
- [ ] Does Cowork get any reserved support window (best-effort vs SLA)?

---

## (template for future external commitments)

When adding a new entry:

```markdown
## C<N> — <Short title>

**Requestor:** <name + handle>
**Surfaced:** <date + channel>
**Artifact:** <link or filename>

**Context:** <what they asked for and why>

**Status:** <Active / Deferred / In-progress / Closed>

**What air owes:** <concrete next steps, with estimates>

**Risk if dropped:** <what breaks if we never get to this>

**Decision needed:**
- [ ] <decisions blocking the work>
```

---

*External commitments are tracked here so they don't pollute the internal roadmap. Closed items stay in this doc for audit.*
