#!/usr/bin/env python3
"""Deterministic wiki bloat-cap — the advisory→enforced counterpart to the
learn-orchestrator's soft size caps.

The learn session is *told* to keep the wiki bounded (~15 entries, ~200-char
glossary defs, "no per-pass narrative"), but those caps are advisory prose an
LLM applies inconsistently — the documented blowups (GLOSSARY 261KB,
PROJECT-PROFILE 173KB, REVIEW-HISTORY 550KB) all happened *while the soft caps
were nominally in force*. This is the same advisory→enforced move air already
made for gating (`verdict.py`), the re-review ledger (`pin_and_resurrect`), and
the exposure floor (`count_category_floored`): the model curates (semantic,
where it's good); Python assigns the hard ceiling (deterministic, where the
model is unreliable). The prompt caps stay as the semantic first pass; this is
the structural backstop that bounds the artifact regardless of what the session
produced.

Design contract:
  cap_files(files: dict[path, content]) -> (capped: dict, log: list[str])
- SAFE by construction: trims ONLY provably-redundant content — accumulated
  per-pass/changelog narrative, over-long PR-ref enumerations, and (for the
  glossary table) over-long definition tails. It NEVER drops a rule body, a
  glossary term, a lifetime count, or an active author pattern, and it NEVER
  blind-slices bytes (`content[:N]`) — every trim operates on parsed structure
  (table rows, ref-lists, narrative blocks).
- FAIL-OPEN: if a file is still over its ceiling after the full safe ladder
  (every remaining byte is must-keep), ship it WHOLE + emit a `[cap][warn]`
  line. Boundedness is best-effort against a guaranteed-safe floor — we never
  trade correctness for size (air's "stricter, never wrong" philosophy).
- IDEMPOTENT: capping already-capped output is a no-op.
- KILL SWITCH: AIR_WIKI_CAP in {0,false,no} → byte-identical pass-through.
- Per-file ceilings are env-overridable (AIR_WIKI_CAP_<FILE>); files not in the
  table pass through untouched.

Pure + stdlib-only (no API import) — unit-testable with plain strings, and
single-sourced: the managed learn tail, render_store_to_wiki, and the CLI learn
flow all call this one module.
"""
import os
import re
import sys


