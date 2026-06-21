# air Managed Agent

Automated code review on every PR — zero human trigger needed.

## Setup (per org, one-time)

1. Create a GitHub bot account (e.g., `air-reviewer-bot`)
2. Add it as collaborator (Write) to your repos
3. Generate a classic PAT on the bot account with `repo` scope
   (fine-grained PATs don't support wiki push)
4. Add two org secrets:
   - `ANTHROPIC_API_KEY` — your Anthropic API key with Managed Agents access
   - `AIR_BOT_TOKEN` — the bot's PAT
5. Optional: `OPENAI_API_KEY` — adds Codex as a 5th reviewer. Skipped cleanly if unset.

## Enable on a repo

Add one file — pick a trigger variant by cost/latency preference. Measured cost is ~$5–9 per review session (heavy PRs $15–30), so the trigger model is the single biggest cost decision.

**Variant A — request-driven (recommended):** first review fires when the PR opens or leaves draft; re-reviews fire only when the bot is requested as a reviewer. `/air:review --respond` re-requests the bot automatically after pushing fixes, so re-reviews arrive when the developer declares fixes done — not on every push. Zero added latency, no wasted burst reviews.

```yaml
# .github/workflows/air-review.yml
name: air review
on:
  pull_request:
    types: [opened, ready_for_review, review_requested]
  workflow_dispatch:
    inputs:
      pr_number:
        description: 'PR number to review (works on closed/merged PRs)'
        required: true
        type: string
      closed:
        description: 'Allow review of closed/merged PR'
        required: false
        type: string
        default: 'true'

jobs:
  review:
    # On review_requested, fire only for the bot account — requesting a
    # human reviewer must not burn a paid review. Replace `air-machine`
    # with your bot's login.
    if: ${{ github.event.action != 'review_requested' || github.event.requested_reviewer.login == 'air-machine' }}
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    with:
      pr_number: ${{ inputs.pr_number }}
      closed: ${{ inputs.closed }}
      # Pin the blessed agent set from an air release's notes (recommended
      # for work repos — bump deliberately instead of riding main; omit to
      # float on latest). Pin the WHOLE set from one release.
      # agent_versions: '{"air-code-reviewer": N, "air-simplify": N, "air-security-auditor": N, "air-git-history-reviewer": N, "air-ui-copy-reviewer": N, "air-review-verifier": N, "air-coordinator": N}'  # N = versions from the release notes
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      AIR_BOT_TOKEN: ${{ secrets.AIR_BOT_TOKEN }}
```

**Variant B — push-driven:** every push to an open PR re-reviews. Burst pushes are coalesced by the `cooldown_minutes` debounce (default 20): a push landing inside the window sleeps out the remainder at $0 before any session starts, and a newer push cancels the sleeper. First reviews and manual dispatches are never delayed; a *solo* push inside the window waits out the remainder.

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]
  # Paste the workflow_dispatch block from Variant A here for on-demand runs.

jobs:
  review:
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    with:
      pr_number: ${{ inputs.pr_number }}
      closed: ${{ inputs.closed }}
      # cooldown_minutes: '20'   # default; '0' disables the debounce
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      AIR_BOT_TOKEN: ${{ secrets.AIR_BOT_TOKEN }}
```

**Variant C — multi-reviewer (advanced, optional):** most teams should stop at Variant A or B — one dedicated bot account, one `AIR_BOT_TOKEN`. This variant exists for teams that want reviews attributed to the individual requested reviewer: the review posts under whichever teammate was requested, using *their* PAT. air's contract is unchanged — it still receives exactly one `AIR_BOT_TOKEN` and derives the identity from it at runtime. Selection happens entirely caller-side: a `resolve` job maps the requested login → a friendly secret **stem** via one repo variable `AIR_PAT_MAP`, and only that one PAT is passed (no `secrets: inherit`).

```yaml
name: air review
on:
  pull_request:
    types: [review_requested]
  workflow_dispatch:
    inputs:
      pr_number:
        description: 'PR number to review'
        required: true
        type: string
      reviewer:
        description: 'GitHub login whose PAT posts the review (workflow_dispatch only)'
        required: true
        type: string
      closed:
        description: 'Allow review of closed/merged PR'
        required: false
        type: string
        default: 'true'

