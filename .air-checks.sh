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
# Scope: operational flow only. Skip binaries (-I) and build/test dirs — test
# fixtures use literal /tmp as adversarial sandbox-refusal input, not as paths
# air writes to. Allow-list: mktemp calls, find /tmp GC, and /tmp in prose.
STRAY_TMP=$(grep -rnI --exclude-dir=__pycache__ --exclude-dir=tests '/tmp/' plugins/air/ managed/prompts/ 2>/dev/null \
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

# Check D: all 4 specialist agents must carry the duplicate-flagging
# instruction (verifier sees the annotated output but doesn't produce
# findings, so it's exempt). Anchor on the section header literal
# `PR conversation duplicate-flagging:` rather than the bracket marker
# `[already raised by @` — the latter could appear in any quoted
# example or unrelated context, so its presence isn't a reliable signal
# the actual instruction is intact.
for f in plugins/air/agents/code-reviewer.md \
         plugins/air/agents/simplify.md \
         plugins/air/agents/security-auditor.md \
         plugins/air/agents/git-history-reviewer.md; do
  grep -qF 'PR conversation duplicate-flagging:' "$f" \
    || fail "$f missing 'PR conversation duplicate-flagging:' section header"
done

# Check E: the verdict-gating contract must stay in lockstep between the
# shared implementation (plugins/air/lib/verdict.py — executed by BOTH the
# CLI's Step 12 and managed CI) and the pipeline spec (review.md) that
# instructs the model to emit the shape the parser reads.
VERDICT_LIB=plugins/air/lib/verdict.py
REVIEW_MD=plugins/air/commands/review.md
for status_token in "FIXED" "NOT FIXED" "PARTIALLY FIXED" "DEFERRED" "DISPUTED"; do
  grep -qF "$status_token" "$REVIEW_MD" \
    || fail "review.md missing re-review status token '$status_token' (lib/verdict.py parses it)"
done
grep -qF 'FIXED|NOT\s+FIXED|PARTIALLY\s+FIXED|DEFERRED|DISPUTED' "$VERDICT_LIB" \
  || fail "lib/verdict.py status enum changed — update review.md Step 6 + this check together"
grep -qF 'lib/verdict.py" --decide' "$REVIEW_MD" \
  || fail "review.md Step 12 no longer routes the verdict through lib/verdict.py --decide"
grep -qF -- '- **#N** [<severity>] — STATUS' "$REVIEW_MD" \
  || fail "review.md missing the prior-status entry anchor lib/verdict.py parses"
# PR 7: the re-review severity-pin + ledger guard. The deterministic functions
# must stay in the shared lib, and review.md's Step 11.5 must route the
# re-review body through `--pin` before the Step 12 `--decide` — or the CLI
# silently loses the carry-forward guarantee managed enforces.
for fn in parse_changed_lines finding_changed build_carry_forward_ledger pin_and_resurrect _canonicalize_status_synonyms; do
  grep -qF "def $fn(" "$VERDICT_LIB" \
    || fail "lib/verdict.py missing PR7 guard fn '$fn' (re-review severity-pin contract)"
done
grep -qF 'lib/verdict.py" --pin' "$REVIEW_MD" \
  || fail "review.md Step 11.5 no longer routes the re-review body through lib/verdict.py --pin"

# Check F: fresh-gate exposure floor. Two halves must stay consistent:
#  - APPLICATION lives in lib/verdict.py (count_category_floored + the
#    _BLOCKER_CATEGORIES vocabulary it floors).
#  - EMISSION lives in the verifier SYSTEM PROMPT (agents/review-verifier.md)
#    so managed + CLI + solo all emit `[sec:<token>]` from one source.
# Every blocker-class token in the frozenset MUST appear (backtick-quoted) in
# review-verifier.md, or a tag the model emits could fail to gate (or a token
# the prompt teaches isn't in the floor → silently never gates).
VERIFIER_MD=plugins/air/agents/review-verifier.md
grep -qF 'def count_category_floored(' "$VERDICT_LIB" \
  || fail "lib/verdict.py missing the floor fn 'count_category_floored' (fresh-gate determinism)"
grep -qF '_BLOCKER_CATEGORIES = frozenset(' "$VERDICT_LIB" \
  || fail "lib/verdict.py missing the floor vocabulary frozenset '_BLOCKER_CATEGORIES'"
grep -qF '[sec:<token>]' "$VERIFIER_MD" \
  || fail "$VERIFIER_MD no longer instructs the verifier to emit the [sec:<token>] gate tag"
# Lock the markdown token list to the frozenset (markdown can't import Python).
python3 - "$VERDICT_LIB" "$VERIFIER_MD" <<'PYF' || status=1
import re, sys
lib, md = open(sys.argv[1]).read(), open(sys.argv[2]).read()
m = re.search(r"_BLOCKER_CATEGORIES = frozenset\(\{(.*?)\}\)", lib, re.DOTALL)
toks = re.findall(r'"([a-z0-9-]+)"', m.group(1)) if m else []
missing = [t for t in toks if f"`{t}`" not in md]
if missing:
    print(f"  [FAIL] review-verifier.md missing floor token(s): {missing}", file=sys.stderr)
    sys.exit(1)
PYF

if [ "$status" -eq 0 ]; then
  printf 'air drift-check: all checks passed.\n'
fi
exit $status
