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


# ---------------------------------------------------------------------------
# S1 — codex overlap orchestration (_start_codex_task)
# ---------------------------------------------------------------------------

def test_start_codex_task_actually_starts_before_returning(monkeypatch):
    # create_task alone is lazy: without the yield inside _start_codex_task
    # the coroutine body (which spawns the codex subprocess) would not run
    # until the caller's next await — i.e. after all the sync precomp it
    # was supposed to overlap (the PR #147 blocker).
    import asyncio

    state = {"started": False}

    async def fake_codex(repo, sha):
        state["started"] = True
        return "findings"

    monkeypatch.setattr(review, "run_codex_session", fake_codex)

    async def main():
        task, t0, timer = await review._start_codex_task("/repo", "a" * 40)
        timer.cancel()
        started_at_return = state["started"]
        out = await task
        return started_at_return, out, t0

    started_at_return, out, t0 = asyncio.run(main())
    assert started_at_return is True
    assert out == "findings"
    assert t0 > 0


def test_codex_makes_progress_while_main_blocks_in_to_thread(monkeypatch):
    # The overlap contract: with precomp in a worker thread, the event loop
    # stays free, so the codex task progresses to completion DURING the
    # blocking work — not after it. Deterministic by construction: the fake
    # needs exactly ONE further loop iteration after launch, and ANY
    # to_thread suspension hands the loop those iterations — no wall-clock
    # margins to race a loaded CI runner.
    import asyncio
    import threading

    state = {"finished": False}
    release = threading.Event()

    async def fake_codex(repo, sha):
        await asyncio.sleep(0)
        state["finished"] = True
        return "findings"

    def blocking_precomp():
        # Wait until the loop has had the chance to run the codex task to
        # completion (it only needs ready-callback iterations, which the
        # loop processes while this thread holds main suspended).
        release.wait(timeout=10)

    monkeypatch.setattr(review, "run_codex_session", fake_codex)

    async def main():
        task, _, timer = await review._start_codex_task("/repo", "a" * 40)
        timer.cancel()
        task.add_done_callback(lambda _t: release.set())
        await asyncio.to_thread(blocking_precomp)
        finished_during_block = state["finished"]
        return finished_during_block, await task

    finished_during_block, out = asyncio.run(main())
    assert finished_during_block is True   # progressed while main was blocked
    assert out == "findings"


def test_codex_task_cancel_reaches_coroutine(monkeypatch):
    # The precomp cancel-guard relies on task.cancel() reaching
    # run_codex_session (whose finally kills the subprocess).
    import asyncio

    state = {"cancelled": False}

    async def fake_codex(repo, sha):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return "never"

    monkeypatch.setattr(review, "run_codex_session", fake_codex)

    async def main():
        task, _, timer = await review._start_codex_task("/repo", "a" * 40)
        timer.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(main())
    assert state["cancelled"] is True


def test_watchdog_cancels_codex_during_blocked_overlap_window(monkeypatch):
    # The launch-armed loop timer is the ACTIVE budget enforcer: it must
    # fire and cancel codex even while the main coroutine sits in
    # to_thread (the await-site wait_for can't run yet at that point).
    import asyncio
    import time as _t

    async def slow_codex(repo, sha):
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(review, "run_codex_session", slow_codex)
    monkeypatch.setattr(review, "SESSION_TIMEOUT_SECS", 0.05)

    async def main():
        task, _, timer = await review._start_codex_task("/repo", "a" * 40)
        await asyncio.to_thread(_t.sleep, 0.3)   # overlap window > budget
        cancelled_during_window = task.cancelled() or task.cancelling() > 0
        try:
            await task
        except asyncio.CancelledError:
            pass
        timer.cancel()
        return cancelled_during_window, task.cancelled()

    during, final = asyncio.run(main())
    assert during is True    # the timer fired while main was blocked
    assert final is True


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