jobs:
  # Map the requested reviewer's login -> friendly PAT stem via the
  # AIR_PAT_MAP repo variable. Keys = the allowlist; an unmapped login
  # yields an empty stem -> the review job is skipped (safe by default,
  # so merging this file changes nothing until the variable + secrets exist).
  resolve:
    runs-on: ubuntu-latest
    outputs:
      stem: ${{ steps.map.outputs.stem }}
      login: ${{ steps.map.outputs.login }}   # for the optional expected_reviewer assert
    steps:
      - id: map
        env:
          LOGIN: ${{ github.event.requested_reviewer.login || inputs.reviewer }}
          MAP: ${{ vars.AIR_PAT_MAP }}
        run: |
          # Keep verbatim. Do NOT rewrite as ${MAP:-{}} — bash mis-parses
          # the nested braces and jq errors. This guard form is correct.
          [ -n "$MAP" ] || MAP='{}'
          STEM=$(printf '%s' "$MAP" | jq -r --arg k "$LOGIN" '.[$k] // empty')
          echo "stem=$STEM" >> "$GITHUB_OUTPUT"
          echo "login=$LOGIN" >> "$GITHUB_OUTPUT"

  review:
    needs: resolve
    if: ${{ needs.resolve.outputs.stem != '' }}
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    with:
      pr_number: ${{ inputs.pr_number }}   # empty on review_requested -> falls back to the PR event
      closed: ${{ inputs.closed }}
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      # The needs.* context IS available in secrets:; the requester's login is
      # never an input here, so hyphenated/dotted logins can't break a secret name.
      AIR_BOT_TOKEN: ${{ secrets[format('{0}_PAT', needs.resolve.outputs.stem)] }}
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}   # optional
```

Setup for Variant C:

```bash
# 1. The allowlist + login->stem map (keys = logins, values = friendly stems):
gh variable set AIR_PAT_MAP --repo <owner>/<repo> \
  --body '{"alice":"ALICE","bob-smith":"BOB"}'

# 2. Each reviewer sets their own per-repo secret <STEM>_PAT (ALICE_PAT, BOB_PAT, ...)
#    = a PAT scoped to the repos (Pull requests: RW, Contents: RO, Checks: RW).
#    If your org caps PAT lifetimes, each reviewer refreshes their own secret
#    on that cadence — on EVERY repo that holds a copy (a repo left on the old
#    PAT fails auth silently at its next review).
```

**Why a stem map (not bare `<LOGIN>_PAT`):** GHA expressions have no `upper()` and secret names allow only `[A-Za-z0-9_]`, so a raw login like `bob-smith` can't be a secret name and `alice` won't match `ALICE_PAT`. The `resolve` job decouples the login from the secret name and keeps the lookup off the unambiguous `needs` context.

**Behavioral note:** air keys prior-review detection, the pre-post dedup, and the re-review FIXED/NOT-FIXED delta on the token owner's login. A review posted under one reviewer's identity is *not* seen as "prior" by a run under a different reviewer's token on the same PR — that run posts a **fresh** review, not a delta. This is intentional (each requested reviewer keeps an independent thread); the cooldown debounce is any-author, so burst-coalescing still works across reviewers.

**Cross-account gate-orphan dismissal (v1.32.3, automatic):** GitHub's `reviewDecision` blocks on ANY reviewer account whose *latest* review is `CHANGES_REQUESTED`, so a fresh APPROVE under reviewer B would otherwise leave a stale block from reviewer A still gating the PR — a correct APPROVE that silently fails to un-gate. air now stamps every verdict body with an invisible sentinel (`<!-- air-review-verdict -->`, hidden in the GitHub UI) and, after each verdict, dismisses prior `CHANGES_REQUESTED` reviews it owns under *other* accounts (same-account is auto-superseded by GitHub). It recognizes its own verdicts by the sentinel — **zero false-positives, only air writes it** — so a human's `CHANGES_REQUESTED` is never dismissed. Best-effort: a missing dismiss permission logs a `[warn]` and is non-fatal. **Optional legacy coverage:** to also clear orphans posted *before* the sentinel shipped, expose the rotating bot logins to the review environment as a comma-separated `AIR_BOT_LOGINS` (the `AIR_PAT_MAP` keys are also honored when present in env); those accounts' stale blocks are then dismissed by identity. Managed-only — the CLI posts under a single account, so the orphan can't occur there.

**Optional hardening (`expected_reviewer`, available):** to fail loud on a mis-pasted PAT, pass the optional `expected_reviewer` input — air resolves the `AIR_BOT_TOKEN` owner (`GET /user`) before any review spend and exits with `::error::` unless it equals this login (case-insensitive). Pass the **login**, not the stem (the resolve job already outputs it):

```yaml
  review:
    needs: resolve
    if: ${{ needs.resolve.outputs.stem != '' }}
    uses: VorobiovD/air/.github/workflows/managed-review.yml@main
    with:
      pr_number: ${{ inputs.pr_number }}
      closed: ${{ inputs.closed }}
      expected_reviewer: ${{ needs.resolve.outputs.login }}   # opt-in identity assert
    secrets: # ... (unchanged)
