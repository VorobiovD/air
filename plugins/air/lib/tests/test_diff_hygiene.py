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
    assert len(out.encode()) <= 300 or "file(s) omitted" in out


def test_count_diff_changed_lines_excludes_headers():
    assert dh.count_diff_changed_lines("+++ a\n--- b\n+x\n-y\n z\n") == 2


def test_empty_diff_is_noop():
    assert dh.apply_diff_hygiene("") == ""
