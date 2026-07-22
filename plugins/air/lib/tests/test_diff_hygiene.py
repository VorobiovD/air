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