```

Empty/absent → byte-for-byte today's behavior, so legacy single-token and SHA-pinned callers are unaffected. (Local/manual runs: `export AIR_EXPECTED_REVIEWER=<login>` before `python review.py`.)

First PR auto-bootstraps the agents. Subsequent PRs reuse them.

**Blessed agent sets:** to capture the set for a release, list the current versions after a green run on that release and paste the JSON into the GitHub Release notes:

```bash
curl -s https://api.anthropic.com/v1/agents?limit=100 \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01-research-preview" |
  jq -c '[.data[] | select(.archived_at == null and (.name | startswith("air-")) and .name != "air-learner")] | map({(.name): .version}) | add'
```

`air-learner` is not pinnable (learn always tracks the latest prompt).

**Migration note — adding a specialist (e.g. `air-ui-copy-reviewer`, v1.27.0):** when a new specialist joins the pinnable set, a caller that pins `agent_versions` with the coordinator pinned will fail `setup.py` with `air-coordinator is pinned but ['air-<new>'] are not` on its next run. To adopt: float once (drop `agent_versions`) so the agent is created, then add its version to the blessed set. Callers that float `@main` with no `agent_versions` (the default) are unaffected.

The `workflow_dispatch` trigger lets you review any PR on-demand from the Actions tab — including closed or merged PRs (post-merge audits, wiki-pattern backfills from history). For `pull_request` triggers, `pr_number` / `closed` defaults apply (current PR, state gate enforced).

### Review mode (`review_mode` — opt-in single-agent path)

Optional `review_mode` input (default `full`) selects the review architecture:

- **`full`** (default) — the 6-agent coordinator. Byte-identical to leaving it unset.
- **`solo`** — ONE `air-solo-reviewer` agent applies all 6 lenses + self-verifies + folds Codex in a single session (the UI/copy lens self-scopes on non-UI diffs). Benchmarked at ~$2–4 / ~7 min vs full's ~$10 / ~25 min (repo-A #994). Its prompt is assembled at sync from the 6 specialist prompts (zero-drift; no standalone file; the agent is created only when a run uses solo/both) and is not pinnable. **Solo posts the same `APPROVE`/`REQUEST_CHANGES` verdict as full** (it can gate/approve), but **⚠️ is NOT gate-safe** — a single agent downgrades blocker *severity* (it can APPROVE a PR whose real blocker it rated medium), so that verdict is not a trustworthy hard gate. Enable only where a single agent's verdict is acceptable. (Pattern learning: store-backed repos still strengthen author patterns post-review; legacy-wiki repos skip per-review reinforcement — only `/air:learn` cleanup runs.)
- CLI counterpart: `/air:review --solo` runs the same assembled prompt (shared `plugins/air/lib/solo_prompt.py`) as one local Fable agent on the user's subscription.
- **`both`** — runs full AND solo **concurrently** (wall-clock ≈ the slower of the two, not the sum). The **full** review gates as usual and drives the verdict/learn; the solo review posts alongside as a separate, non-blocking `## Code Review (solo — experimental)` comment for comparison (testing). A solo failure never affects the gating coordinator review.

```yaml
    # workflow_dispatch input, then pass it through:
    with:
      review_mode: ${{ inputs.review_mode }}   # 'full' | 'solo' | 'both'
```

