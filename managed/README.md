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
      # agent_versions: '{"air-code-reviewer": N, "air-simplify": N, "air-security-auditor": N, "air-git-history-reviewer": N, "air-review-verifier": N, "air-coordinator": N}'  # N = versions from the release notes
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

**Variant C — multi-reviewer (post under the requested reviewer's identity):** the review posts as whichever teammate was requested as reviewer, using *their* PAT. air's contract is unchanged — it still receives exactly one `AIR_BOT_TOKEN` and derives the identity from it at runtime. Selection happens entirely caller-side: a `resolve` job maps the requested login → a friendly secret **stem** via one repo variable `AIR_PAT_MAP`, and only that one PAT is passed (no `secrets: inherit`). Reference implementation: **thecvlb/svc-transcribe PR #88**.

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

# DEFERRED (match svc-transcribe): do NOT SHA-pin the air ref yet (#89) and
# do NOT add expected_reviewer yet (#90) — land them as additive follow-ups.

jobs:
  # Map the requested reviewer's login -> friendly PAT stem via the
  # AIR_PAT_MAP repo variable. Keys = the allowlist; an unmapped login
  # yields an empty stem -> the review job is skipped (safe by default,
  # so merging this file changes nothing until the variable + secrets exist).
  resolve:
    runs-on: ubuntu-latest
    outputs:
      stem: ${{ steps.map.outputs.stem }}
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
  --body '{"caguilaron":"CARLOS","adamdanielsnavarro":"ADAM","VorobiovD":"DIMA"}'

# 2. Each reviewer sets their own per-repo secret <STEM>_PAT (CARLOS_PAT, ADAM_PAT, ...)
#    = a fine-grained PAT (Pull requests: RW, Contents: RO, Checks: RW).
#    Corporate PATs are capped at 7-day expiry -> rotate weekly; per-repo only.
```

**Why a stem map (not bare `<LOGIN>_PAT`):** GHA expressions have no `upper()` and secret names allow only `[A-Za-z0-9_]`, so a raw login like `christinacephus-md` can't be a secret name and `caguilaron` won't match `CAGUILARON_PAT`. The `resolve` job decouples the login from the secret name and keeps the lookup off the unambiguous `needs` context.

**Behavioral note:** air keys prior-review detection, the pre-post dedup, and the re-review FIXED/NOT-FIXED delta on the token owner's login. A review posted under one reviewer's identity is *not* seen as "prior" by a run under a different reviewer's token on the same PR — that run posts a **fresh** review, not a delta. This is intentional (each requested reviewer keeps an independent thread); the cooldown debounce is any-author, so burst-coalescing still works across reviewers.

**Optional hardening (`expected_reviewer`, deferred):** when a caller wants to fail loud on a mis-pasted PAT, air can grow an optional `expected_reviewer` input that asserts the resolved token-owner login equals the requester's login (case-insensitive). The caller passes the **login** (not the stem). Empty/absent → byte-for-byte today's behavior, so legacy single-token and SHA-pinned callers are unaffected. Tracked alongside svc-transcribe #90; not yet shipped.

First PR auto-bootstraps the agents. Subsequent PRs reuse them.

**Blessed agent sets:** to capture the set for a release, list the current versions after a green run on that release and paste the JSON into the GitHub Release notes:

```bash
curl -s https://api.anthropic.com/v1/agents?limit=100 \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01-research-preview" |
  jq -c '[.data[] | select(.archived_at == null and (.name | startswith("air-")) and .name != "air-learner")] | map({(.name): .version}) | add'
```

`air-learner` is not pinnable (learn always tracks the latest prompt).

The `workflow_dispatch` trigger lets you review any PR on-demand from the Actions tab — including closed or merged PRs (post-merge audits, wiki-pattern backfills from history). For `pull_request` triggers, `pr_number` / `closed` defaults apply (current PR, state gate enforced).

## How it works

**Multi-agent coordinator (v1.9.0+)**: the Python driver does upstream prep (fetch PR data, state gates, build context, optionally run codex), then hands off to a single `air-coordinator` session that dispatches the specialists in parallel + verifier as `callable_agents` sub-agents within one Anthropic session — mirroring the local CLI's architecture. This replaced v1.7's client-side `asyncio.gather` over 5 separate sessions once Anthropic granted research-preview access for `callable_agents` on 2026-04-25.

```
PR opened (or air-machine requested as reviewer)
  │
  ▼
GitHub Action triggers `python review.py <repo> <pr>`
  │
  ├── Syncs 5 specialist agents + air-coordinator (creates on first run, updates prompts otherwise)
  ├── Fetches PR metadata + diff via GitHub API
  ├── Fetches current PR conversation (issue comments + reviews + inline comments) and bot identity
  │     concurrently — humans + other AI bots are surfaced to specialists as <pr-conversation>
  │     so findings can flag overlap with [already raised by @<author>]
  ├── Optional: runs `codex review --base <sha>` as a subprocess (Pattern B: GHA-side, sequential
  │     before the coordinator) — output html-escaped, length-capped, and bundled into the
  │     mounted verifier-task.md (inline fallback: coordinator user message)
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
  ├── TURN 2: dispatches air-review-verifier with the 4 specialist findings + codex findings +
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

Migrated repos store review patterns in a per-repo Anthropic memory store instead of the git wiki (`migrate_wiki_to_store.py owner/repo [--dry-run]` to migrate; store discovered by name `air-patterns <owner>/<repo>` — its presence is the rollout flag). Review sessions mount it **read-only** (PR content is untrusted; deterministic post-review writes happen in `pattern_writer.py`), learn sessions mount read-write and export a rendered mirror back to the git wiki for humans + CLI reads. The `/air:learn` counter lives at `/meta/air-meta.json` with sha256-preconditioned updates — no more wiki push races. Rollback: archive the store; the next run falls back to the wiki mount.

## Cost

Claude-only (default), **measured** from real session usage (~340 review sessions, May–June 2026): median **~$5–9 per review**, heavy PRs $15–30. Learn epilogue sessions: ~$8–11 each on Opus (pre-v1.15.0; ~40% less on Sonnet). The dominant driver is cache-read volume (~5M cached tokens read per median review session; 30M on large PRs) — output tokens and the $0.08/session-hour runtime are minor. The fast-mode premium is not billed on Managed Agents sessions. Real May 2026 total at ~300 reviews + 130 learns across repos: ~$2.5–4K — push-triggered re-review density is the biggest cost lever, followed by learn cadence (cut ~3× in v1.15.0).

With Codex enabled (`OPENAI_API_KEY` set): +$1–2 per review depending on diff size and Codex's default model (gpt-5.4 at the time of writing). Opt-out with the `no_codex` workflow input or `--no-codex` on manual invocation.
