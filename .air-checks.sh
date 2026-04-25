#!/bin/bash
# Pre-commit drift checks for the air repo itself.
# Invoked by plugins/air/hooks/pre-commit-drift.py on every `git commit`.
#
# Pattern: call the plugin's built-in auto-detection first (catches standard
# version-mirror drift), then add air-specific extras below. Skip temporarily
# with `git commit --no-verify`.

set -u
status=0
fail() { printf '  [FAIL] %s\n' "$1" >&2; status=1; }

# --- Built-in auto-detection (version mirror, shields badge, etc.) ---
# $AIR_PLUGIN_ROOT is exported by the pre-commit hook at invocation time.
if [ -n "${AIR_PLUGIN_ROOT:-}" ] && [ -x "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" ]; then
  "$AIR_PLUGIN_ROOT/hooks/builtin-checks.sh" || status=1
fi

# --- Air-specific extras below ---

# Check A: no bare /tmp/<name> operational paths (should be $AIR_TMP/<name>).
# Allow-list: mktemp calls, find /tmp GC, and /tmp in example/docstring prose.
STRAY_TMP=$(grep -rn '/tmp/' plugins/air/ managed/prompts/ 2>/dev/null \
  | grep -Ev 'mktemp|find /tmp|do NOT fall back|e\.g\.?[,:]? */tmp/|parallel session|session temp directory')
if [ -n "$STRAY_TMP" ]; then
  printf '%s\n' "$STRAY_TMP" >&2
  fail "bare /tmp/<name> paths found (should be \$AIR_TMP/<name>)"
fi

# Check B: every PR-Context-building file carries the literal
# `Wiki files directory:` field. managed/prompts/orchestrator.md was deleted
# in the move to client-side orchestration (v1.7.0) — managed review now
# builds the PR Context block in review.py and passes it as a user message
# instead of having a server-side orchestrator prompt render it.
for f in \
  plugins/air/commands/review.md \
  plugins/air/commands/review-self.md \
  plugins/air/commands/review-respond.md; do
  grep -q 'Wiki files directory:' "$f" 2>/dev/null \
    || fail "$f missing literal 'Wiki files directory:' field in PR Context template"
done

# Check C: all 5 agents must share the byte-identical do-NOT-fall-back sentence.
CANON_SENTENCE="If the \`Wiki files directory:\` field is missing from the PR Context, proceed without patterns — do NOT fall back to reading \`/tmp/REVIEW.md\` directly (those paths may belong to a parallel session)."
for f in plugins/air/agents/code-reviewer.md \
         plugins/air/agents/simplify.md \
         plugins/air/agents/security-auditor.md \
         plugins/air/agents/git-history-reviewer.md \
         plugins/air/agents/review-verifier.md; do
  grep -qF "$CANON_SENTENCE" "$f" \
    || fail "$f missing canonical 'do NOT fall back' sentence"
done

# Check D: all 4 specialist agents must carry the `[already raised by @`
# duplicate-flagging instruction (verifier sees the annotated output but
# doesn't produce findings, so it's exempt). Catches drift where one
# agent prompt is updated and the others miss the matching change.
for f in plugins/air/agents/code-reviewer.md \
         plugins/air/agents/simplify.md \
         plugins/air/agents/security-auditor.md \
         plugins/air/agents/git-history-reviewer.md; do
  grep -qF '[already raised by @' "$f" \
    || fail "$f missing PR-conversation duplicate-flag instruction (literal '[already raised by @')"
done

if [ "$status" -eq 0 ]; then
  printf 'air drift-check: all checks passed.\n'
fi
exit $status