`review_mode` is per-request (set it on a `workflow_dispatch` run, or pin it in a caller's `with:`) — **or persistently via a caller repo/org variable** `AIR_REVIEW_MODE` (Settings → Variables). `managed-review.yml` resolves `vars.AIR_REVIEW_MODE || inputs.review_mode || 'full'`, and `vars` in a reusable workflow reads the *caller's* variables — so to run `both` across a data-collection window, set `AIR_REVIEW_MODE=both` on the repo and delete it to revert (no workflow edit, immune to caller-file/mirror resets). The variable **wins over the input** (callers commonly pass `review_mode: ${{ inputs.review_mode || 'full' }}`, which would otherwise pin the input to `full` on `review_requested`); a one-off `workflow_dispatch` mode is overridden while the variable is set. An invalid value fails loud (review.py validates the mode). It's **managed-only** — the CLI `/air:review` runs its agents locally with no managed coordinator, so there is no CLI solo equivalent.

### Promote fast-path (`promote_fastpath` — opt-in cost saver for promote chains)

Optional `promote_fastpath` input (default `false`). Repos that ship via a `promote/staging-to-main-*` branch chain open a near-identical promote PR over and over; each one normally reviews from scratch as a full read. When enabled, a fresh promote PR with **no prior review of its own** is instead re-reviewed as a delta against its last-merged, already-reviewed sibling promote — **but only when the two overlap ≥80% of changed lines** (else it falls back to a full review). It reuses the whole re-review engine: inter-diff against the sibling's `Reviewed at:` SHA, carry-forward verifier, unfixed-blocker-only gating; the repo is still mounted read-only so specialists keep full-file context on unchanged lines.

Two ways to turn it on (either one being `true` enables it):

```yaml
    with:
      promote_fastpath: ${{ inputs.promote_fastpath }}   # 'true' | 'false' (default)
```

…or — **with no caller workflow change at all** — set a repository (or organization) **variable** `AIR_PROMOTE_FASTPATH=true` on the **caller** repo (Settings → Secrets and variables → Actions → Variables). `vars` in a reusable workflow resolves to the *caller's* repo + org variables, so the reusable `managed-review.yml` reads it directly. Flip the variable to `false` (or delete it) to disable instantly — no PR either way. An **org-level** variable is a single fleet-wide switch.

Conservative by construction — any uncertainty (no merged sibling, sibling never reviewed or missing a SHA footer, compare-API failure, <80% overlap) falls back to full review. Enable only on repos that use the `promote/staging-to-main-*` convention. Decision logs print `[promote] …` lines (chosen sibling #, overlap %, fired vs full) to the run log. **v1 limitation:** no periodic full-anchor re-read — a long chain rides re-reviews indefinitely; watch the logs and force a `--fresh` (or disable the flag) if drift accumulates. Backtested on the repo-A/repo-B Phase-4 promote chain at ~64% cost reduction with zero net-new-finding loss.

### Re-review severity-pin + deferred-findings ledger (`AIR_LEDGER_PIN` — default ON)

On a re-review the verifier re-judges every prior finding from scratch, which lets a prior `blocker` on code that **didn't change** drift back to `medium` (silently un-gating it), or a prior finding be dropped entirely. This guard makes severity carry-forward and finding-persistence a **hard, deterministic** guarantee — the shared gating contract in `plugins/air/lib/verdict.py` (so managed CI and the CLI enforce it identically). On by default; set the caller/org **variable** `AIR_LEDGER_PIN=0` (or `false`/`no`) to disable instantly with no workflow edit — it fails closed, so disabling only removes the guard, never breaks a review.

How it works: after the verifier responds, `review.py` builds `build_carry_forward_ledger(prior_body, inter_diff, base_sha)`, then `pin_and_resurrect(review_body, ledger)` rewrites the posted body before `should_request_changes` gates it. The spine is finding-NUMBER identity; line evidence is used ONLY where provably safe (the two-tier rule, validated on live fleet data):

- **Round 3+** (prior is a re-review) → pure number-identity (`INDETERMINATE`). Carried `### Previous Findings Status` lines have no anchor, and the only `**N.**` anchors belong to that round's NEW findings, whose numbers RESTART at 1 and collide with carried `#N`; a line-evidence join would cross-wire a carried blocker to an unrelated finding and could un-gate it (the bug the dogfood review caught).
- **Round-1 → round-2** (the most common re-review; prior is a *fresh* review) → **hunk-level line evidence**, which is safe here (a fresh body's `**N.**` findings are its only findings — no collision — and their anchors sit at round-1's SHA == the inter-diff OLD side). `extract_fresh_findings` enumerates them by number+section-severity, `extract_fresh_finding_locations` recovers anchors, and `finding_changed` marks a finding CHANGED iff its line falls in an edited hunk's old-side window. So a real fix (incl. additive/refactor — the flagged line itself need not be a `-` line) is HONORED, not over-gated. Live-fleet measured: pure-number-identity over-gated **~70%** of round-2 re-reviews (every genuine fix); hunk-level line evidence dropped it to **~10%** (only cross-region fixes — fix lands at a different line than the anchor — which fail safe), with **zero** un-gates.

Rules:

- A pinned finding keeps `max(prior, emitted)` severity — reverts a downgrade, preserves a genuine escalation, so the gate can only ever get **stricter**.
- A fake `FIXED` on a medium+ finding whose code did NOT change is rewritten to `NOT FIXED` (an explicit low/nit `FIXED` is trusted — never gates anyway). A `DEFERRED` is rewritten to `NOT FIXED` only when it's a `blocker` or its code changed; a non-blocker `DEFERRED` on unchanged code stays deferred (the carry-forward auto-defer). A `**`-bolded status (`— **FIXED**`) is tolerated; `DISPUTED`/`FALSE POSITIVE`/`PRE-EXISTING` stay valid evidence-bearing exits.
- Any prior `#N` silently absent from the emitted block is **resurrected** as `NOT FIXED` at its prior severity — a vanished blocker gates.

Decision logs print `[pin]`/`[ledger]` lines. The CLI mirrors this in `review.md` Step 11.5 (pipes the body through `verdict.py --pin` before Step 12's `--decide`). **Limits:** round 3+ pins by number, not line (over-conservative — a legit late fix on a *carried* finding stays `NOT FIXED` until cleared via `DISPUTED`/human override); round-2 line evidence is hunk-level, so a **cross-region fix** (the fix lands at a different line than the flagged anchor — e.g. a root-cause fix upstream of the symptom) reads UNCHANGED and over-gates (fails safe, disputable); a finding *introduced in round K* isn't pinned until it lands in a status block (per-round numbering re-uses 1.. for new findings, so cross-round anchor tracking for carried findings is v2). The honor threshold can be widened to file-level (clears the cross-region case at the cost of a narrower un-gate exposure) if the cross-region false-block rate proves high in monitoring.

### Fresh-gate exposure floor (`AIR_CATEGORY_FLOOR` — default ON)

The ledger above pins severity by carrying a *prior* finding forward — it has nothing to bite on for a **fresh** review. But a fresh review is exactly where a weaker model tier can find a real exposure and rate it `medium`, silently producing an APPROVE (the 2026-06-12 Sonnet bench: an org-wide PII leak and an RBAC bypass, both found, both rated `medium`, both would have un-gated). This floor closes that gap: the verifier tags each confirmed, genuinely-exploitable exposure with `[sec:<token>]` (one fixed blocker-class token — `pii-exposure` / `phi-exposure` / `authz-bypass` / `idor` / `sqli` / `leaked-credential` / …), and `verdict.count_category_floored` floors any such finding to a **blocker** for the gate regardless of the section the model put it in. The model classifies into a bucket (reliable across tiers); verdict.py assigns the gate severity (deterministic, ours). Same advisory→enforced move as `has_conflict_markers`.

On by default; set the caller/org **variable** `AIR_CATEGORY_FLOOR=0` (or `false`/`no`) to disable instantly with no workflow edit. It is **inert on any body without `[sec:...]` tags**, so disabling — or any review the verifier didn't tag — is byte-identical to the pre-floor gate. It can only ever make the gate *stricter* on a real exposure, never un-gate. **Single emission source:** the tag-emission rule lives in the verifier system prompt (`plugins/air/agents/review-verifier.md`) — the system prompt for the managed verifier agent, the CLI verifier, AND assembled into solo — so all three paths emit `[sec:<token>]` from one place (no per-task injection). The floor *application* is parity-wired into `verdict.py --decide`, so managed CI, CLI, and solo all gate identically. The markdown token list is locked to `verdict._BLOCKER_CATEGORIES` by `.air-checks.sh` Check F. Decision logs print the floored category list in the verdict reason.

### Diff hygiene & cost caps (managed-only)

Three knobs trim per-review context spend. All of them leave **visible markers** — nothing is dropped silently — and none of them changes gating:

- **Generated-file stubbing** (`github_client.apply_diff_hygiene`, applied to both the PR diff and re-review inter-diffs): minified bundles (`*.min.js/css`), sourcemaps, snapshots, `dist/`/`vendor/`/`node_modules/`/`__snapshots__/` segments, and **lockfiles whose same-directory manifest also changed** are replaced by a one-line `[air: <path>: N changed lines omitted (generated/vendored)]` marker. A **lockfile-only** change (resolver/integrity swap with no manifest touch — the supply-chain attack shape) is never stubbed; manifests outside vendored dirs always stay whole. (A lockfile-only diff larger than the byte cap below is still size-capped — it then gets a dedicated `[air: LOCKFILE … supply-chain review incomplete]` marker instead of folding into the generic count, so the security checklist can flag the gap.)
- **Size cap** — `AIR_DIFF_MAX_BYTES` (env, default 500000): greedy first-fit at file boundaries; the marker tail-truncates shown paths and shrinks until the result fits, so the cap holds marker-included for any budget above the ~80-byte path-less-marker floor. Omitted files are named in the marker + stderr. A truncated re-review delta **never** skips codex (it reads the git tree, not the diff).
- **Conversation tail-cap** — `CONVERSATION_MAX_ENTRIES` (review.py constant, 30): the `<pr-conversation>` block keeps the newest entries with a `<conv-truncated>` marker. **Codex skip** — `CODEX_RE_REVIEW_MIN_LINES` (constant, 20): re-review deltas under 20 changed lines skip the advisory codex leg with a decision-log line.

**CLI gap (by design):** `/air:review` is unchanged — its bash path fetches diffs via `gh pr diff` and uses `pr_conversation.py`'s default cap (100). The hygiene/caps live in the managed driver only.

### Multiagent workspace-handoff (`AIR_MULTIAGENT=1` — EXPERIMENTAL, off by default)

Runs full mode through `air-coordinator-ma`, a coordinator on the GA `multiagent` roster primitive whose sub-agent threads **share `/workspace`** (the research-preview `callable_agents` threads are isolated). Instead of re-emitting the PR context + diff into every specialist delegation (~60–150K output tokens/review — full mode's #1 structural cost), the coordinator writes them to `/workspace/context/` ONCE (TURN 0) and delegates short file pointers; specialists write findings to `/workspace/findings/` and the verifier reads them there. `air-git-history-reviewer` keeps an inline delegation (its model tier under-read file pointers in benchmarking). The MA agent is created by setup.py only when the flag is on, is not pinnable, and `AIR_FILE_HANDOFF` is ignored while the flag is set (Files-API mounts don't materialize on this runtime — probed). A/B complete (2026-06-11, 4 PRs across air + the work repos): $1.00–1.77/review vs $3.92–7.73 production inline (≈ −65–80%), wall ~9–14 min vs ~12–25, quality held.

**Enable per-repo with no workflow edit** — same caller-variable mechanism as `AIR_REVIEW_MODE`: set a repository (or org) **variable** `AIR_MULTIAGENT=1` on the caller repo (Settings → Secrets and variables → Actions → Variables); `managed-review.yml` passes it through to the driver. Delete the variable to roll back instantly. Roll out one repo at a time, dogfooding on the air repo first.

**Optional cheaper coordinator tier (`AIR_MA_COORDINATOR_MODEL=haiku`).** The MA coordinator only *delegates and relays the verifier's review verbatim* — it does not synthesize — so it can run on a cheaper/faster model. Set this caller variable (alongside `AIR_MULTIAGENT=1`) to route the coordinator to a separate `air-coordinator-ma-<alias>` agent (created by setup.py; the shared Sonnet coordinator is untouched, so other repos are unaffected). Benefit: ~15% lower wall-time and cheaper idle-wakes; **not a gate-safety change** (specialists + verifier are unchanged). Unset / `sonnet` / an unknown value falls back to the standard coordinator. Delete the variable to roll back. **Caveat (2026-06-20):** the relay-safe A/B used small reviews; in production a *large* review (repo-A #1243, 11-finding body) saw the Haiku coordinator drop 2 findings + renumber to 9 (reached the posted comment; low-sev, gate held). Relay-drop risk scales with review size. repo-A was rolled back; direct-post (below) is the durable fix.

**Direct-post — post the verifier body, not the coordinator relay (`AIR_POST_VERIFIER_BODY=1`, default OFF).** The coordinator only relays the verifier's review verbatim, so its relay turn is the only place a cheap coordinator can drop findings. When enabled (MA full/both only), `run_session` captures **all** `## Code Review` bodies delivered to the coordinator and `review.py:_select_verifier_body` posts the VERIFIER's verbatim (plus a synthesized `Reviewed at:` footer) instead of the relay. Selection is **safe by construction**: a candidate is used only if it carries a verifier-only section (Strengths/Pre-existing/Blockers), its finding titles cover ≥60% of the relayed findings, and it has ≥ as many findings as the relay — so a specialist's body (it also emits `## Code Review`) can never be posted, and the result is re-validated through the same SHA-checking extractor. Anything ambiguous falls back to the coordinator relay. Eliminates the relay-drop failure mode for any coordinator model (Haiku can then run everywhere). Decision log: `[direct]` (with the recovered-finding count). Measured on repo-A #1243: ~$5.5 / ~12 min on a Haiku coordinator vs $7.3 / ~26 min for a Sonnet-relay coordinator. v1 limit: a 0-finding relayed review isn't direct-posted (clean approve vs total drop are indistinguishable by coverage). Opt-in until fleet-soaked.

**Silent solo-improvisation guard.** A coordinator whose delegation capability is broken does not error — it reviews the PR alone and posts an unverified review that looks like a normal success. Two real triggers (both hit during an org workspace migration, 2026-06-11): (1) the delegation capability is *unnamed* in the agent toolset — it rides `default_config`, so a default-deny toolset with named allows (`bash`, `read`, …) disables it on runtimes that enforce config (`Permission to use create_agent has been denied`); setup.py therefore default-ENABLES and explicitly disables the named tools outside the frontmatter allowlist. (2) The `multiagent` roster only exists in the GA API dialect (`managed-agents-2026-04-01`): the research-preview update endpoint silently drops it, and the research-preview GET renders it as `null` even when set — so setup.py sends roster-carrying requests with the GA header and aborts the sync if the response comes back roster-less. Defense in depth at run time: `run_session(require_dispatch=True)` fails the review loudly if a coordinator session completes without ever opening a sub-agent thread (both runtimes), instead of posting the improvised output.

### UI / copy reviewer — covering CLI/TUI copy (`## User-Facing Copy Paths`)

`air-ui-copy-reviewer` dispatches whenever a PR's diff touches a **web** surface (`.tsx/.jsx/.vue/.svelte/.html`, i18n catalogs, user-facing docs) — automatically, no config. For **CLI/TUI products** whose user-facing copy lives in non-markup files (e.g. repo-C's Python patient/agent message modules), add a `## User-Facing Copy Paths` section to the repo's **PROJECT-PROFILE.md** listing glob patterns, one per `- ` line:

```markdown
## User-Facing Copy Paths
- agent-core/agents/*.py
- `**/messages/*.py`
```

The dispatch gate reads those globs from the store (`read_memory`) **only when the web check misses**, so web PRs and repos without the section pay nothing extra. Keep the globs **narrow** — they should match only the copy modules, so routine backend PRs still skip the reviewer ($0). `fnmatch` semantics (`*` is greedy across `/`, so `agent-core/agents/*.py` matches any depth). **Store-backed repos only** (legacy-wiki repos fall back to the web-only gate). The reviewer reviews **static** user-visible strings (display text, prompts, canned/template messages) — not runtime-generated agent output.

## How it works

**Multi-agent coordinator (v1.9.0+)**: the Python driver does upstream prep (fetch PR data, state gates, build context, optionally run codex), then hands off to a single `air-coordinator` session that dispatches the specialists in parallel + verifier as `callable_agents` sub-agents within one Anthropic session — mirroring the local CLI's architecture. This replaced v1.7's client-side `asyncio.gather` over 5 separate sessions once Anthropic granted research-preview access for `callable_agents` on 2026-04-25.

```
PR opened (or air-machine requested as reviewer)
  │
  ▼
GitHub Action triggers `python review.py <repo> <pr>`
  │
  ├── Syncs 6 specialist agents + air-coordinator + air-solo-reviewer (creates on first run, updates prompts otherwise)
  ├── Fetches PR metadata + diff via GitHub API
  ├── Fetches current PR conversation (issue comments + reviews + inline comments) and bot identity
  │     concurrently — humans + other AI bots are surfaced to specialists as <pr-conversation>
  │     so findings can flag overlap with [already raised by @<author>]
  ├── Optional: runs `codex review --base <sha>` as a subprocess (Pattern B: GHA-side, overlapping
  │     precomp, completes before the coordinator) — output html-escaped, length-capped, and
  │     bundled into the mounted verifier-task.md (inline fallback: coordinator user message)
  ├── File-handoff (v1.18.0, EXPERIMENTAL — AIR_FILE_HANDOFF=1, off by default): uploads PR
  │     context, diff, and verifier task via the Files API, mounted at /workspace/context/ with
  │     a short pointer note as the user message. Blocked on the research-preview runtime:
  │     sub-agent threads run in isolated containers (file mounts + cross-thread writes don't
  │     propagate) — inline is the production shape until that lands.
  │
  ▼
Single air-coordinator session (callable_agents multi-agent runtime)
  │
  ├── TURN 1: dispatches specialists in parallel as sub-agents (one Anthropic session; each
  │     thread runs in its OWN container — github_repository/wiki/memory mounts replicate per
  │     thread, but file resources and cross-thread filesystem writes do not propagate,
  │     verified 2026-06-03):
  │     ├── air-code-reviewer       — bugs, design, test coverage
  │     ├── air-simplify            — reuse, quality, efficiency
  │     ├── air-security-auditor    — 31-item checklist
  │     └── air-git-history-reviewer — blame, churn, recurring patterns
  │
  ├── TURN 2: dispatches air-review-verifier with the dispatched specialist findings + codex findings +
  │           the verifier_task template embedded (inline — the production shape; the experimental
  │           file-handoff pointer variant is blocked on thread isolation, see above). Verifies
  │           each finding, drops false positives, emits markdown
  │
  └── TURN 3: outputs verifier's response verbatim + bash tool call to update the wiki (REVIEW.md
              author-pattern entry on recurring findings, with one-shot rebase-retry on push)
  │
  ▼
Python driver posts the review comment to the PR via GitHub API,
then bumps the wiki-backed counter and runs the /air:learn epilogue
synchronously when the threshold fires.
```

**Wall-clock:** ~10-25 min depending on PR size (specialists run concurrently in the coordinator; wall ≈ slowest specialist + verifier + optional codex). Beta header `managed-agents-2026-04-01-research-preview` unlocks `callable_agents`.

## Manual trigger

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export AIR_BOT_TOKEN=ghp_...

pip install -r requirements.txt
python review.py myorg/myrepo 123             # post review comment
python review.py myorg/myrepo 123 --dry-run   # print comment, skip post
python review.py myorg/myrepo 123 --fresh     # force full review (ignore re-review auto-detect)
python review.py myorg/myrepo 123 --closed    # review a closed/merged PR (default refuses)
python review.py myorg/myrepo 123 --no-codex  # skip Codex even if OPENAI_API_KEY is set
```

## Agent updates

When agent prompts change in the air repo, the workflow auto-updates deployed agents on the next PR (compares and patches via API). No manual step needed — unless the caller pins via `agent_versions`, in which case pinned agents skip sync until the caller bumps the pin.

## Security

- **Repo clone**: authenticated via `github_repository` resource (token in API request, not conversation)
- **Wiki access from sessions**: each specialist session clones the wiki itself using `GH_TOKEN` injected via the user message — same token as the repo auth. Visible in Anthropic's session logs; mitigated by bot account with minimal permissions, classic PAT with `repo` scope only, rotatable.
- **Comment posting**: done client-side by `review.py` via GitHub API using `AIR_BOT_TOKEN` from the runner env — never sent to Anthropic.
- **Permissions**: `repo` scope on bot account (needed for wiki push — fine-grained PATs don't support wiki)
- **Agent access**: each org's agents are isolated under their own Anthropic API key

## Pattern memory store (pilot)

Migrated repos store review patterns in a per-repo Anthropic memory store instead of the git wiki (`migrate_wiki_to_store.py owner/repo [--dry-run]` to migrate; store discovered by name `air-patterns <owner>/<repo>` — its presence is the rollout flag). Review sessions mount it **read-only** (PR content is untrusted; deterministic post-review writes happen in `pattern_writer.py`), learn sessions mount read-write to curate it. The `/air:learn` counter lives at `/meta/air-meta.json` with sha256-preconditioned updates — no more wiki push races. Rollback: archive the store; the next run falls back to the wiki mount.

**Wiki mirror (`render_store_to_wiki.py`).** The git wiki is an exported mirror, rendered by a deterministic Python step (the inverse of the migrate split — `--dry-run` prints the rendered files + byte counts without pushing), NOT by the AI learn session. It runs throttled after each review (≤1×/hr — `meta.py mirror-due` is a cheap meta read; clone+push only when stale) and authoritatively after each `/air:learn` curation (always, resetting the throttle). The learn session pushes only REVIEW-HISTORY.md (not in the store); the renderer pushes everything else. Both call sites are best-effort and never fail the review/learn. Operator check: `python render_store_to_wiki.py owner/repo --dry-run` (needs the venv + `ANTHROPIC_API_KEY`).

## Cost

Claude-only (default), **measured** from real session usage (~340 review sessions, May–June 2026): median **~$5–9 per review**, heavy PRs $15–30. Learn epilogue sessions: ~$8–11 each on Opus (pre-v1.15.0; ~40% less on Sonnet). The dominant driver is cache-read volume (~5M cached tokens read per median review session; 30M on large PRs) — output tokens and the $0.08/session-hour runtime are minor. The fast-mode premium is not billed on Managed Agents sessions. Real May 2026 total at ~300 reviews + 130 learns across repos: ~$2.5–4K — push-triggered re-review density is the biggest cost lever, followed by learn cadence (cut ~3× in v1.15.0).

With Codex enabled (`OPENAI_API_KEY` set): +$1–2 per review depending on diff size and Codex's default model (gpt-5.4 at the time of writing). Opt-out with the `no_codex` workflow input or `--no-codex` on manual invocation.
