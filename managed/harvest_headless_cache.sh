#!/usr/bin/env bash
# Harvest recent air HEADLESS reviews for cache-TTL data: pull each air-review CI run
# log, keep the ones with per-turn telemetry, and reprice 5m-vs-1h via
# analyze_cache_ttl.py (exact same-review cost + cache-miss %). Read-only; no push.
#
# This is the durable, on-demand counterpart to the in-code telemetry (agent_loop's
# `[turn] … gap=… cw=… cr=…`). Run it whenever you want a roll-up over real PRs:
#     bash managed/harvest_headless_cache.sh [N_recent_runs]   (default 20)
# Needs `gh` auth; the analyzer is offline (no ANTHROPIC_API_KEY).
set -uo pipefail
REPO="${AIR_HARVEST_REPO:-VorobiovD/air}"
N="${1:-20}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${AIR_PY:-$(cd "$HERE/.." && pwd)/.venv-test/bin/python}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

mapfile -t ids < <(gh run list --repo "$REPO" --workflow air-review.yml --limit "$N" \
  --json databaseId,status --jq '.[] | select(.status=="completed") | .databaseId')

logs=()
for id in "${ids[@]:-}"; do
  [ -z "$id" ] && continue
  gh run view "$id" --repo "$REPO" --log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' \
    | grep -E '\[turn\]|cache TTL|complete in' > "$tmp/$id.log" 2>/dev/null || true
  grep -q '\[turn\]' "$tmp/$id.log" 2>/dev/null && logs+=("$tmp/$id.log")
done

if [ "${#logs[@]}" -eq 0 ]; then
  echo "No headless runs with [turn] telemetry in the last $N air-review runs."
  echo "(Only headless/messages-api reviews emit it; managed reviews won't. Data accrues"
  echo " as air does headless reviews — enable AIR_REVIEW_MODE=messages-api on a fleet repo"
  echo " to accrue faster.)"
  exit 0
fi
echo "Harvested ${#logs[@]} headless run(s) from the last $N air-review CI runs:"
"$PY" "$HERE/analyze_cache_ttl.py" "${logs[@]}"
