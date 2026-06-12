#!/usr/bin/env bash
# Rotate the air bot PAT across the WHOLE fleet in one command.
#
# The PAT lives as a secret on the air repo AND on every caller repo that
# passes it into managed-review.yml. Updating only one copy is an ops
# outage waiting to happen: the other repos keep reviewing with the stale
# PAT until it expires and their runs start failing (corporate fine-grained
# PATs are commonly capped at 7-day expiry). This script preflights the new
# token, then fans the update out to every repo in one pass.
#
# Usage:
#   ./scripts/rotate-air-pat.sh                          # default fleet, AIR_BOT_TOKEN
#   ./scripts/rotate-air-pat.sh owner/r1 owner/r2        # explicit repo list
#   ./scripts/rotate-air-pat.sh --secret-name ADAM_PAT owner/r1
#                                                        # per-reviewer PAT variant
#   AIR_FLEET="o/r1 o/r2" ./scripts/rotate-air-pat.sh    # fleet override via env
#
# The token is read from stdin or an interactive hidden prompt — NEVER from
# argv (argv leaks into shell history and process listings).
set -euo pipefail

SECRET_NAME="AIR_BOT_TOKEN"
DEFAULT_FLEET="VorobiovD/air thecvlb/qai-be thecvlb/qai-fe thecvlb/ai-relay thecvlb/svc-transcribe"

REPOS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --secret-name) SECRET_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "error: unknown flag $1" >&2; exit 2 ;;
    *) REPOS+=("$1"); shift ;;
  esac
done
if [ ${#REPOS[@]} -eq 0 ]; then
  # shellcheck disable=SC2206 # intentional word-splitting of the repo list
  REPOS=(${AIR_FLEET:-$DEFAULT_FLEET})
fi

if [ -t 0 ]; then
  printf 'Paste the new PAT (input hidden): ' >&2
  read -rs TOKEN
  echo >&2
else
  TOKEN=$(cat)
fi
TOKEN=${TOKEN//[$'\r\n']/}
[ -n "$TOKEN" ] || { echo "error: empty token — nothing updated" >&2; exit 1; }

# Preflight: the token must authenticate BEFORE it overwrites anything.
# A mis-pasted token that fails here leaves every repo on the old (working)
# secret instead of bricking the fleet.
LOGIN=$(GH_TOKEN="$TOKEN" gh api user --jq .login 2>/dev/null) || {
  echo "error: token failed GET /user preflight — nothing updated" >&2
  exit 1
}
echo "token owner: $LOGIN" >&2
echo "updating secret $SECRET_NAME on: ${REPOS[*]}" >&2

status=0
for repo in "${REPOS[@]}"; do
  if printf '%s' "$TOKEN" | gh secret set "$SECRET_NAME" --repo "$repo"; then
    echo "  $repo: $SECRET_NAME updated" >&2
  else
    echo "  $repo: FAILED — update manually before the old PAT expires" >&2
    status=1
  fi
done
exit $status
