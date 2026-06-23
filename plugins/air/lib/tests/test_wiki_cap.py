"""Unit tests for the deterministic wiki bloat-cap (managed/wiki_cap.py).

Locks the safety contract: caps shrink mechanical bloat (glossary definition
tails, ref-lists, narrative) WITHOUT dropping a glossary term, corrupting a
table row, or byte-slicing a rule — and fail open (ship whole + warn) when a
file is over-ceiling for must-keep reasons. Pure-string fixtures, no network.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import wiki_cap  # noqa: E402


def _glossary(n_rows, defn):
    head = "| Term | Definition |\n|---|---|\n"
    return head + "".join(f"| term{i} | {defn} |\n" for i in range(n_rows))


def test_glossary_cell_cap_shrinks_but_keeps_all_terms(monkeypatch):
    monkeypatch.setenv("AIR_WIKI_CAP_GLOSSARY", "25000")
    # 80 rows × ~700-char verbose definitions ≈ 56KB; cell-cap brings it under 25KB.
    verbose = ("This term is " + "padded with a long re-narrated protocol " * 18).strip()
    text = _glossary(80, verbose)
    capped, log = wiki_cap.cap_files({"GLOSSARY.md": text})
    out = capped["GLOSSARY.md"]
    assert len(out.encode()) < len(text.encode())  # shrank
    # every term row survives (no term dropped) and the table is intact
    for i in range(80):
        assert f"| term{i} |" in out
    # no row lost its trailing pipe (table not corrupted)
    rows = [l for l in out.split("\n") if l.startswith("| term")]
    assert all(l.rstrip().endswith("|") for l in rows)
    assert any(l.startswith("[cap] GLOSSARY.md") for l in log)


def test_fail_open_when_all_must_keep(monkeypatch):
    monkeypatch.setenv("AIR_WIKI_CAP_REVIEW", "100")  # absurdly low
    # REVIEW.md prose with no narrative/ref-lists to strip → cannot shrink safely.
    text = "## Common Findings\n\n**Always validate input before the DB call.**\n" * 5
    capped, log = wiki_cap.cap_files({"REVIEW.md": text})
    assert capped["REVIEW.md"] == text  # shipped WHOLE, never byte-sliced
    assert any(l.startswith("[cap][warn] REVIEW.md") for l in log)


def test_kill_switch_is_byte_identical(monkeypatch):
    monkeypatch.setenv("AIR_WIKI_CAP", "0")
    monkeypatch.setenv("AIR_WIKI_CAP_GLOSSARY", "100")
    text = _glossary(50, "x" * 600)
    capped, log = wiki_cap.cap_files({"GLOSSARY.md": text})
    assert capped["GLOSSARY.md"] == text and "disabled" in log[0]


def test_idempotent(monkeypatch):
    monkeypatch.setenv("AIR_WIKI_CAP_GLOSSARY", "4000")
    text = _glossary(60, "verbose definition " * 30)
    once, _ = wiki_cap.cap_files({"GLOSSARY.md": text})
    twice, _ = wiki_cap.cap_files(once)
    assert twice["GLOSSARY.md"] == once["GLOSSARY.md"]


def test_ref_list_windowing_keeps_count_and_recent():
    refs = ", ".join(f"#{i}" for i in range(1, 21))  # #1..#20
    out = wiki_cap._window_ref_lists(f"- **Pattern** (20x: {refs}): tends to miss guards")
    assert "20x" in out and "#20" in out and "#19" in out  # count + recent kept
    assert "#1," not in out and "…" in out                 # stale middle windowed


def test_pass_narrative_stripped():
    text = "## Glossary\n3rd cleanup pass: reorganized terms\n| a | b |\n"
    out = wiki_cap._strip_pass_narrative(text)
    assert "cleanup pass" not in out and "| a | b |" in out  # narrative gone, content kept


def test_pr_provenance_stripped_but_rule_kept():
    text = "Check auth on every endpoint (retiered in PR #46), temporarily adjusted in PR #169."
    out = wiki_cap._strip_pr_provenance(text)
    assert "Check auth on every endpoint" in out          # rule text preserved
    assert "PR #46" not in out and "PR #169" not in out    # provenance gone


def test_bare_version_not_stripped():
    # bare versions can be load-bearing in a rule — must NOT be removed
    text = "Requires Python 3.11+ and the v1.9.0 coordinator protocol."
    assert wiki_cap._strip_pr_provenance(text) == text


def test_unknown_and_small_files_passthrough(monkeypatch):
    monkeypatch.delenv("AIR_WIKI_CAP", raising=False)
    files = {"SomeOther.md": "x" * 200_000, "REVIEW.md": "tiny"}
    capped, log = wiki_cap.cap_files(files)
    assert capped["SomeOther.md"] == files["SomeOther.md"]  # not in cap table
    assert capped["REVIEW.md"] == "tiny"                    # under ceiling


def test_dup_glossary_rows_dropped():
    text = "| t | def one |\n| t | def one |\n| u | def two |\n"
    out = wiki_cap._drop_dup_table_rows(text)
    assert out.count("| t |") == 1 and "| u |" in out
