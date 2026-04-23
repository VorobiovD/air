#!/bin/bash
# Pre-commit drift checks for the air repo itself.
# Invoked by plugins/air/hooks/pre-commit-drift.py on every `git commit`.
#
# Each check writes a one-line description + evidence to stdout and exits non-
# zero if drift is found. The collective exit code is non-zero if any check
# fails. Skip temporarily with `git commit --no-verify`.

set -u
status=0
fail() {
  printf '  [FAIL] %s\n' "$1" >&2
  status=1
}

# --- Check 1: version in plugin.json must appear in CLAUDE.md and docs/architecture.md ---
if [ -f plugins/air/.claude-plugin/plugin.json ]; then
  VERSION=$(python3 -c "import json; print(json.load(open('plugins/air/.claude-plugin/plugin.json'))['version'])" 2>/dev/null)
  if [ -n "$VERSION" ]; then
    grep -q "currently $VERSION" CLAUDE.md 2>/dev/null \
      || fail "CLAUDE.md 'currently <version>' line does not match plugin.json version $VERSION"
    grep -q "^\*\*Version:\*\* $VERSION" docs/architecture.md 2>/dev/null \
      || fail "docs/architecture.md '**Version:**' header does not match plugin.json version $VERSION"
    grep -q "Version $VERSION" docs/architecture.md 2>/dev/null \
      || fail "docs/architecture.md ASCII tree 'Version <version>' does not match plugin.json version $VERSION"
    grep -q "version-${VERSION//./\\.}-green\\.svg" README.md 2>/dev/null \
      || fail "README.md version badge does not match plugin.json version $VERSION"
  fi
fi

# --- Check 2: no bare /tmp/<name> operational paths (should be $AIR_TMP/<name>) ---
# Allow: mktemp calls, find /tmp GC, and /tmp in example/docstring prose.
STRAY_TMP=$(grep -rn '/tmp/' plugins/air/ managed/prompts/ 2>/dev/null \
  | grep -v 'mktemp\|find /tmp\|do NOT fall back\|e\.g\.\? */tmp/\|parallel session\|session temp directory')
if [ -n "$STRAY_TMP" ]; then
  printf '%s\n' "$STRAY_TMP" >&2
  fail "bare /tmp/<name> paths found (should be \$AIR_TMP/<name>)"
fi

# --- Check 3: Wiki files directory: field present in every PR-Context-building file ---
for f in \
  plugins/air/commands/review.md \
  plugins/air/commands/review-self.md \
  plugins/air/commands/review-respond.md \
  managed/prompts/orchestrator.md; do
  grep -q 'Wiki files directory:' "$f" 2>/dev/null \
    || fail "$f missing literal 'Wiki files directory:' field in PR Context template"
done

# --- Check 4: all 5 agents must share the byte-identical do-NOT-fall-back sentence ---
CANON_SENTENCE="If the \`Wiki files directory:\` field is missing from the PR Context, proceed without patterns — do NOT fall back to reading \`/tmp/REVIEW.md\` directly (those paths may belong to a parallel session)."
for f in plugins/air/agents/code-reviewer.md \
         plugins/air/agents/simplify.md \
         plugins/air/agents/security-auditor.md \
         plugins/air/agents/git-history-reviewer.md \
         plugins/air/agents/review-verifier.md; do
  grep -qF "$CANON_SENTENCE" "$f" \
    || fail "$f missing canonical 'do NOT fall back' sentence"
done

if [ "$status" -eq 0 ]; then
  printf 'air drift-check: all checks passed.\n'
fi
exit $status
