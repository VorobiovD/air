#!/usr/bin/env bash
# rotate-air-pat.sh — push your freshly-created PAT to every air-review repo at once.
#
# Corporate policy caps PATs at 7 days, so each reviewer rotates weekly. GitHub
# makes you *create* the fine-grained PAT in the UI (can't be automated), but
# *distributing* it to all the repos is one command — this script. A missed repo
# keeps a stale (existing-but-expired) PAT and its next review fails auth
# silently, which is why partial rotation exits non-zero.
#
# One-time setup:
#   1. gh auth login
#   2. cp scripts/rotate-air-pat.sh ~/.local/bin/air-rotate && chmod +x ~/.local/bin/air-rotate
#      (no ~/.local/bin on PATH? use ~/bin, or: alias air-rotate='/abs/path/rotate-air-pat.sh')
#   3. Create the PAT (classic, scope `repo`, Expiration → 7 days):
#      https://github.com/settings/tokens/new?scopes=repo&description=air-review
#   4. Authorize SSO (required to reach the thecvlb repos):
#      https://github.com/settings/tokens → Configure SSO next to air-review → Authorize thecvlb.
#
# Each week:
#   1. Regenerate: https://github.com/settings/tokens → air-review → Regenerate token, 7 days, copy.
#   2. Re-authorize SSO — it does NOT survive a regenerate: same token → Configure SSO → Authorize.
#      (Forget this and the token still authenticates but can't reach thecvlb —
#      the preflight below catches it before any secret is overwritten.)
#   3. Run:  air-rotate                  # auto-detects your <STEM>_PAT from your gh login
#            air-rotate CHRISTINA_PAT    # …or pass it explicitly (ai-relay-only reviewers)
#            air-rotate AIR_BOT_TOKEN VorobiovD/air   # bot-PAT flow: explicit name + repos
#      Paste the PAT at the prompt, press Enter — four ✓ and done.
#
# The PAT is read with `read -rs` (single line, silent): it never appears in
# argv/shell history AND is not echoed to the screen as you paste it.
# This is the multi-reviewer model's per-repo secret: <LOGIN>_PAT in each repo,
# each owned by that reviewer. (Corporate PATs never go in the personal/public
# air repo — those repos hold only the reusable workflow code.)
set -euo pipefail

# Secret name: pass it explicitly, or omit to auto-detect from your gh login via
# the AIR_PAT_MAP allowlist on svc-transcribe (caguilaron→CARLOS_PAT, VorobiovD→
# DIMA_PAT, …). Uses gh's built-in jq, so no external jq needed for this part.
if [ -n "${1:-}" ]; then
  SECRET="$1"
  shift
else
  LOGIN=$(gh api user --jq '.login' 2>/dev/null) \
    || { echo "gh not authenticated — run 'gh auth login', or pass your secret name: rotate-air-pat.sh CARLOS_PAT" >&2; exit 1; }
  STEM=$(gh api "repos/thecvlb/svc-transcribe/actions/variables/AIR_PAT_MAP" \
           --jq ".value | fromjson | .[\"${LOGIN}\"] // empty" 2>/dev/null || true)
  [ -n "$STEM" ] || { echo "couldn't auto-detect a PAT stem for '$LOGIN' in AIR_PAT_MAP — pass it explicitly, e.g. rotate-air-pat.sh CHRISTINA_PAT" >&2; exit 1; }
  SECRET="${STEM}_PAT"
  echo "Detected you as '$LOGIN' → rotating ${SECRET}" >&2
fi

# Repos whose air-review bot runs under per-reviewer PATs. Extra args (after the
# secret name) or AIR_FLEET override the list — the AIR_BOT_TOKEN flow passes
# the repos that hold the bot copy explicitly.
if [ $# -gt 0 ]; then
  REPOS=("$@")
else
  # shellcheck disable=SC2206 # intentional word-splitting of the env override
  REPOS=(${AIR_FLEET:-thecvlb/svc-transcribe thecvlb/qai-be thecvlb/qai-fe thecvlb/ai-relay})
fi

read -rsp "Paste new 7-day PAT for ${SECRET} (then press Enter): " PAT
printf '\n' >&2
[ -n "$PAT" ] || { echo "no PAT provided — aborting" >&2; exit 1; }

# Preflight the PASTED token before it overwrites anything: a mis-paste (wrong
# clipboard, truncated copy) must leave every repo on the old, still-working
# PAT — not get distributed and fail auth at the next review. Also surfaces
# whose token it is, catching the wrong-account paste.
PAT_LOGIN=$(GH_TOKEN="$PAT" gh api user --jq '.login' 2>/dev/null) \
  || { echo "new PAT failed GET /user preflight — nothing updated" >&2; exit 1; }
# Second preflight, the weekly gotcha: SSO authorization does NOT survive a
# token regenerate, and an un-authorized token still passes GET /user — it
# just can't see the org's repos. Verify real access against the first fleet
# repo before distributing.
GH_TOKEN="$PAT" gh api "repos/${REPOS[0]}" --jq .full_name >/dev/null 2>&1 || {
  echo "new PAT authenticates as '${PAT_LOGIN}' but cannot reach ${REPOS[0]} — " >&2
  echo "almost certainly SSO not (re-)authorized: https://github.com/settings/tokens" >&2
  echo "→ Configure SSO next to the token → Authorize, then re-run. Nothing updated." >&2
  exit 1
}
echo "new PAT authenticates as '${PAT_LOGIN}' (org access ✓) → updating ${SECRET} on: ${REPOS[*]}" >&2

ok=0
for r in "${REPOS[@]}"; do
  # Capture stderr (don't discard it) so a real failure — auth expiry, network,
  # org policy blocking the secret name — is shown, not guessed at.
  if err=$(printf '%s' "$PAT" | gh secret set "$SECRET" --repo "$r" 2>&1); then
    echo "  ✓ $r"
    ok=$((ok + 1))
  else
    echo "  ✗ $r — ${err:-unknown error}" >&2
  fi
done
unset PAT
echo "Rotated ${SECRET} across ${ok}/${#REPOS[@]} repos."
# A missed repo keeps a stale (existing-but-expired) PAT — and the workflow's
# `|| AIR_BOT_TOKEN` fallback does NOT fire on an existing secret, so that repo's
# next review fails auth silently. Make a partial rotation a loud non-zero exit.
[ "$ok" -eq "${#REPOS[@]}" ] || { echo "WARNING: partial rotation — fix the repos above and re-run." >&2; exit 1; }