# The wiki files this module caps (also the commit list for the CLI/learn paths).
CAPPED_FILES = (
    "GLOSSARY.md", "PROJECT-PROFILE.md", "REVIEW.md",
    "REVIEW-HISTORY.md", "REVIEW-ARCHIVE.md",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Per-file byte ceilings. Set generously enough that the SAFE trims can plausibly
# reach them on a bloated file; the fail-open floor covers the residual. Tunable.
def _ceilings() -> dict:
    return {
        # GLOSSARY bloat is mechanical (verbose definition tails) → the cell-cap
        # reaches this. PROFILE/REVIEW are mostly must-keep rules/findings → these
        # ceilings give headroom for legit content; an over-ceiling warn is a
        # SIGNAL the orchestrator should semantically consolidate (the cap won't
        # byte-slice a rule). HISTORY/ARCHIVE rarely bind.
        "GLOSSARY.md": _env_int("AIR_WIKI_CAP_GLOSSARY", 45_000),
        "PROJECT-PROFILE.md": _env_int("AIR_WIKI_CAP_PROFILE", 50_000),
        "REVIEW.md": _env_int("AIR_WIKI_CAP_REVIEW", 44_000),
        "REVIEW-HISTORY.md": _env_int("AIR_WIKI_CAP_HISTORY", 35_000),
        "REVIEW-ARCHIVE.md": _env_int("AIR_WIKI_CAP_ARCHIVE", 90_000),
    }


def _enabled() -> bool:
    return os.environ.get("AIR_WIKI_CAP", "1").strip().lower() not in ("0", "false", "no")


# ---- safe, class-aware trims (each pure: str -> str) ------------------------

# Accumulated per-pass / changelog narrative the orchestrator is told never to
# write but does. Whole-line (or leading-block) removal — provably redundant
# (git history retains it). Conservative: only lines that clearly self-identify
# as cross-pass narrative, never a rule/finding/term line.
_PASS_NARRATIVE_RE = re.compile(
    r"(?im)^\s*[-*>]?\s*"
    r"(?:#{1,6}\s*)?"
    r"(?:\d+(?:st|nd|rd|th)\s+(?:cleanup|learn|curation)\s+pass\b"
    r"|since\s+the\s+previous\s+(?:pass|review|cleanup)\b"
    r"|(?:this|the)\s+(?:cleanup|learn|curation)\s+pass\b"
    r"|update\s+\d{4}-\d{2}-\d{2}\s*[:\-]"
    r"|changelog\s*[:\-]).*$\n?"
)


def _strip_pass_narrative(text: str) -> str:
    return _PASS_NARRATIVE_RE.sub("", text)


# Over-long PR-ref enumerations: keep the COUNT + the last N refs, drop the
# middle. Matches `(64x: #1, #11, #23, …, #190)` style lists (and bare
# `#a, #b, #c, …` runs of 4+). Counts + the most-recent refs are preserved;
# only the stale middle of a long enumeration is windowed.
_REF_RUN_RE = re.compile(r"#\d+(?:\s*,\s*#\d+){3,}")


# Inline PR/version PROVENANCE clauses that bloat rule prose — "retiered in PR
# #46", "temporarily adjusted in PR #169", parenthetical "(PR #169)" / "(#46)".
# Ephemeral (git history retains it); the rule TEXT it annotates is preserved.
# Conservative: only "<verb> in PR #N" clauses + parenthetical bare PR refs —
# NOT bare version numbers (e.g. "Python 3.11+", "v1.9.0 protocol") which can be
# load-bearing in a rule.
_PR_PROVENANCE_RE = re.compile(
    r"[,;]?\s*\(?\b(?:re-?tiered|adjusted|introduced|added|removed|changed|updated|"
    r"tightened|relaxed|deferred|reinstated|temporarily\s+\w+)\s+in\s+(?:PR\s*)?#\d+\b\)?"
    r"|\s*\((?:see\s+)?(?:PR|MR)?\s*#\d+\)",
    re.IGNORECASE,
)


def _strip_pr_provenance(text: str) -> str:
    return _PR_PROVENANCE_RE.sub("", text)


def _window_ref_lists(text: str, keep_n: int = 8) -> str:
    def _win(m: re.Match) -> str:
        refs = re.findall(r"#\d+", m.group(0))
        if len(refs) <= keep_n:
            return m.group(0)
        return "…, " + ", ".join(refs[-keep_n:])
    return _REF_RUN_RE.sub(_win, text)


# A glossary row is `| term | definition |`. Cap the DEFINITION cell to a char
# budget at a sentence/word boundary, preserving the term + the leading
# definition (what the term IS). Rows that aren't 2-column table rows pass
# through untouched (header, separator, prose).
_TABLE_ROW_RE = re.compile(r"^\|(?P<term>[^|]*)\|(?P<defn>.*)\|\s*$")


def _cap_glossary_cells(text: str, max_cell: int) -> str:
    out = []
    for line in text.split("\n"):
        m = _TABLE_ROW_RE.match(line)
        if not m:
            out.append(line)
            continue
        defn = m.group("defn")
        # leave the markdown separator row (|---|---|) and short cells alone
        if set(defn.strip()) <= {"-", ":", " "} or len(defn) <= max_cell:
            out.append(line)
            continue
        head = defn[:max_cell]
        # back off to the last sentence end, else last space, to avoid mid-word cut
        cut = max(head.rfind(". "), head.rfind("; "))
        if cut < max_cell // 2:
            cut = head.rfind(" ")
        kept = defn[:cut + 1].rstrip() if cut > 0 else head.rstrip()
        out.append(f"|{m.group('term')}|{kept} …|")
    return "\n".join(out)


def _drop_dup_table_rows(text: str) -> str:
    """Drop exact-duplicate glossary rows (same term key), keeping the first."""
    seen = set()
    out = []
    for line in text.split("\n"):
        m = _TABLE_ROW_RE.match(line)
        if m:
            defn = m.group("defn").strip()
            is_separator = defn != "" and set(defn) <= {"-", ":", " "}
            key = m.group("term").strip().lower()
            if not is_separator and key:
                if key in seen:
                    continue  # exact-duplicate term row — drop
                seen.add(key)
        out.append(line)
    return "\n".join(out)


# ---- per-file ladders -------------------------------------------------------

def _cap_glossary(text: str) -> str:
    t = _strip_pass_narrative(text)
    t = _drop_dup_table_rows(t)
    t = _window_ref_lists(t)
    # progressively tighter cell budgets until under ceiling (fail-open below)
    ceiling = _ceilings()["GLOSSARY.md"]
    for budget in (400, 320, 260):
        if len(t.encode()) <= ceiling:
            break
        t = _cap_glossary_cells(t, budget)
    return t


def cap_files(files: dict) -> tuple:
    """Apply the safe trim ladder to each known wiki file. Returns
    (capped_files, log_lines). Unknown files + already-small files pass
    through. Fail-open: a file still over ceiling after the ladder ships whole
    with a [cap][warn] line."""
    if not _enabled():
        return dict(files), ["[cap] disabled (AIR_WIKI_CAP) — pass-through"]
    ceilings = _ceilings()
    out = {}
    log = []
    for name, content in files.items():
        ceiling = ceilings.get(name)
        before = len(content.encode())
        if ceiling is None or before <= ceiling:
            out[name] = content
            continue
        if name == "GLOSSARY.md":
            capped = _cap_glossary(content)
        else:
            # generic prose files (profile/review/history): strip per-pass
            # narrative + ephemeral PR provenance + window long ref-lists. All
            # SAFE — rule/finding TEXT is preserved; only redundant provenance
            # and stale ref enumerations are removed.
            capped = _window_ref_lists(
                _strip_pr_provenance(_strip_pass_narrative(content)))
        after = len(capped.encode())
        out[name] = capped
        if after > ceiling:
            log.append(f"[cap][warn] {name} {after // 1000}KB still > {ceiling // 1000}KB "
                       f"after safe ladder (all must-keep) — shipped whole")
        elif after < before:
            log.append(f"[cap] {name} {before // 1000}KB → {after // 1000}KB "
                       f"(ceiling {ceiling // 1000}KB)")
    return out, log


def cap_dir(wiki_dir: str, dry_run: bool = False) -> list:
    """Cap the known wiki files in `wiki_dir` IN PLACE (the learn.py / CLI path).
    Returns the [cap] log lines. dry_run computes but doesn't write."""
    from pathlib import Path
    d = Path(wiki_dir)
    files = {n: (d / n).read_text(encoding="utf-8")
             for n in _ceilings() if (d / n).is_file()}
    capped, log = cap_files(files)
    if not dry_run:
        for name, content in capped.items():
            if content != files.get(name):
                (d / name).write_text(content, encoding="utf-8")
    return log


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Deterministic wiki bloat-cap (in place)")
    ap.add_argument("--dir", required=True, help="wiki checkout dir to cap in place")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, don't write")
    a = ap.parse_args()
    for line in cap_dir(a.dir, a.dry_run):
        print(line, file=sys.stderr)
