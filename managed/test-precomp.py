#!/usr/bin/env python3
"""Unit tests for the parallel precomp (PR4/S2): blame/churn fan out per-file
git calls on a thread pool, but the assembled context block must stay
BYTE-IDENTICAL to the old serial loop — output ordered by input, content
unchanged. A real throwaway git repo is the fixture (the functions shell out
to git; mocking subprocess would test nothing).

Run: python -m pytest managed/test-precomp.py
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import review  # noqa: E402
from review import compute_blame_summaries, compute_churn_data, _map_files  # noqa: E402


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("precomp-repo")

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True,
            capture_output=True,
            env={"GIT_AUTHOR_NAME": "alice", "GIT_AUTHOR_EMAIL": "a@x",
                 "GIT_COMMITTER_NAME": "alice", "GIT_COMMITTER_EMAIL": "a@x",
                 "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo)},
        )

    git("init", "-q")
    for name in ("zeta.py", "alpha.py", "mid.py"):
        (repo / name).write_text(f"# {name}\nline2\nline3\n")
    git("add", ".")
    git("commit", "-q", "-m", "initial")
    (repo / "alpha.py").write_text("# alpha.py\nchanged\nline3\n")
    git("add", ".")
    git("commit", "-q", "-m", "second")
    return str(repo)


# Deliberately NOT alphabetical and NOT churn-ordered — output must follow
# THIS order exactly.
FILES = ["zeta.py", "alpha.py", "mid.py"]


def test_blame_output_in_input_order(fixture_repo):
    out = compute_blame_summaries(fixture_repo, FILES)
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0].lstrip().startswith("zeta.py:")
    assert lines[1].lstrip().startswith("alpha.py:")
    assert lines[2].lstrip().startswith("mid.py:")


def test_blame_parallel_equals_per_file_serial(fixture_repo):
    # Byte-identical check: the batched (parallel) result equals the
    # concatenation of single-file (inherently serial) calls.
    batched = compute_blame_summaries(fixture_repo, FILES)
    serial = "\n".join(
        compute_blame_summaries(fixture_repo, [f]) for f in FILES
    )
    assert batched == serial


def test_churn_output_in_input_order(fixture_repo):
    out = compute_churn_data(fixture_repo, FILES)
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[0].lstrip().startswith("zeta.py:")
    assert lines[1].lstrip().startswith("alpha.py:")
    assert lines[2].lstrip().startswith("mid.py:")


def test_churn_parallel_equals_per_file_serial(fixture_repo):
    batched = compute_churn_data(fixture_repo, FILES)
    serial = "\n".join(compute_churn_data(fixture_repo, [f]) for f in FILES)
    assert batched == serial


def test_churn_counts_correct(fixture_repo):
    out = compute_churn_data(fixture_repo, FILES)
    assert "alpha.py: 2 commits" in out   # touched by both commits
    assert "zeta.py: 1 commits" in out


def test_failed_file_skipped_not_crashing(fixture_repo):
    out = compute_blame_summaries(fixture_repo, ["nonexistent.py", "alpha.py"])
    assert "nonexistent.py" not in out
    assert "alpha.py:" in out


def test_empty_inputs():
    assert compute_blame_summaries("", FILES) == ""
    assert compute_blame_summaries("/tmp", []) == ""
    assert compute_churn_data("", FILES) == ""


def test_map_files_preserves_order_under_concurrency():
    # Slow-first workload: with completion-order collection the fast items
    # would come back first; input-order collection must win.
    import time as _t

    def one(x):
        _t.sleep(0.05 if x == 0 else 0)
        return x

    assert _map_files(one, list(range(12))) == list(range(12))


def test_map_files_single_file_skips_pool():
    assert _map_files(lambda x: x * 2, [21]) == [42]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
