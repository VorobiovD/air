"""Core tests for diff_hygiene (the shared stub + size-cap pass), imported
directly from lib so air-lib-tests.yml covers it. Exhaustive cases (76) live in
managed/test-cost-wins.py via the github_client re-export.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import diff_hygiene as dh  # noqa: E402


def _seg(path, n=3):
    body = "".join(f"+line{i}\n" for i in range(n))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}"


def test_stubs_minified_bundle():
    out = dh.apply_diff_hygiene(_seg("assets/app.min.js", 50))
    assert "changed lines omitted (generated/vendored)" in out
    assert "+line0" not in out  # body stubbed


def test_keeps_real_source():
    diff = _seg("src/app.py", 4)
    assert dh.apply_diff_hygiene(diff) == diff  # untouched


# --- filter_diff_to_files (re-review inter-diff scope) ----------------------

def test_filter_keeps_only_listed_files():
    diff = _seg("src/a.py") + _seg("docs/mockups/x.html") + _seg("src/b.ts")
    out = dh.filter_diff_to_files(diff, {"src/a.py", "src/b.ts"})
    assert "a/src/a.py b/src/a.py" in out
    assert "a/src/b.ts b/src/b.ts" in out
    assert "docs/mockups/x.html" not in out  # merged-in noise dropped


def test_filter_drops_the_ballooning_merged_tree():
    # The repo-A #17061 shape: the PR's own file + a big merged-in docs tree.
    pr_file = _seg("packages/planex/order.ts", 5)
    noise = "".join(_seg(f"docs/epics/mockups/case-{i}.html", 40) for i in range(20))
    out = dh.filter_diff_to_files(pr_file + noise, {"packages/planex/order.ts"})
    assert out == pr_file            # exactly the PR file, all noise gone
    assert "docs/epics" not in out


def test_filter_rename_new_path_kept():
    # `diff --git a/old b/new` — b/-side (new) path is what filter matches.
    seg = "diff --git a/src/old.py b/src/new.py\nrename from src/old.py\nrename to src/new.py\n"
    assert dh.filter_diff_to_files(seg, {"src/new.py"}) == seg
    assert dh.filter_diff_to_files(seg, {"src/other.py"}) == ""


def test_filter_empty_keep_drops_everything():
    assert dh.filter_diff_to_files(_seg("src/a.py") + _seg("src/b.py"), set()) == ""


def test_filter_preserves_leading_preamble():
    # A clean GitHub compare diff starts with `diff --git`; a stray preamble
    # (defensive) is preserved rather than mis-parsed as a file segment.
    diff = "some preamble line\n" + _seg("src/a.py")
    out = dh.filter_diff_to_files(diff, {"src/a.py"})
    assert out.startswith("some preamble line\n") and "a/src/a.py" in out
    out2 = dh.filter_diff_to_files(diff, set())
    assert out2 == "some preamble line\n"   # preamble kept, file dropped


def test_lockfile_only_change_stays_whole():
    # package-lock.json with NO same-dir package.json change → not stubbed
    diff = _seg("package-lock.json", 200)
    assert "changed lines omitted" not in dh.apply_diff_hygiene(diff)


def test_lockfile_with_manifest_change_is_stubbed():
    diff = _seg("package.json", 3) + _seg("package-lock.json", 200)
    out = dh.apply_diff_hygiene(diff)
    assert "package.json" in out and "+line0" in out          # manifest kept whole
    assert "changed lines omitted" in out                      # lockfile stubbed


def test_size_cap_truncates_with_marker():
    big = _seg("src/huge.py", 5000)
    out = dh.apply_diff_hygiene(big, max_bytes=300)
    assert dh.DIFF_TRUNCATION_MARKER in out
    assert len(out.encode()) <= 300            # cap holds (budget >> ~80-byte marker floor)


def test_count_diff_changed_lines_excludes_headers():
    assert dh.count_diff_changed_lines("+++ a\n--- b\n+x\n-y\n z\n") == 2


def test_empty_diff_is_noop():
    assert dh.apply_diff_hygiene("") == ""


def test_main_rewrites_file_in_place(tmp_path):
    f = tmp_path / "pr.diff"
    f.write_text(_seg("assets/app.min.js", 40))
    assert dh._main(["--diff-file", str(f)]) == 0
    assert "changed lines omitted (generated/vendored)" in f.read_text()  # hygiene applied in place


def test_main_missing_file_returns_1(tmp_path):
    assert dh._main(["--diff-file", str(tmp_path / "nope.diff")]) == 1  # read-error guard


# --- deleted-file body stub (lifemd #17748: a deletion-heavy cleanup PR blew the
# cap on removed bodies, cap-omitted its ADDED code, and fail-closed the gate) ---

def _seg_deleted(path, lines):
    body = "".join(f"-line {i}\n" for i in range(lines))
    return (f"diff --git a/{path} b/{path}\ndeleted file mode 100644\nindex abc..000\n"
            f"--- a/{path}\n+++ /dev/null\n@@ -1,{lines} +0,0 @@\n{body}")


def _seg_modified(path, lines):
    body = "".join(f"+added {i}\n" for i in range(lines))
    return (f"diff --git a/{path} b/{path}\nindex abc..def 100644\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -1,0 +1,{lines} @@\n{body}")


def test_deletion_stub_is_inert_when_the_diff_fits():
    """Under budget ⇒ byte-identical to pre-feature. The fleet and the prompt
    cache must not move for the PRs that were always fine."""
    from diff_hygiene import apply_diff_hygiene
    d = _seg_deleted("old/legacy.js", 200) + _seg_modified("src/new.ts", 5)
    assert apply_diff_hygiene(d, max_bytes=1_000_000) == d


def test_deletion_stub_reclaims_budget_instead_of_omitting_added_code():
    from diff_hygiene import apply_diff_hygiene, DIFF_TRUNCATION_MARKER
    d = _seg_deleted("old/legacy.test.js", 4000) + _seg_modified("src/new.ts", 20)
    out = apply_diff_hygiene(d, max_bytes=5_000)
    assert "4000 lines removed (file deleted" in out          # deleted body stubbed…
    assert "git show <base-sha>:old/legacy.test.js" in out    # …and still retrievable
    assert "+added 19" in out                                 # the ADDED code survived
    assert DIFF_TRUNCATION_MARKER not in out                  # so the gate does NOT fail closed
    assert len(out.encode()) <= 5_000


def test_deletion_stub_keeps_the_git_headers_so_path_and_ledger_parsing_hold():
    from diff_hygiene import apply_diff_hygiene, _segment_path
    out = apply_diff_hygiene(_seg_deleted("a/b/gone.py", 4000) + _seg_modified("s.ts", 2),
                             max_bytes=3_000)
    seg = [s for s in out.split("diff --git ") if s.startswith("a/a/b/gone.py")][0]
    assert "deleted file mode 100644" in seg and "index abc..000" in seg
    assert _segment_path("diff --git " + seg) == "a/b/gone.py"


def test_deletion_stub_is_largest_first_and_stops_when_it_fits():
    """A diff barely over budget loses its ONE biggest deleted body, not every
    deletion in the PR (minimum damage — the trim-ladder idiom)."""
    from diff_hygiene import apply_diff_hygiene
    d = _seg_deleted("big.js", 3000) + _seg_deleted("small.js", 40) + _seg_modified("s.ts", 2)
    out = apply_diff_hygiene(d, max_bytes=4_000)
    assert "3000 lines removed" in out            # the big one was stubbed
    assert "40 lines removed" not in out          # the small one was left intact
    assert "-line 39" in out


def test_a_modification_that_deletes_many_lines_is_never_stubbed():
    """Only a whole-file removal (`deleted file mode`) qualifies — a file whose
    remaining content still needs review keeps its body."""
    from diff_hygiene import apply_diff_hygiene, DIFF_TRUNCATION_MARKER
    heavy = (f"diff --git a/src/app.ts b/src/app.ts\nindex abc..def 100644\n"
             f"--- a/src/app.ts\n+++ b/src/app.ts\n@@ -1,3000 +1,1 @@\n"
             + "".join(f"-gone {i}\n" for i in range(3000)) + "+kept\n")
    out = apply_diff_hygiene(heavy, max_bytes=4_000)
    assert "lines removed (file deleted" not in out
    assert DIFF_TRUNCATION_MARKER in out          # falls through to the honest fail-close


def test_still_fails_closed_when_stubbing_deletions_is_not_enough():
    from diff_hygiene import apply_diff_hygiene, DIFF_TRUNCATION_MARKER
    out = apply_diff_hygiene(_seg_deleted("gone.js", 500) + _seg_modified("huge.ts", 5000),
                             max_bytes=4_000)
    assert DIFF_TRUNCATION_MARKER in out          # real over-cap code ⇒ gate still fails closed


def test_deletion_stub_kill_switch(monkeypatch):
    from diff_hygiene import apply_diff_hygiene, DIFF_TRUNCATION_MARKER
    monkeypatch.setenv("AIR_DELETION_STUB", "0")
    out = apply_diff_hygiene(_seg_deleted("gone.js", 4000) + _seg_modified("s.ts", 2),
                             max_bytes=3_000)
    assert "lines removed (file deleted" not in out and DIFF_TRUNCATION_MARKER in out


def test_adversarially_named_file_cannot_forge_the_truncation_marker():
    """Git allows a file named `diff truncated.trap`. Interpolated raw, its stub
    line's first bytes collide with DIFF_TRUNCATION_MARKER — the one marker every
    consumer treats as unforgeable. Both stub sites must neutralize it."""
    from diff_hygiene import apply_diff_hygiene, DIFF_TRUNCATION_MARKER
    trap = "diff truncated.trap"
    out = apply_diff_hygiene(_seg_deleted(trap, 4000) + _seg_modified("s.ts", 2),
                             max_bytes=3_000)
    assert "lines removed (file deleted" in out              # it WAS stubbed…
    assert not any(ln.startswith(DIFF_TRUNCATION_MARKER) for ln in out.splitlines())
    assert f"'{trap}'" in out                                # …quoted, so readable and honest
    # same hole in the pre-existing generated/vendored stub: `.min.js` classifies
    # as generated, so a crafted name forges the same prefix there too
    gen = (f"diff --git a/{trap}.min.js b/{trap}.min.js\nindex a..b 100644\n"
           f"--- a/{trap}.min.js\n+++ b/{trap}.min.js\n@@ -1 +1 @@\n-a\n+b\n")
    out2 = apply_diff_hygiene(gen, max_bytes=1_000_000)
    assert "changed lines omitted (generated/vendored)" in out2
    assert not any(ln.startswith(DIFF_TRUNCATION_MARKER) for ln in out2.splitlines())


def test_marker_path_strips_bracket_and_backtick_injection():
    """A path containing `]` or a backtick would end the marker or its `git show`
    hint early, letting crafted content sit outside the marker."""
    from diff_hygiene import _safe_marker_path
    assert _safe_marker_path("a]b`c.js") == "a_b_c.js"
    assert _safe_marker_path("ok/path.js") == "ok/path.js"      # untouched otherwise
