#!/usr/bin/env python3
"""MA-independent (`messages-api`) learn for store-backed repos — the headless
counterpart to managed/learn.py's coordinator session.

Why this exists
---------------
The fleet runs reviews headless (no managed coordinator). But `/air:learn`
curation still spun up a managed *session* (learn.py -> client.beta.sessions),
so a 100%-headless review fleet still carried one managed dependency. This
module removes it: it curates the pattern store CLIENT-SIDE with plain
`messages.create` calls and deterministic Python, exactly the architectural move
air made for reviews (kill the orchestrator; Python orchestrates; bounded LLM
calls; deterministic writes).

Shape — map / reduce, NOT one long agentic session
--------------------------------------------------
A stateful session re-reads its growing thread (~10x per learn-orchestrator.md
Step 4) — its #1 cost. This driver is stateless: each curatable file is ONE
single-shot `complete()` call (content fed in-prompt, no exploratory tool loop),
run concurrently; Python reduces + writes. Benefits over the session:
  * MA-independent  — plain Messages API, no session/coordinator/scheduling stall.
  * cheaper         — kills the thread-re-read multiplier; each call caches the
                      shared instruction prefix; map-calls are Batch-API-ready
                      (single-shot) for a future 50% discount (Phase 2).
  * reliable        — deterministic orchestration; one flaky file-curation is
                      isolated + skipped, never aborts the run; sha256-
                      preconditioned writes; no 25-min-session-timeout-kills-all;
                      composes with meta.py's atomic claim-lock (no learn-storm).

The LLM PROPOSES a curated file; Python WRITES it (memory_store.update_with,
sha256) — the same injection-safe split as pattern_writer. A SIZE-FLOOR guard
refuses to write a curation that collapses a file (an LLM truncation/error can
never silently destroy lifecycle counts); the store keeps full fidelity, the
deterministic render+wiki_cap own the bounded mirror.

Scope (Phase 1a): curate the PATTERN STORE — the source of truth reviews read
(per-author files, common-findings, service-patterns, glossary). REVIEW-HISTORY
(KAIROS) regeneration + PROJECT-PROFILE refresh are staged (see _STAGED below):
they need PR-history fetch + finding extraction / a repo scan, and neither is
read on the review hot path the way patterns are. Render + counter-reset run
regardless, so the wiki mirror + cadence stay correct.

Store-backed repos only (legacy-wiki repos keep the CLI/managed wiki pipeline).
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import memory_store
import render_store_to_wiki

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins", "air", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import meta  # noqa: E402  (plugins/air/lib — shared counter, stdlib)

MODEL = os.environ.get("AIR_LEARN_MODEL", "claude-sonnet-4-6")
# Concurrency cap for the map stage — mirrors review.py's PRECOMP_PARALLELISM.
MAP_PARALLELISM = int(os.environ.get("AIR_LEARN_PARALLELISM", "8"))
# Safety floor: refuse to write a curated file that collapses below this
# fraction of the original byte size — an LLM truncation/error must never
# silently destroy lifecycle counts. Legit semantic dedup rarely halves a file;
# the deterministic wiki_cap (in render) owns the *upper* bound separately.
MIN_KEEP_FRACTION = float(os.environ.get("AIR_LEARN_MIN_KEEP", "0.5"))

# Shared, curatable store files (besides per-author files). Each maps to a
# curation persona below. REVIEW-HISTORY + PROJECT-PROFILE are intentionally
# absent — see _STAGED.
_SHARED_CURATABLE = (
    memory_store.GLOSSARY_PATH,
    memory_store.COMMON_FINDINGS_PATH,
    memory_store.SERVICE_PATTERNS_PATH,
)

_STAGED = (
    "REVIEW-HISTORY (KAIROS): needs the last ~30 PR review bodies fetched + "
    "per-PR finding extraction (map) + cumulative-table aggregation (reduce). "
    "PROJECT-PROFILE refresh: needs a repo scan. Both are Phase-1b — they don't "
    "feed the review hot path the way patterns do. Hook: extend _curation_specs "
    "+ a fetch step; the map/reduce/write/render scaffolding here already fits."
)


# --- curation prompts (single-sourced shape; mirror learn-orchestrator.md) ---

_AUTHOR_PERSONA = (
    "You curate ONE air author-pattern file (lifecycle format: "
    "`- **<name>** (<Nx>: <PR refs> | last <N> PRs: <M> clean): <tendency>`).\n"
    "ONLY valid operations: merge SEMANTIC DUPLICATES within this author "
    "(combine counts + PR refs, keep the higher clean counter), fix formatting "
    "to the lifecycle shape, window an over-long PR-ref list to the most-recent "
    "~8 (the COUNT is preserved), and trim per-entry narrative to the 3 most "
    "recent examples. NEVER drop a pattern, NEVER lower a count, NEVER remove an "
    "(archived)/(declining) tag, NEVER invent a pattern. No per-pass changelog "
    "narrative. Return the COMPLETE curated file and nothing else."
)
_GLOSSARY_PERSONA = (
    "You curate the air GLOSSARY — a domain-term reference read into every "
    "review. Each term is ONE table row: `| `Term` | Definition | source |`. "
    "Definitions are terse (~200 chars: what the term IS) EXCEPT a definition "
    "encoding a governance rule / gotcha / safety property, which is kept in "
    "full. NEVER drop a term. Strip any header essay or per-pass narrative to a "
    "single date line. Return the COMPLETE curated glossary and nothing else."
)
_FINDINGS_PERSONA = (
    "You curate an air shared-findings file (Common Findings or Service-Specific "
    "Patterns). Merge semantic duplicates, keep entries terse and generalized "
    "(the tendency, not the one incident), cap at ~15 entries by dropping only "
    "exact duplicates/obsolete items. NEVER invent findings. No per-pass "
    "narrative. Return the COMPLETE curated file and nothing else."
)


def _persona_for(path: str) -> str:
    if path.startswith(memory_store.AUTHOR_PREFIX):
        return _AUTHOR_PERSONA
    if path == memory_store.GLOSSARY_PATH:
        return _GLOSSARY_PERSONA
    return _FINDINGS_PERSONA


def _default_complete(persona: str, content: str, *, label: str = "") -> str:
    """Single-shot curation call. Caches the persona (stable prefix) so a batch
    of files curated in one run shares the cached system prompt."""
    from anthropic import Anthropic
    client = Anthropic()
    msg = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=[{"type": "text", "text": persona,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": f"Curate this file. Return only the curated file.\n\n{content}"}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _curate_one(path: str, content: str, complete, log) -> tuple[str, str, str | None]:
    """Run one curation map-call with the size-floor safety guard.
    Returns (path, original_stripped, curated) — curated is None when the result
    is unsafe/empty/unchanged (caller skips the write). original_stripped lets
    the writer detect a concurrent change and yield. Never raises — a flaky file
    is isolated, never aborts the run."""
    original = (content or "").strip()
    if not original:
        return path, original, None  # nothing to curate
    try:
        curated = complete(_persona_for(path), content, label=path)
    except Exception as e:  # isolate: one bad file never aborts the run
        log(f"  [learn] curate failed for {path}: {type(e).__name__}: {e} — keeping current")
        return path, original, None
    curated = (curated or "").strip()
    if not curated:
        log(f"  [learn] empty curation for {path} — keeping current")
        return path, original, None
    if len(curated) < len(original) * MIN_KEEP_FRACTION:
        # Collapse guard: a curation that halves a file is almost certainly a
        # truncation/error, not a legit dedup — refuse it (the store keeps full
        # fidelity; a real shrink can land on the next run once verified).
        log(f"  [learn] curation for {path} collapsed "
            f"({len(original)}->{len(curated)} bytes) — REFUSED (size floor)")
        return path, original, None
    if curated == original:
        return path, original, None  # no-op — skip the write
    return path, original, curated


def run_headless_learn(repo, *, token=None, store_id=None, complete=None,
                       dry_run=False, log=print) -> dict:
    """Curate the pattern store for `repo` client-side, render the wiki mirror,
    and reset the learn counter. Returns a summary dict. Best-effort throughout:
    render + reset failures are logged, never raised (the curation already
    landed). Store-backed repos only.
    """
    complete = complete or _default_complete
    token = token or os.environ.get("AIR_BOT_TOKEN", "")
    store_id = store_id or memory_store.get_store_id(repo, flow="learn")
    if not store_id:
        log(f"  [learn] {repo} has no pattern store — not a store-backed repo; "
            f"skipping headless learn (use the CLI/managed wiki pipeline).")
        return {"store_id": None, "curated": [], "skipped": "no-store"}

    log(f"  [learn] headless curation for {repo} (store {store_id}, dry_run={dry_run})")
    log(f"  [learn] STAGED (not yet in headless): {_STAGED}")

    listing = memory_store.list_memories(store_id, "/")
    targets = [p for p in listing
               if p.startswith(memory_store.AUTHOR_PREFIX) or p in _SHARED_CURATABLE]
    if not targets:
        log("  [learn] store has no curatable files yet — nothing to do")
        targets = []

    # --- MAP: one single-shot curation call per file, concurrently ---
    # proposals[path] = (original_stripped, curated)
    proposals: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=MAP_PARALLELISM) as pool:
        futs = {}
        for path in targets:
            got = memory_store.read_memory(store_id, path)
            content = got[0] if got else ""
            futs[pool.submit(_curate_one, path, content, complete, log)] = path
        for fut in as_completed(futs):
            path, original, curated = fut.result()
            if curated is not None:
                proposals[path] = (original, curated)

    # --- REDUCE + WRITE: deterministic, sha256-preconditioned ---
    written = []
    if dry_run:
        for path in sorted(proposals):
            log(f"  [learn] (dry-run) would update {path}")
    else:
        for path in sorted(proposals):
            original, curated = proposals[path]
            # Race-aware: update_with re-reads current; if a per-review
            # pattern_writer strengthen landed since our MAP read (current !=
            # the snapshot we curated), YIELD — return current unchanged so
            # update_with no-ops (memory_store.py:189), never clobbering the
            # strengthen. The next learn re-curates the merged state.
            def _write(cur, _orig=original, _new=curated):
                return _new if cur.strip() == _orig else cur
            try:
                result = memory_store.update_with(store_id, path, _write, must_exist=True)
                if result is not None and result.strip() == curated:
                    written.append(path)
                    log(f"  [learn] updated {path}")
                else:
                    log(f"  [learn] {path} changed since curation (concurrent "
                        f"write) — yielded, not clobbered")
            except Exception as e:
                log(f"  [learn] write failed for {path}: {type(e).__name__}: {e}")

    # --- RENDER mirror (deterministic) + RESET counter — best-effort ---
    rendered = False
    if not dry_run and written:
        try:
            rendered = render_store_to_wiki.render_push_and_stamp(store_id, repo, token)
        except Exception as e:
            log(f"  [learn] mirror render failed: {type(e).__name__}: {e}")
    if not dry_run:
        try:
            meta.main(["reset", "--store-id", store_id, "--pr-number", "0"])
        except Exception as e:
            log(f"  [learn] counter reset failed: {type(e).__name__}: {e}")

    return {"store_id": store_id, "curated": sorted(proposals),
            "written": written, "rendered": rendered, "dry_run": dry_run}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="MA-independent headless learn (store-backed repos)")
    p.add_argument("repo", help="owner/repo")
    p.add_argument("--dry-run", action="store_true", help="curate + diff, do not write/render")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    summary = run_headless_learn(args.repo, dry_run=args.dry_run)
    print(f"[learn] done: {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
