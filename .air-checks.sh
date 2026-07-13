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
# Lock the markdown token list to the frozenset BOTH WAYS (markdown can't import
# Python). Forward: every frozenset token is taught in the prompt. Reverse
# (audit CHK-F): every token the prompt teaches in the fixed-vocabulary sentence
# is in the frozenset — a token taught but NOT floored would be emitted as
# `[sec:<tok>]` and silently NEVER gate (the exact failure the floor prevents).
python3 - "$VERDICT_LIB" "$VERIFIER_MD" <<'PYF' || status=1
import re, sys
lib, md = open(sys.argv[1]).read(), open(sys.argv[2]).read()
m = re.search(r"_BLOCKER_CATEGORIES = frozenset\(\{(.*?)\}\)", lib, re.DOTALL)
toks = set(re.findall(r'"([a-z0-9-]+)"', m.group(1)) if m else [])
missing = sorted(t for t in toks if f"`{t}`" not in md)
if missing:
    print(f"  [FAIL] review-verifier.md missing floor token(s): {missing}", file=sys.stderr)
    sys.exit(1)
# Reverse: extract bare backtick tokens from the 'fixed vocabulary:' line (a
# dotted token like `verdict.py` on that same line is not a bare backtick match).
vocab_line = next((ln for ln in md.splitlines() if "fixed vocabulary:" in ln), "")
taught = set(re.findall(r"`([a-z][a-z0-9-]+)`", vocab_line))
extra = sorted(taught - toks)
if extra:
    print(f"  [FAIL] review-verifier.md teaches [sec:] token(s) absent from "
          f"_BLOCKER_CATEGORIES (would never gate): {extra}", file=sys.stderr)
    sys.exit(1)
if not taught:
    print("  [FAIL] could not locate the [sec:] 'fixed vocabulary:' enumeration "
          "in review-verifier.md (Check F reverse anchor moved)", file=sys.stderr)
    sys.exit(1)
PYF

# Check G: wiki bloat-cap contract. wiki_cap.py must define CAPPED_FILES with the
# 5 wiki files AND a ceiling for each in _ceilings() — locks the cap set the way
# Check F locks the security vocabulary (a silently-dropped ceiling would un-bound
# a wiki file, re-opening the bloat deadlock).
WIKI_CAP_LIB="plugins/air/lib/wiki_cap.py"
if [ -f "$WIKI_CAP_LIB" ]; then
  python3 - "$WIKI_CAP_LIB" <<'PYF' || status=1
import re, sys
src = open(sys.argv[1]).read()
need = {"GLOSSARY.md", "PROJECT-PROFILE.md", "REVIEW.md",
        "REVIEW-HISTORY.md", "REVIEW-ARCHIVE.md"}
m = re.search(r"CAPPED_FILES = \((.*?)\)", src, re.DOTALL)
capped = set(re.findall(r'"([A-Z][A-Z-]+\.md)"', m.group(1) if m else ""))
cm = re.search(r"def _ceilings\(\).*?return \{(.*?)\}", src, re.DOTALL)
body = cm.group(1) if cm else ""
miss_set = sorted(need - capped)
miss_ceil = sorted(f for f in need if f'"{f}"' not in body)
if miss_set:
    print(f"  [FAIL] wiki_cap.py CAPPED_FILES missing: {miss_set}", file=sys.stderr); sys.exit(1)
if miss_ceil:
    print(f"  [FAIL] wiki_cap.py _ceilings() missing a ceiling for: {miss_ceil}", file=sys.stderr); sys.exit(1)
PYF
else
  fail "$WIKI_CAP_LIB missing — the deterministic wiki bloat-cap contract is gone"
fi

# Check H: the store-mirror banner substring must stay byte-identical across the
# render that WRITES it (render_store_to_wiki.py MIRROR_BANNER) and the two CLI
# detectors that GREP it (review.md Step 3, learn.md Step 1). If the banner is
# reworded without updating the greps, the CLI silently stops detecting store
# mirrors and re-opens the clobber bug (#224) — so lock the shared substring.
BANNER_SUBSTR='source of truth is the air pattern memory store'
for f in managed/render_store_to_wiki.py plugins/air/commands/review.md plugins/air/commands/learn.md; do
  if [ -f "$f" ]; then
    grep -qF "$BANNER_SUBSTR" "$f" \
      || fail "$f missing the store-mirror banner substring (Check H: render MIRROR_BANNER ↔ CLI mirror detection)"
  fi
done

