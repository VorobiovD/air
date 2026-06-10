#!/usr/bin/env python3
"""Unit tests for the cost quick-wins batch (PR3): diff hygiene (generated
files stubbed, manifests kept, hard size cap with visible marker),
conversation tail-cap newest-kept semantics, and the codex skip on tiny
re-review deltas.

Pure functions, no network. Run: python -m pytest managed/test-cost-wins.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "air" / "lib"))
import github_client  # noqa: E402
import pr_conversation  # noqa: E402
import review  # noqa: E402
from github_client import _is_generated_path, apply_diff_hygiene  # noqa: E402


def _file_segment(path: str, added: int = 3) -> str:
    lines = [f"diff --git a/{path} b/{path}",
             f"--- a/{path}", f"+++ b/{path}", "@@ -1,1 +1,4 @@", " context"]
    lines += [f"+added line {i}" for i in range(added)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# C1 — generated-path classification
# ---------------------------------------------------------------------------

def test_lockfiles_are_generated():
    for p in ("package-lock.json", "deep/nested/yarn.lock", "go.sum",
              "composer.lock", "poetry.lock"):
        assert _is_generated_path(p) is True


def test_manifests_are_NOT_generated():
    # Supply-chain review reads these — they must stay whole.
    for p in ("package.json", "composer.json", "pyproject.toml",
              "Cargo.toml", "go.mod", "requirements.txt"):
        assert _is_generated_path(p) is False


def test_minified_and_maps_are_generated():
    assert _is_generated_path("assets/app.min.js") is True
    assert _is_generated_path("assets/app.min.css") is True
    assert _is_generated_path("assets/app.js.map") is True
    assert _is_generated_path("tests/__snapshots__/x.snap") is True


def test_segment_match_not_substring():
    assert _is_generated_path("pkg/dist/bundle.js") is True
    assert _is_generated_path("vendor/lib/x.php") is True
    # `dist`/`vendor` as substrings of real source dirs must NOT match.
    assert _is_generated_path("src/distance.py") is False
    assert _is_generated_path("src/distribution/calc.py") is False
    assert _is_generated_path("app/vendors_list.py") is False


def test_regular_source_not_generated():
    assert _is_generated_path("managed/review.py") is False
    assert _is_generated_path("src/app.js") is False


# ---------------------------------------------------------------------------
# C1 — hygiene transformation
# ---------------------------------------------------------------------------

def test_lockfile_stubbed_when_manifest_changed_too():
    # Dependency-bump shape: manifest + lockfile both in the diff → the
    # lockfile noise is stubbed, manifest + source stay whole.
    diff = (_file_segment("src/app.py") + _file_segment("package.json")
            + _file_segment("package-lock.json", added=500))
    out = apply_diff_hygiene(diff)
    assert "+added line 0" in out                     # source hunks intact
    assert "500 changed lines omitted" in out         # stub is visible
    assert out.count("+added line") == 6              # app.py + package.json only
    assert "diff --git a/package-lock.json" in out    # header survives


def test_lockfile_only_change_is_NOT_stubbed():
    # The supply-chain attack shape: resolver/integrity swap with no
    # manifest touch — must stay fully reviewable.
    diff = _file_segment("package-lock.json", added=50)
    assert apply_diff_hygiene(diff) == diff


def test_lockfile_manifest_pairing_is_per_directory():
    # Monorepo: a root package.json change must NOT justify stubbing a
    # sub-package's lockfile (its own manifest didn't change).
    diff = (_file_segment("package.json")
            + _file_segment("packages/a/yarn.lock", added=50))
    out = apply_diff_hygiene(diff)
    assert "+added line 49" in out                    # sub-lockfile kept whole
    same_dir = (_file_segment("packages/a/package.json")
                + _file_segment("packages/a/yarn.lock", added=50))
    assert "changed lines omitted" in apply_diff_hygiene(same_dir)


def test_stubbed_file_counts_zero_changed_lines():
    diff = _file_segment("assets/app.min.js", added=500)
    out = apply_diff_hygiene(diff)
    assert review._count_diff_changed_lines(out) == 0


def test_clean_diff_passes_through_byte_identical():
    diff = _file_segment("src/app.py") + _file_segment("lib/util.py")
    assert apply_diff_hygiene(diff) == diff


def test_empty_diff_passthrough():
    assert apply_diff_hygiene("") == ""


RESERVE = github_client._MARKER_RESERVE_BYTES


def test_cap_truncates_at_file_boundary_with_marker():
    diff = _file_segment("a.py", added=50) + _file_segment("b.py", added=50)
    seg_size = len(_file_segment("a.py", added=50).encode())
    out = apply_diff_hygiene(diff, max_bytes=seg_size + RESERVE + 10)
    assert "diff --git a/a.py" in out
    assert "+added line 49" in out.split("diff --git a/b.py")[0]  # a.py whole
    assert "[air: diff truncated" in out
    assert "b.py" in out.split("[air: diff truncated")[1]         # named, not silent


def test_cap_first_fit_keeps_small_file_after_oversized_one():
    # [small, huge, small] where the smalls fit together: the huge segment
    # is omitted ALONE — files after it must not be dragged down with it.
    small_a = _file_segment("small_a.py", added=5)
    huge = _file_segment("huge_generated.bin", added=2000)
    small_b = _file_segment("small_b.py", added=5)
    budget = len((small_a + small_b).encode()) + RESERVE + 10
    out = apply_diff_hygiene(small_a + huge + small_b, max_bytes=budget)
    assert "diff --git a/small_a.py" in out
    assert "diff --git a/small_b.py" in out           # survives the huge file
    assert "+added line 1999" not in out
    assert "huge_generated.bin" in out.split("[air: diff truncated")[1]


def test_cap_result_stays_within_budget_marker_included():
    diff = "".join(_file_segment(f"f{i}.py", added=80) for i in range(20))
    budget = 3000
    out = apply_diff_hygiene(diff, max_bytes=budget)
    assert len(out.encode()) <= budget


def test_cap_not_applied_when_under_budget():
    diff = _file_segment("a.py")
    assert "[air: diff truncated" not in apply_diff_hygiene(diff, max_bytes=10**6)


# ---------------------------------------------------------------------------
# C2 — conversation tail-cap keeps the NEWEST entries
# ---------------------------------------------------------------------------

def test_conversation_cap_keeps_newest():
    issues = [
        {"user": {"login": f"dev{i}"}, "body": f"comment {i}",
         "created_at": f"2026-06-{i + 1:02d}T00:00:00Z"}
        for i in range(5)
    ]
    out = pr_conversation.build_pr_conversation(issues, [], [], "air-machine",
                                                max_entries=2)
    assert "comment 4" in out and "comment 3" in out
    assert "comment 0" not in out
    assert '<conv-truncated total="5" shown="2"/>' in out


# ---------------------------------------------------------------------------
# C3 — codex skip on tiny re-review deltas
# ---------------------------------------------------------------------------

def test_codex_skips_tiny_rereview_delta():
    diff = _file_segment("src/app.py", added=5)  # 5 changed lines < 20
    assert review._codex_skip_tiny_delta("re-review", diff) == 5


def test_codex_runs_on_big_rereview_delta():
    diff = _file_segment("src/app.py", added=50)
    assert review._codex_skip_tiny_delta("re-review", diff) is None


def test_codex_always_runs_on_full_review():
    diff = _file_segment("src/app.py", added=1)
    assert review._codex_skip_tiny_delta("full", diff) is None


def test_codex_threshold_boundary():
    diff = _file_segment("src/app.py", added=review.CODEX_RE_REVIEW_MIN_LINES)
    assert review._codex_skip_tiny_delta("re-review", diff) is None  # == runs


def test_codex_never_skips_truncated_diff():
    # A byte-capped re-review delta may hide real changes in the omitted
    # tail — codex reads the git tree, so it must still run.
    diff = (_file_segment("src/app.py", added=2)
            + f"{github_client.DIFF_TRUNCATION_MARKER} at 500000 bytes — 3 file(s) omitted: x.py]\n")
    assert review._codex_skip_tiny_delta("re-review", diff) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