# Check I: review-format v2 contract. The v2 layout (verdict banner + folded
# evidence/nits/strengths) restyles only FREE-PROSE zones — the gate parses
# line-anchored regex over the raw body, so the frozen anchors must stay
# byte-exact. Lock: (1) the AIR_REVIEW_FORMAT kill switch exists, (2) the
# Blockers heading is NEVER decorated in the emitted skeletons (a suffix/emoji
# makes count_blockers match nothing → a real blocker silently un-gates),
# (3) review-verifier.md's Output Format states the verdict-banner + no-prefix
# frozen-anchor rules.
if [ -f managed/prompts.py ]; then
  { grep -q 'def review_format' managed/prompts.py && grep -q 'AIR_REVIEW_FORMAT' managed/prompts.py; } \
    || fail "managed/prompts.py missing the AIR_REVIEW_FORMAT kill switch (review_format())"
fi
for f in managed/prompts.py plugins/air/commands/review.md; do
  # `[[:space:]]*` (not `+`) after Blockers so a GLUED decoration (`### Blockers🔴`,
  # no separating space) is caught too — a mandatory-space pattern missed it.
  if [ -f "$f" ] && grep -nE '^#{3,4}[[:space:]]+Blockers[[:space:]]*[^[:space:]]' "$f" >/dev/null 2>&1; then
    fail "$f has a DECORATED Blockers heading — it must stay exactly '### Blockers' / '#### Blockers' (count_blockers anchors on 'Blockers\$')"
  fi
done
if [ -f "$VERIFIER_MD" ]; then
  { grep -q '\[!CAUTION\]' "$VERIFIER_MD" && grep -q 'NEVER prefix it with an emoji' "$VERIFIER_MD"; } \
    || fail "$VERIFIER_MD Output Format section missing the v2 verdict-banner / no-prefix frozen-anchor rules (Check I)"
fi
# CHK-I positive lock: the decoration check above only FORBIDS a bad heading; it
# does not ensure the heading EXISTS. A rename (### Blockers → ### Blocking
# Issues) would make count_blockers match nothing → every blocker silently
# un-gates, yet pass both the decoration check and Check E. Assert the literal
# heading is present in the emitted skeletons (count_blockers now tolerates a
# decorated suffix, but a rename/removal must still fail loud).
# The 3-hash pattern is anchored to NOT be preceded by a '#' — a plain
# `grep -F '### Blockers'` also matches `#### Blockers` (it's a substring,
# offset one hash), so a rename of ONLY the fresh 3-hash heading would still
# pass while count_blockers matches nothing on a fresh review (the H5 fail-open,
# one layer down). `(^|[^#])` requires a real 3-hash heading.
grep -qE '(^|[^#])### Blockers' managed/prompts.py \
  || fail "managed/prompts.py no longer emits a 3-hash '### Blockers' heading (count_blockers would match nothing on a fresh review → un-gate)"
grep -qE '(^|[^#])### Blockers' plugins/air/commands/review.md \
  || fail "plugins/air/commands/review.md no longer emits a 3-hash '### Blockers' heading"
grep -qF '#### Blockers' managed/prompts.py \
  || fail "managed/prompts.py no longer emits the re-review '#### Blockers' subsection heading"

# Check J: CLI verdict sentinel ↔ AIR_VERDICT_SENTINEL. The CLI (review.md
# Step 12) posts its APPROVE/REQUEST_CHANGES verdict under the developer's OWN
# account and must stamp the SAME invisible sentinel the managed path uses, so a
# stale CLI block on a since-fixed blocker is dismissable by CI/headless air's
# cross-account cleanup (github_client.dismiss_stale_air_verdicts). Without it a
# CLI verdict looks like a human review and gates forever. The literal is
# hardcoded in the markdown (can't import the Python constant), so lock it to
# AIR_VERDICT_SENTINEL and require it on every CLI verdict-submission line.
if [ -f managed/github_client.py ] && [ -f plugins/air/commands/review.md ]; then
  SENTINEL=$(grep -oE 'AIR_VERDICT_SENTINEL = "[^"]+"' managed/github_client.py | sed -E 's/.*"([^"]+)".*/\1/')
  if [ -z "$SENTINEL" ]; then
    fail "Check J: could not extract AIR_VERDICT_SENTINEL from managed/github_client.py"
  elif grep -nE 'reviews .*-f event=(APPROVE|REQUEST_CHANGES)' plugins/air/commands/review.md | grep -vqF "$SENTINEL"; then
    fail "review.md has a CLI verdict-submission line missing the air sentinel '$SENTINEL' (Check J: CLI verdicts must carry AIR_VERDICT_SENTINEL so CI air can clear stale CLI blocks)"
  fi
fi

if [ "$status" -eq 0 ]; then
  printf 'air drift-check: all checks passed.\n'
fi
exit $status
